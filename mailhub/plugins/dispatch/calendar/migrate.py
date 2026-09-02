"""迁移：`migrate-alibaba-divisions` 命令实现。

修复阿里巴巴「同公司不同业务线」被合并的历史问题：
- 旧代码将所有阿里巴巴 confirmed 面试归一为 `[面试] 阿里巴巴`，导致千问事业部/淘天集团等
  不同业务线相互覆盖（后到的邮件更新前一条日程）。
- 新代码通过 `CandidateEvent.business_line` + `build_title(..., business_line=...)` 在
  title 上追加 `·业务线`，避免合并。
- 本迁移扫描 store + 日历里的旧 `[面试] 阿里巴巴` 类条目，删除并清 mark_processed +
  action_executions，让 `sync` 重建为两条独立日程。

注意：`action_executions` 也要清，否则 idempotency_key 已 succeeded 的旧记录会让 handler
在 `mailhub sync` 时直接返回 skipped（receipt.status="skipped"），engine 仍把它
当 applied 计，但实际上 CalDAV 远端没写、store 也不新插行——这就是「sync 输出新建 N 但
日历里看不到」的根因。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from mailhub.plugins.caldav import CalDavClient
from mailhub.runtime.config import Settings
from mailhub.store.sqlite import EventStore, StoredEvent

from .calendar_io import delete_calendar_event


@dataclass(frozen=True)
class AlibabaDivisionChange:
    event_row_id: int
    company: str
    title: str
    calendar_uid: str
    source_message_id: str
    extra_message_ids: tuple[str, ...] = ()
    purged_processed: int = 0
    purged_action_receipts: int = 0


# 旧格式识别：title 严格等于「[面试] 阿里巴巴」且公司含「阿里」，
# 排除已经有「·业务线」的新格式与 schedule_invite 等其他类型。
_OLD_FORMAT_TITLE_RE = re.compile(r"^\[(面试|笔试|测评)\]\s*阿里巴巴(?:\s*[校招招].*)?$")


def _is_old_alibaba_title(row: StoredEvent) -> bool:
    if row.status != "active":
        return False
    if "阿里" not in (row.company or ""):
        return False
    # 新格式 title 含「·」分隔符
    if "·" in (row.title or ""):
        return False
    return bool(_OLD_FORMAT_TITLE_RE.match(row.title or ""))


def migrate_alibaba_divisions(
    store: EventStore,
    settings: Settings,
    *,
    dry_run: bool,
    purge_processed: bool = False,
    client: Optional[CalDavClient] = None,
) -> list[AlibabaDivisionChange]:
    """扫描并删除旧格式「[面试] 阿里巴巴」类日程。

    - `--dry-run`：只列出待清理条目，不删除。
    - `purge_processed=True`：同时从 `processed_messages` 与 `action_executions`
      移除受影响邮件 ID，使 sync 能重新处理这些邮件以重建独立日程。
    """
    changes: list[AlibabaDivisionChange] = []
    for row in store.list_active_events():
        if not _is_old_alibaba_title(row):
            continue
        calendar_uid = row.sinks.get("calendar") or ""
        if not calendar_uid:
            # 没有日历 sink 的条目无法定位远端资源，跳过避免误删
            continue

        # 收集所有曾经绑到这条 store 行的 message_id，包括 source_message_id 与
        # 通过 event_messages 链接的所有邮件——后者包含「同一会话窗口内被同一 bug
        # 覆盖掉的另一封邮件」，是真正的清理目标。
        linked_message_ids: list[str] = []
        if row.source_message_id:
            linked_message_ids.append(row.source_message_id)
        for em in store.list_event_messages(row.id):
            if em.message_id and em.message_id not in linked_message_ids:
                linked_message_ids.append(em.message_id)

        change = AlibabaDivisionChange(
            event_row_id=row.id,
            company=row.company,
            title=row.title,
            calendar_uid=calendar_uid,
            source_message_id=row.source_message_id or "",
            extra_message_ids=tuple(
                mid for mid in linked_message_ids if mid != (row.source_message_id or "")
            ),
        )
        changes.append(change)
        if dry_run:
            continue

        # 1) 先删 CalDAV 远端资源
        try:
            delete_calendar_event(calendar_uid, settings, client)
        except Exception:
            # CalDAV 删除失败时不冒进硬删 store，留给用户重试
            continue
        # 2) 硬删 store 行（连带 event_messages、calendar_sinks）
        store.delete_event(row.id)
        # 3) 可选：清 mark_processed + action_executions，让 sync 重建
        purged_processed_count = 0
        purged_receipts_count = 0
        if purge_processed:
            for mid in linked_message_ids:
                purged_processed_count += store.unmark_processed(
                    mid, source_id=settings.source_id
                )
                purged_receipts_count += store.delete_action_receipts_for_message(
                    source_id=settings.source_id, message_id=mid
                )
            # frozen dataclass 不可变，重建一个带回填统计
            changes[-1] = AlibabaDivisionChange(
                event_row_id=change.event_row_id,
                company=change.company,
                title=change.title,
                calendar_uid=change.calendar_uid,
                source_message_id=change.source_message_id,
                extra_message_ids=change.extra_message_ids,
                purged_processed=purged_processed_count,
                purged_action_receipts=purged_receipts_count,
            )

    return changes