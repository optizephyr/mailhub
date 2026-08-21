from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from typing import Optional

from mailhub.contracts.messages import IngestBatch, MailMessage, SourceRef
from mailhub.logging.lifecycle import new_trace_id
from mailhub.plugins.caldav import CalDavClient
from mailhub.plugins.dispatch.calendar.calendar_io import (
    list_calendar_events,
    list_calendars,
)
from mailhub.plugins.dispatch.reminders.migrate import migrate_reminder_titles
from mailhub.plugins.dispatch.reminders.reminder_io import list_reminder_lists
from mailhub.plugins.policies.qiuzhao import QiuzhaoResolver
from mailhub.plugins.sources.qq_imap import QqImapSource
from mailhub.runtime.config import (
    load_settings,
    require_bark_config,
    require_caldav_account,
    require_caldav_config,
    require_mail_credentials,
)

import yaml
from mailhub.runtime.context import RunContext
from mailhub.runtime.engine import run_once
from mailhub.runtime.identity_migrate import migrate_identities
from mailhub.store.sqlite import EventStore


def cmd_list_calendars(_: argparse.Namespace) -> None:
    settings = load_settings()
    require_caldav_account(settings)
    names = list_calendars(settings)
    print("CalDAV 日历：")
    for name in names:
        print(f"  - {name}")


def _scan_window(days: int) -> tuple[datetime, datetime]:
    now = datetime.now()
    return now - timedelta(days=1), now + timedelta(days=days)


def cmd_list_reminders(_: argparse.Namespace) -> None:
    settings = load_settings()
    require_caldav_account(settings)
    names = list_reminder_lists(settings)
    print("CalDAV 提醒事项列表：")
    for name in names:
        print(f"  - {name}")


def cmd_scan_calendar(args: argparse.Namespace) -> None:
    settings = load_settings()
    require_caldav_config(settings)
    if not settings.calendar_name:
        raise SystemExit("日历未启用，请先配置 calendar_name")
    days = args.days or settings.calendar_scan_days or 90
    start, end = _scan_window(days)
    events = list_calendar_events(settings, start, end)
    print(f"日历「{settings.calendar_name}」未来 {days} 天内 {len(events)} 条日程：")
    for ev in events:
        marker = f"  来源={ev.marker_message_id}" if ev.marker_message_id else ""
        print(f"  - {ev.start_at}  {ev.summary}  uid={ev.uid}{marker}")


def cmd_migrate_reminder_titles(args: argparse.Namespace) -> None:
    settings = load_settings()
    if not settings.reminders_list:
        raise SystemExit("提醒事项未启用，请先配置 reminders_list")
    require_mail_credentials(settings)
    require_caldav_account(settings)
    client = CalDavClient(settings)
    client.collection(settings.reminders_list, "VTODO")
    source = QqImapSource(
        settings.qq_email,
        settings.qq_auth_code,
        source_id=settings.source_id,
    )

    store = EventStore(settings.data_dir / "synced.sqlite")
    missing_message_ids: list[str] = []
    try:
        changes = migrate_reminder_titles(
            store,
            settings,
            dry_run=bool(args.dry_run),
            client=client,
            source_ref_fetcher=source.fetch_by_source_refs,
            missing_message_ids=missing_message_ids,
        )
    finally:
        store.close()

    for change in changes:
        print(
            f"  - #{change.event_row_id} "
            f"{change.old_title} → {change.new_title}"
        )
    if missing_message_ids:
        print(f"跳过 {len(missing_message_ids)} 条：原邮件未找到")
    if args.dry_run:
        print(
            f"预览完成：将更新 {len(changes)} 条提醒事项标题，"
            f"跳过 {len(missing_message_ids)} 条"
        )
    else:
        print(
            f"迁移完成：已更新 {len(changes)} 条提醒事项标题，"
            f"跳过 {len(missing_message_ids)} 条"
        )


def cmd_migrate_identities(args: argparse.Namespace) -> None:
    settings = load_settings()
    require_caldav_account(settings)
    client = CalDavClient(settings)
    store = EventStore(settings.data_dir / "synced.sqlite")
    try:
        result = migrate_identities(
            store,
            client,
            source_id=settings.source_id,
            dry_run=bool(args.dry_run),
        )
    finally:
        store.close()

    for change in result.changes:
        print(f"  - #{change.event_row_id} item_uid={change.item_uid}")
    for missing in result.missing_resources:
        print(f"  - 资源无 UID：{missing}")
    for error in result.errors:
        print(f"  - 失败：{error}")
    verb = "将回填" if args.dry_run else "已回填"
    print(
        f"身份迁移完成：{verb} {len(result.changes)} 条 item_uid，"
        f"{result.linked_messages} 条邮件关联；"
        f"资源缺失 {len(result.missing_resources)}，失败 {len(result.errors)}"
    )
    if result.errors:
        raise SystemExit(1)


