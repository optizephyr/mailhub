from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .apple import (
    create_apple_event,
    delete_apple_event,
    list_apple_calendars,
    update_apple_event,
)
from .config import load_settings, require_mail_credentials
from .mail_qq import fetch_mails
from .models import CandidateEvent, StoredEvent, SyncResult
from .parser import parse_mail
from .store import EventStore


def cmd_list_apple(_: argparse.Namespace) -> None:
    names = list_apple_calendars()
    print("本机 Apple 日历：")
    for name in names:
        print(f"  - {name}")


def _find_target(store: EventStore, event: CandidateEvent) -> Optional[StoredEvent]:
    return store.find_active_event(
        company=event.company,
        event_type=event.event_type if event.event_type != "other" else "",
        references=event.references,
    )


def _apply_create(
    event: CandidateEvent,
    settings,
    store: EventStore,
) -> int:
    apple_event_id = create_apple_event(
        event, settings.apple_calendar_name, settings.reminder_minutes
    )
    return store.create_event(
        company=event.company,
        event_type=event.event_type,
        title=event.title,
        start_at=event.start_at,
        end_at=event.end_at,
        source_message_id=event.message_id,
        sinks={"apple": apple_event_id},
    )


def _apply_update(
    target: StoredEvent,
    event: CandidateEvent,
    settings,
    store: EventStore,
) -> None:
    sink_ids = dict(target.sinks)
    external_id = sink_ids.get("apple")
    if external_id and external_id != "apple-ok":
        update_apple_event(external_id, event, settings.apple_calendar_name)
    else:
        sink_ids["apple"] = create_apple_event(
            event, settings.apple_calendar_name, settings.reminder_minutes
        )
    store.update_event(
        target.id,
        title=event.title,
        start_at=event.start_at,
        end_at=event.end_at,
        source_message_id=event.message_id,
        sinks=sink_ids,
    )


def _apply_cancel(
    target: StoredEvent,
    event: CandidateEvent,
    store: EventStore,
) -> None:
    external_id = target.sinks.get("apple")
    if external_id and external_id != "apple-ok":
        delete_apple_event(external_id)
    store.cancel_event(target.id, event.message_id)


def cmd_sync(args: argparse.Namespace) -> None:
    settings = load_settings()
    require_mail_credentials(settings)

    dry_run = bool(args.dry_run)
    full = bool(args.full)

    store = EventStore(settings.data_dir / "synced.sqlite")
    result = SyncResult()

    last_uid = None if full else store.get_last_uid("INBOX")
    if last_uid:
        print(f"增量拉取：INBOX UID > {last_uid}")
    else:
        print(
            f"全量窗口拉取：最近 {settings.lookback_days} 天、最多 {settings.mail_limit} 封"
            + ("（--full）" if full else "（首次/无游标）")
        )

    fetched = fetch_mails(
        settings.qq_email,
        settings.qq_auth_code,
        lookback_days=settings.lookback_days,
        limit=settings.mail_limit,
        since_uid=last_uid,
        full=full or last_uid is None,
    )
    result.scanned = fetched.examined
    print(
        f"模式={fetched.mode}，检查 {fetched.examined} 封，命中秋招相关 {len(fetched.mails)} 封"
        f"（max_uid={fetched.max_uid}）"
    )

    for mail in fetched.mails:
        event = parse_mail(mail, settings)
        if not event:
            continue
        result.matched += 1
        result.events.append(event)

        time_part = (
            f"{event.start_at} → {event.end_at}"
            if event.action != "cancel"
            else "(取消)"
        )
        print(
            f"\n• [{event.action}] {event.title}\n"
            f"  {time_part}\n"
            f"  置信度={event.confidence:.2f}  主题={event.subject[:60]}"
        )

        if dry_run:
            continue

        if store.already_processed(event.message_id):
            result.skipped += 1
            print("  - 该邮件已处理，跳过")
            continue

        try:
            if event.action == "cancel":
                target = _find_target(store, event)
                if not target:
                    result.failed.append(f"cancel: 未找到可取消的旧日程 company={event.company}")
                    print("  - 未找到可取消的旧日程（需公司名匹配或回复链）")
                    store.mark_processed(event.message_id, "cancel", None)
                    continue
                _apply_cancel(target, event, store)
                store.mark_processed(event.message_id, "cancel", target.id)
                result.cancelled += 1
                print(f"  - 已删除旧日程 #{target.id}")
            elif event.action == "reschedule":
                target = _find_target(store, event)
                if target:
                    _apply_update(target, event, settings, store)
                    store.mark_processed(event.message_id, "reschedule", target.id)
                    result.updated += 1
                    print(f"  - 已更新日程 #{target.id}")
                else:
                    row_id = _apply_create(event, settings, store)
                    store.mark_processed(event.message_id, "reschedule", row_id)
                    result.created += 1
                    print("  - 未找到旧日程，已新建")
            else:
                # create：若同公司同学段已有活跃日程且时间不同，视为改期覆盖
                target = _find_target(store, event)
                if target and target.start_at and target.start_at != event.start_at:
                    _apply_update(target, event, settings, store)
                    store.mark_processed(event.message_id, "create", target.id)
                    result.updated += 1
                    print(f"  - 检测到时间变化，已更新日程 #{target.id}")
                elif target and target.start_at == event.start_at:
                    store.mark_processed(event.message_id, "create", target.id)
                    result.skipped += 1
                    print(f"  - 已存在相同时间日程 #{target.id}，跳过")
                else:
                    row_id = _apply_create(event, settings, store)
                    store.mark_processed(event.message_id, "create", row_id)
                    result.created += 1
                    print("  - 已创建")
        except Exception as exc:
            msg = str(exc)
            result.failed.append(msg)
            print(f"  - 失败: {msg}")

    if not dry_run and fetched.max_uid:
        prev = store.get_last_uid("INBOX") or 0
        if fetched.max_uid >= prev:
            store.set_last_uid(fetched.max_uid, "INBOX")
            print(f"\n游标已更新：INBOX last_uid={fetched.max_uid}")

    store.close()
    print(
        f"\n完成：相关 {result.matched}，新建 {result.created}，更新 {result.updated}，"
        f"取消 {result.cancelled}，跳过 {result.skipped}，失败 {len(result.failed)}"
    )
    if args.json:
        print(json.dumps([e.to_dict() for e in result.events], ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-to-calendar",
        description="从 QQ 邮箱解析秋招面试/笔试/测评邮件，写入 Apple 日历",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="扫描邮件并创建/更新/删除日程")
    sync.add_argument("--dry-run", action="store_true", help="只解析不写入")
    sync.add_argument(
        "--full",
        action="store_true",
        help="忽略增量游标，按 LOOKBACK_DAYS 窗口重扫",
    )
    sync.add_argument("--json", action="store_true", help="输出解析结果 JSON")
    sync.set_defaults(func=cmd_sync)

    apple = sub.add_parser("list-apple", help="列出本机 Apple 日历名称")
    apple.set_defaults(func=cmd_list_apple)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
