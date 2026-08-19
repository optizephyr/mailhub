from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from typing import Optional

from mailhub.contracts.messages import IngestBatch, MailMessage, SourceRef
from mailhub.logging.lifecycle import new_trace_id
from mailhub.plugins.dispatch.apple_calendar import list_apple_calendars, list_apple_events
from mailhub.plugins.policies.qiuzhao import QiuzhaoResolver
from mailhub.plugins.sources.qq_imap import QqImapSource
from mailhub.runtime.config import Settings, load_settings, require_mail_credentials
from mailhub.runtime.context import RunContext
from mailhub.runtime.engine import run_once
from mailhub.store.sqlite import EventStore


def cmd_list_apple(_: argparse.Namespace) -> None:
    names = list_apple_calendars()
    print("本机 Apple 日历：")
    for name in names:
        print(f"  - {name}")


def _scan_window(days: int) -> tuple[datetime, datetime]:
    now = datetime.now()
    return now - timedelta(days=1), now + timedelta(days=days)


def cmd_scan_apple(args: argparse.Namespace) -> None:
    settings = load_settings()
    days = args.days or settings.calendar_scan_days or 90
    start, end = _scan_window(days)
    events = list_apple_events(settings.apple_calendar_name, start, end)
    print(f"日历「{settings.apple_calendar_name}」未来 {days} 天内 {len(events)} 条日程：")
    for ev in events:
        marker = f"  来源={ev.marker_message_id}" if ev.marker_message_id else ""
        print(f"  - {ev.start_at}  {ev.summary}  uid={ev.uid}{marker}")


def cmd_run(args: argparse.Namespace) -> None:
    settings = load_settings()
    require_mail_credentials(settings)
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
    if hasattr(cmd_run, "_test_fetch"):
        source.fetch = cmd_run._test_fetch  # type: ignore[method-assign]

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
    # Test hooks
    for key in (
        "create_apple_event",
        "update_apple_event",
        "delete_apple_event",
        "list_apple_events",
    ):
        fn = getattr(cmd_run, f"_test_{key}", None)
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


# backward-compatible alias
cmd_sync = cmd_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailhub",
        description="邮件处理中心：拉取邮箱、筛选重要邮件，并通过日程等方式提醒",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="拉取 → 研判 → 分发提醒")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="只读匹配并展示最终动作，不写入、不推进游标",
    )
    run.add_argument(
        "--full",
        action="store_true",
        help="忽略增量游标，按 LOOKBACK_DAYS 窗口重扫",
    )
    run.add_argument("--json", action="store_true", help="输出解析结果 JSON")
    run.set_defaults(func=cmd_run)

    # alias
    sync = sub.add_parser("sync", help="同 run（兼容旧命令）")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--full", action="store_true")
    sync.add_argument("--json", action="store_true")
    sync.set_defaults(func=cmd_run)

    apple = sub.add_parser("list-apple", help="列出本机 Apple 日历名称")
    apple.set_defaults(func=cmd_list_apple)

    scan = sub.add_parser("scan-apple", help="列出目标日历里已有的日程")
    scan.add_argument(
        "--days", type=int, default=0, help="往后看多少天，默认取 CALENDAR_SCAN_DAYS"
    )
    scan.set_defaults(func=cmd_scan_apple)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