def cmd_sync(args: argparse.Namespace) -> None:
    settings = load_settings()
    require_mail_credentials(settings)
    require_bark_config(settings)
    test_io = any(
        hasattr(cmd_sync, name)
        for name in (
            "_test_create_calendar_event",
            "_test_create_reminder",
            "_test_list_calendar_events",
        )
    )
    caldav_client = None
    if not test_io:
        require_caldav_config(settings)
        caldav_client = CalDavClient(settings)
        if settings.calendar_name:
            caldav_client.collection(settings.calendar_name, "VEVENT")
        if settings.reminders_list:
            caldav_client.collection(settings.reminders_list, "VTODO")
    dry_run = bool(args.dry_run)
    full = bool(args.full)

    store = EventStore(settings.data_dir / "synced.sqlite")
    source = QqImapSource(
        settings.qq_email,
        settings.qq_auth_code,
        source_id=settings.source_id,
        lookback_days=settings.lookback_days,
        limit=settings.mail_limit,
        full=full,
    )
    # Allow tests to replace fetch via monkeypatch on this module
    if hasattr(cmd_sync, "_test_fetch"):
        source.fetch = cmd_sync._test_fetch  # type: ignore[method-assign]

    resolver = QiuzhaoResolver(settings)
    checkpoint = None if full else store.get_checkpoint(settings.source_id)
    if checkpoint:
        print(f"增量拉取：{settings.source_id} checkpoint > {checkpoint}")
    else:
        print(
            f"全量窗口拉取：最近 {settings.lookback_days} 天、最多 {settings.mail_limit} 封"
            + ("（--full）" if full else "（首次/无游标）")
        )

    extras = {
        "settings": settings,
    }
    if caldav_client is not None:
        extras["caldav_client"] = caldav_client
    # Test hooks
    for key in (
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
        "list_calendar_events",
        "create_reminder",
        "update_reminder",
        "delete_reminder",
    ):
        fn = getattr(cmd_sync, f"_test_{key}", None)
        if fn is not None:
            extras[key] = fn

    ctx = RunContext(
        run_id=new_trace_id(),
        dry_run=dry_run,
        full=full or checkpoint is None,
        source=source,
        resolver=resolver,
        planners=[],
        handlers={},
        store=store,
        source_id=settings.source_id,
        lifecycle_log_path=settings.lifecycle_log_path,
        extras=extras,
    )

    # Peek batch size for banner: engine fetches again. Avoid double fetch by
    # wrapping source to cache first fetch.
    cached: dict[str, IngestBatch] = {}
    orig_fetch = source.fetch

    def _cached_fetch(cp: Optional[str]) -> IngestBatch:
        key = cp or ""
        if key not in cached:
            cached[key] = orig_fetch(cp)
        return cached[key]

    source.fetch = _cached_fetch  # type: ignore[method-assign]
    preview = source.fetch(None if ctx.full else checkpoint)
    print(
        f"检查 {len(preview.messages)} 封"
        f"（checkpoint→{preview.next_checkpoint}）"
    )

    result = run_once(ctx)
    store.close()

    if dry_run:
        print(
            f"\n干跑完成：相关 {result.matched}，将新建 {result.created}，将更新 {result.updated}，"
            f"将取消 {result.cancelled}，将跳过 {result.skipped}，将失败 {len(result.failed)}"
        )
    else:
        print(
            f"\n完成：相关 {result.matched}，新建 {result.created}，更新 {result.updated}，"
            f"取消 {result.cancelled}，跳过 {result.skipped}，失败 {len(result.failed)}"
        )
    if args.json:
        payload = result.dry_run_reports if dry_run else result.events
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailhub",
        description="邮件处理中心：拉取邮箱、筛选重要邮件，并通过日程等方式提醒",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="拉取 → 研判 → 同步到日历/提醒事项")
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="只读匹配并展示最终动作，不写入、不推进游标",
    )
    sync.add_argument(
        "--full",
        action="store_true",
        help="忽略增量游标，按 LOOKBACK_DAYS 窗口重扫",
    )
    sync.add_argument("--json", action="store_true", help="输出解析结果 JSON")
    sync.set_defaults(func=cmd_sync)

    calendars = sub.add_parser("list-calendars", help="列出 CalDAV 日历名称")
    calendars.set_defaults(func=cmd_list_calendars)

    reminders = sub.add_parser("list-reminders", help="列出 CalDAV 提醒事项列表名称")
    reminders.set_defaults(func=cmd_list_reminders)

    migrate = sub.add_parser(
        "migrate-reminder-titles",
        help="从原邮件为已有提醒事项补上窗口时间和预计耗时",
    )
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="只展示将修改的标题，不写入 CalDAV 或本地数据库",
    )
    migrate.set_defaults(func=cmd_migrate_reminder_titles)

    identities = sub.add_parser(
        "migrate-identities",
        help="从现有 CalDAV UID 回填事项身份与邮件关联",
    )
    identities.add_argument(
        "--dry-run",
        action="store_true",
        help="只展示将回填的身份，不写入本地数据库",
    )
    identities.set_defaults(func=cmd_migrate_identities)

    scan = sub.add_parser("scan-calendar", help="列出目标日历里已有的日程")
    scan.add_argument(
        "--days", type=int, default=0, help="往后看多少天，默认取 CALENDAR_SCAN_DAYS"
    )
    scan.set_defaults(func=cmd_scan_calendar)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except FileNotFoundError as e:
        # 配置缺失/路径错误：loader 已自带 cp 提示
        print(str(e), file=sys.stderr)
        sys.exit(2)
    except (yaml.YAMLError, ValueError) as e:
        # 配置格式错或类型错：报错不带 traceback
        print(f"配置文件解析失败：{e}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
