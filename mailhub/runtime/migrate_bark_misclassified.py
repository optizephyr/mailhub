"""把已被新版启发式重新分类的「已预约成功」类 bark.push 记录标记 + 清 processed_messages。

背景：
- 旧启发式把「预约成功通知 / 已成功预约」类邮件识别为 ``stage=schedule_invite``，
  进而走 BarkPlanner 推送「{company} 请预约」。这种推送对已经完成预约的候选人
  是误导。
- 新版启发式（见 ``CONFIRMED_BOOKING_SIGNALS``）会把这类邮件识别为 ``stage=confirmed``，
  且正文若没有具体时间会让 ``heuristic_parse`` 返回 None，从而被丢弃。

本迁移：
- 扫描 ``mail_lifecycle.jsonl``，找出主题命中 ``CONFIRMED_BOOKING_SIGNALS`` 且
  ``outcome.status == 'applied'`` 且存在 ``apply/pushed`` 阶段的邮件。
- 对每条命中，在 ``action_executions`` 表里**保留**该 bark.push 行（作为审计），
  仅把 ``error`` 字段填上「misclassified: ...」说明。
- 同时从 ``processed_messages`` 删除该邮件，让下次 ``mailhub sync`` 用新启发式
  重新处理它（正确丢弃，不重复推 Bark）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mailhub.plugins.policies.qiuzhao.parser import CONFIRMED_BOOKING_SIGNALS
from mailhub.store.sqlite import EventStore


@dataclass(frozen=True)
class MisclassifiedRecord:
    message_id: str
    subject: str
    from_: str
    ts: str
    keyword: str
    idempotency_key: str
    executed_at: str
    error_note: str
    purged_processed: bool = False
    error_annotated: bool = False
    skipped_reason: str = ""


@dataclass
class ReclassifyResult:
    records: list[MisclassifiedRecord] = field(default_factory=list)
    scanned_lifecycle_entries: int = 0
    skipped_already_annotated: int = 0
    skipped_no_action_record: int = 0

    @property
    def changed(self) -> list[MisclassifiedRecord]:
        return [r for r in self.records if r.error_annotated or r.purged_processed]


def _looks_like_booking_confirmation(subject: str) -> Optional[str]:
    """若主题命中 CONFIRMED_BOOKING_SIGNALS 任一短语，返回该短语；否则返回 None。"""
    if not subject:
        return None
    for kw in CONFIRMED_BOOKING_SIGNALS:
        if kw and kw in subject:
            return kw
    return None


def _was_pushed(stages: list[dict]) -> bool:
    """apply/pushed 阶段表示走了 BarkPlanner 推送（其它 sink 的 result 是 created/updated/cancelled）。"""
    return any(
        s.get("name") == "apply" and s.get("result") == "pushed"
        for s in stages
    )


def _scan_lifecycle(lifecycle_path: Path) -> list[dict]:
    """读取 lifecycle jsonl；空文件/不存在返回空列表。"""
    if not lifecycle_path.is_file():
        return []
    out: list[dict] = []
    with lifecycle_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _find_bark_action_rows(
    store: EventStore, source_id: str, message_id: str
) -> list[dict]:
    """按 (source_id, message_id) 找 action_executions 里 action_type='bark.push' 的行。

    实际数据里 idempotency_key 有两种格式：
      - bark.push: `bark:{source_id}:{message_id}`（无后缀）
      - 其它 action: `{source_id}:{message_id}:{action}:{result}`

    所以要 LIKE 两个前缀才能匹配上 bark 行。
    """
    bark_prefix = f"bark:{source_id}:{message_id}"
    other_prefix = f"{source_id}:{message_id}:"
    rows = store._conn.execute(
        """
        SELECT idempotency_key, action_type, status, external_id, error, executed_at
        FROM action_executions
        WHERE idempotency_key LIKE ?
           OR idempotency_key LIKE ?
        """,
        (bark_prefix + "%", other_prefix + "%"),
    ).fetchall()
    return [dict(r) for r in rows if r["action_type"] == "bark.push"]


def reclassify_bark_misclassified(
    store: EventStore,
    lifecycle_path: Path,
    *,
    source_id: str = "qq.default",
    dry_run: bool = True,
) -> ReclassifyResult:
    """扫 lifecycle + action_executions，对历史误判的 bark.push 记录：
    - annotate action_executions.error（保留行为审计）
    - delete processed_messages 对应行（让 sync 重跑并被新启发式正确丢弃）

    dry_run=True 时仅返回会变更的记录，不写任何数据。
    """
    result = ReclassifyResult()
    entries = _scan_lifecycle(lifecycle_path)
    result.scanned_lifecycle_entries = len(entries)

    for entry in entries:
        mail = entry.get("mail") or {}
        subject = mail.get("subject") or ""
        message_id = mail.get("message_id") or ""
        outcome = entry.get("outcome") or {}
        if outcome.get("status") != "applied":
            continue

        keyword = _looks_like_booking_confirmation(subject)
        if not keyword:
            continue
        if not _was_pushed(entry.get("stages") or []):
            continue
        if not message_id:
            continue

        bark_rows = _find_bark_action_rows(store, source_id, message_id)
        if not bark_rows:
            # lifecycle 里 apply/pushed 但 action_executions 没找到 bark 行 —— 跳过（异常状态）
            result.skipped_no_action_record += 1
            continue

        for row in bark_rows:
            existing_error = (row.get("error") or "").strip()
            if existing_error.startswith("misclassified:"):
                result.skipped_already_annotated += 1
                continue

            note = (
                f"misclassified: 主题命中 CONFIRMED_BOOKING_SIGNALS={keyword!r}，"
                f"新版启发式应判 stage=confirmed；正文若无具体时间，heuristic_parse "
                f"会返回 None，sync 重跑后正确丢弃（不重复推 Bark）。"
                f"原 bark.push 推送保留为审计。"
            )

            record = MisclassifiedRecord(
                message_id=message_id,
                subject=subject,
                from_=mail.get("from") or "",
                ts=entry.get("ts") or "",
                keyword=keyword,
                idempotency_key=row["idempotency_key"],
                executed_at=row.get("executed_at") or "",
                error_note=note,
            )

            if dry_run:
                result.records.append(record)
                continue

            annotated = store.annotate_action_error(row["idempotency_key"], note)
            purged = store.unmark_processed(message_id, source_id)
            record = MisclassifiedRecord(
                message_id=record.message_id,
                subject=record.subject,
                from_=record.from_,
                ts=record.ts,
                keyword=record.keyword,
                idempotency_key=record.idempotency_key,
                executed_at=record.executed_at,
                error_note=record.error_note,
                purged_processed=purged > 0,
                error_annotated=annotated > 0,
            )
            result.records.append(record)

    return result
