from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .apple import (
    create_apple_event,
    delete_apple_event,
    list_apple_calendars,
    list_apple_events,
    update_apple_event,
)
from .calendar_match import companies_match, match_calendar_event
from .config import load_settings, require_mail_credentials
from .lifecycle_log import MailTrace, planned_event_brief
from .mail_qq import fetch_mails
from .models import AppleEventRef, CandidateEvent, StoredEvent, SyncResult
from .parser import parse_mail
from .store import EventStore


@dataclass
class ApplyPlan:
    result: str
    summary: str
    match_via: str = "none"
    event_row_id: Optional[int] = None
    error: Optional[str] = None


def _session_event_from_candidate(event: CandidateEvent) -> StoredEvent:
    """本轮 sync/dry-run 内「已规划新建」的虚拟日程，供后续邮件去重。"""
    return StoredEvent(
        id=0,
        company=event.company,
        event_type=event.event_type,
        title=event.title,
        start_at=event.start_at,
        end_at=event.end_at,
        status="active",
        source_message_id=event.message_id,
        sinks={},
    )


def _match_session(
    event: CandidateEvent,
    session: list[StoredEvent],
) -> Optional[StoredEvent]:
    company = (event.company or "").strip()
    if not company or not session:
        return None
    want_type = event.event_type if event.event_type != "other" else ""
    # 后出现的优先（同场次重复邀请取本轮最新规划）
    for candidate in reversed(session):
        if want_type and candidate.event_type not in ("", "other", want_type):
            continue
        if companies_match(company, candidate.company):
            return candidate
    return None


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


def _peek_calendar_match(event: CandidateEvent, settings) -> Optional[AppleEventRef]:
    """只读扫描 Apple 日历，不写库。"""
    if settings.calendar_scan_days <= 0:
        return None

    start, end = _scan_window(settings.calendar_scan_days)
    try:
        existing = list_apple_events(settings.apple_calendar_name, start, end)
    except RuntimeError as exc:
        print(f"  - 日历兜底匹配跳过（读取失败）: {exc}")
        return None

    return match_calendar_event(event, existing)


def _adopt_from_calendar(
    store: EventStore,
    event: CandidateEvent,
    settings,
) -> Optional[StoredEvent]:
    """本地库没记录时（库被清过 / 旧版本建的 / 人工改过），回到日历里找并接管。"""
    matched = _peek_calendar_match(event, settings)
    if not matched:
        return None

    row_id = store.create_event(
        company=event.company,
        event_type=event.event_type,
        title=matched.summary,
        start_at=matched.start_at,
        end_at=matched.end_at,
        source_message_id=matched.marker_message_id or event.message_id,
        sinks={"apple": matched.uid},
    )
    print(f"  - 日历中已有「{matched.summary}」{matched.start_at}，接管为 #{row_id}")
    return store.get_event(row_id)


def _virtual_from_calendar(matched: AppleEventRef, event: CandidateEvent) -> StoredEvent:
    """dry-run 用：日历命中但不落库，id=0 表示无 event_row_id。"""
    return StoredEvent(
        id=0,
        company=event.company,
        event_type=event.event_type,
        title=matched.summary,
        start_at=matched.start_at,
        end_at=matched.end_at,
        status="active",
        source_message_id=matched.marker_message_id or event.message_id,
        sinks={"apple": matched.uid},
    )


def _find_target(
    store: EventStore,
    event: CandidateEvent,
    settings,
    *,
    adopt: bool = True,
    session: Optional[list[StoredEvent]] = None,
) -> tuple[Optional[StoredEvent], str]:
    """返回 (目标日程, 匹配途径)。

    途径: references | session | company_type | calendar_adopt | none。
    adopt=False 时只读匹配日历，不写库（dry-run）。
    session 为本轮已规划/已写入的活跃日程，用于同批重复邀请去重。
    """
    refs = [r for r in event.references if r]
    if refs:
        target = store.find_active_event(references=refs)
        if target:
            return target, "references"

    session_hit = _match_session(event, session or [])
    if session_hit:
        return session_hit, "session"

    target = store.find_active_event(
        company=event.company,
        event_type=event.event_type if event.event_type != "other" else "",
    )
    if target:
        return target, "company_type"

    if adopt:
        adopted = _adopt_from_calendar(store, event, settings)
        if adopted:
            return adopted, "calendar_adopt"
    else:
        matched = _peek_calendar_match(event, settings)
        if matched:
            return _virtual_from_calendar(matched, event), "calendar_adopt"
    return None, "none"


def _plan_apply(
    store: EventStore,
    event: CandidateEvent,
    settings,
    *,
    session: Optional[list[StoredEvent]] = None,
) -> ApplyPlan:
    """dry-run：按正式逻辑只读判定最终动作，不写任何数据。"""
    if store.already_processed(event.message_id):
        return ApplyPlan(
            result="would_skip_duplicate",
            summary="该邮件已处理过，将跳过",
        )

    target, via = _find_target(
        store, event, settings, adopt=False, session=session
    )
    row_id = target.id if target and target.id > 0 else None

    if event.action == "cancel":
        if not target:
            return ApplyPlan(
                result="would_fail",
                summary="取消失败：未找到可取消的旧日程",
                match_via=via,
                error="no matching active event",
            )
        return ApplyPlan(
            result="would_cancel",
            summary="将取消匹配到的旧日程",
            match_via=via,
            event_row_id=row_id,
        )

    if event.action == "reschedule":
        if target:
            return ApplyPlan(
                result="would_update",
                summary="将改期并更新匹配到的旧日程",
                match_via=via,
                event_row_id=row_id,
            )
        return ApplyPlan(
            result="would_create",
            summary="改期未匹配到旧日程，将新建",
            match_via=via,
        )

    # create
    if target and target.start_at and target.start_at != event.start_at:
        return ApplyPlan(
            result="would_update",
            summary="检测到时间变化，将更新匹配到的旧日程",
            match_via=via,
            event_row_id=row_id,
        )
    if target and target.start_at == event.start_at:
        return ApplyPlan(
            result="would_skip_same",
            summary="已存在相同时间日程，将跳过",
            match_via=via,
            event_row_id=row_id,
        )
    return ApplyPlan(
        result="would_create",
        summary="将新建日程",
        match_via=via,
    )


def _print_event_details(event: CandidateEvent) -> None:
    time_part = (
        f"{event.start_at} → {event.end_at}"
        if event.action != "cancel"
        else "(取消)"
    )
    desc = event.description or ""
    if len(desc) > 200:
        desc = desc[:200] + "…"

    lines = [
        f"\n• [{event.action}] {event.title}",
        f"  公司={event.company or '-'}  类型={event.event_type}",
        f"  时间={time_part}",
    ]
    if event.location:
        lines.append(f"  地点={event.location}")
    if event.meeting_url:
        lines.append(f"  会议={event.meeting_url}")
    if desc:
        lines.append(f"  描述={desc}")
    lines.append(f"  置信度={event.confidence:.2f}  主题={event.subject[:60]}")
    print("\n".join(lines))


def _tally_dry_run(result: SyncResult, plan: ApplyPlan) -> None:
    if plan.result == "would_create":
        result.created += 1
    elif plan.result == "would_update":
        result.updated += 1
    elif plan.result == "would_cancel":
        result.cancelled += 1
    elif plan.result in ("would_skip_duplicate", "would_skip_same"):
        result.skipped += 1
    elif plan.result == "would_fail":
        result.failed.append(plan.summary)


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


def _finish_apply(
    trace: MailTrace,
    *,
    result: str,
    status: str,
    summary: str,
    match_via: str = "none",
    event_row_id: Optional[int] = None,
    sinks: Optional[dict[str, str]] = None,
    error: Optional[str] = None,
) -> None:
    stage: dict[str, object] = {
        "name": "apply",
        "result": result,
        "match": {"via": match_via},
    }
    if event_row_id is not None:
        stage["event_row_id"] = event_row_id
    if sinks:
        stage["sinks"] = sinks
    if error:
        stage["error"] = error
    trace.add_stage(stage)
    trace.finish(status, summary)


def _finish_applied(
    trace: MailTrace,
    store: EventStore,
    *,
    result: str,
    summary: str,
    match_via: str,
    event_row_id: int,
    sinks: Optional[dict[str, str]] = None,
) -> None:
    """写入成功后的统一收尾：从 store 补 sinks（可显式传入）。"""
    if sinks is None:
        row = store.get_event(event_row_id)
        sinks = row.sinks if row else None
    _finish_apply(
        trace,
        result=result,
        status="applied",
        summary=summary,
        match_via=match_via,
        event_row_id=event_row_id,
        sinks=sinks,
    )


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

    run_meta = {
        "mode": fetched.mode,
        "dry_run": dry_run,
        "full": full,
    }
    # 本轮已规划/已写入的活跃日程：同批重复邀请（如两封拼多多笔试）合并到同一条
    session: list[StoredEvent] = []

    for mail in fetched.mails:
        trace = MailTrace(
            lifecycle_path=settings.lifecycle_log_path,
            mail=mail,
            run=run_meta,
        )
        event = parse_mail(mail, settings, trace=trace)
        if not event:
            continue
        result.matched += 1
        result.events.append(event)

        if dry_run:
            plan = _plan_apply(store, event, settings, session=session)
            _print_event_details(event)
            _tally_dry_run(result, plan)
            planned = planned_event_brief(event)
            result.dry_run_reports.append(
                {
                    "apply": plan.result,
                    "match_via": plan.match_via,
                    "event": event.to_dict(),
                }
            )
            if plan.result == "would_create":
                session.append(_session_event_from_candidate(event))
            elif plan.result == "would_update" and plan.match_via == "session":
                # 更新本轮虚拟日程的时间
                hit = _match_session(event, session)
                if hit:
                    hit.start_at = event.start_at
                    hit.end_at = event.end_at
                    hit.title = event.title
            trace.finish_dry_run(
                plan.summary,
                result=plan.result,
                match_via=plan.match_via,
                event_row_id=plan.event_row_id,
                planned_event=planned,
                error=plan.error,
            )
            continue

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

        if store.already_processed(event.message_id):
            result.skipped += 1
            print("  - 该邮件已处理，跳过")
            _finish_apply(
                trace,
                result="skipped_duplicate",
                status="skipped_duplicate",
                summary="该邮件已处理过，跳过",
            )
            continue

        try:
            if event.action == "cancel":
                target, via = _find_target(store, event, settings, session=session)
                if not target:
                    result.failed.append(
                        f"cancel: 未找到可取消的旧日程 company={event.company}"
                    )
                    print("  - 未找到可取消的旧日程（需公司名匹配或回复链）")
                    store.mark_processed(event.message_id, "cancel", None)
                    _finish_apply(
                        trace,
                        result="failed",
                        status="failed",
                        summary="取消失败：未找到可取消的旧日程",
                        match_via=via,
                        error="no matching active event",
                    )
                    continue
                _apply_cancel(target, event, store)
                store.mark_processed(event.message_id, "cancel", target.id)
                result.cancelled += 1
                if target.id > 0:
                    session[:] = [s for s in session if s.id != target.id]
                else:
                    session[:] = [
                        s
                        for s in session
                        if s.source_message_id != target.source_message_id
                    ]
                print(f"  - 已删除旧日程 #{target.id}")
                _finish_applied(
                    trace,
                    store,
                    result="cancelled",
                    summary=f"已取消日程 #{target.id}",
                    match_via=via,
                    event_row_id=target.id,
                    sinks=target.sinks,
                )
            elif event.action == "reschedule":
                target, via = _find_target(store, event, settings, session=session)
                if target:
                    _apply_update(target, event, settings, store)
                    store.mark_processed(event.message_id, "reschedule", target.id)
                    result.updated += 1
                    print(f"  - 已更新日程 #{target.id}")
                    _finish_applied(
                        trace,
                        store,
                        result="updated",
                        summary=f"改期并更新日程 #{target.id}",
                        match_via=via,
                        event_row_id=target.id,
                    )
                else:
                    row_id = _apply_create(event, settings, store)
                    store.mark_processed(event.message_id, "reschedule", row_id)
                    result.created += 1
                    created = store.get_event(row_id)
                    if created:
                        session.append(created)
                    print("  - 未找到旧日程，已新建")
                    _finish_applied(
                        trace,
                        store,
                        result="created",
                        summary=f"改期未匹配到旧日程，已新建 #{row_id}",
                        match_via=via,
                        event_row_id=row_id,
                    )
            else:
                # create：若同公司同学段已有活跃日程且时间不同，视为改期覆盖
                target, via = _find_target(store, event, settings, session=session)
                if target and target.start_at and target.start_at != event.start_at:
                    _apply_update(target, event, settings, store)
                    store.mark_processed(event.message_id, "create", target.id)
                    result.updated += 1
                    print(f"  - 检测到时间变化，已更新日程 #{target.id}")
                    _finish_applied(
                        trace,
                        store,
                        result="updated",
                        summary=f"检测到时间变化，已更新日程 #{target.id}",
                        match_via=via,
                        event_row_id=target.id,
                    )
                elif target and target.start_at == event.start_at:
                    store.mark_processed(event.message_id, "create", target.id)
                    result.skipped += 1
                    print(f"  - 已存在相同时间日程 #{target.id}，跳过")
                    _finish_apply(
                        trace,
                        result="skipped_same",
                        status="skipped_same",
                        summary=f"已存在相同时间日程 #{target.id}，跳过",
                        match_via=via,
                        event_row_id=target.id if target.id > 0 else None,
                        sinks=target.sinks,
                    )
                else:
                    row_id = _apply_create(event, settings, store)
                    store.mark_processed(event.message_id, "create", row_id)
                    result.created += 1
                    created = store.get_event(row_id)
                    if created:
                        session.append(created)
                    print("  - 已创建")
                    _finish_applied(
                        trace,
                        store,
                        result="created",
                        summary=f"已新建日程 #{row_id}",
                        match_via=via,
                        event_row_id=row_id,
                    )
        except Exception as exc:
            msg = str(exc)
            result.failed.append(msg)
            print(f"  - 失败: {msg}")
            if not trace.finished:
                _finish_apply(
                    trace,
                    result="failed",
                    status="failed",
                    summary=f"写入失败：{msg}",
                    error=msg,
                )

    if not dry_run and fetched.max_uid:
        prev = store.get_last_uid("INBOX") or 0
        if fetched.max_uid >= prev:
            store.set_last_uid(fetched.max_uid, "INBOX")
            print(f"\n游标已更新：INBOX last_uid={fetched.max_uid}")

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
        payload = (
            result.dry_run_reports
            if dry_run
            else [e.to_dict() for e in result.events]
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-to-calendar",
        description="从 QQ 邮箱解析秋招面试/笔试/测评邮件，写入 Apple 日历",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="扫描邮件并创建/更新/删除日程")
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="只读匹配并展示最终动作与日程，不写入、不推进游标",
    )
    sync.add_argument(
        "--full",
        action="store_true",
        help="忽略增量游标，按 LOOKBACK_DAYS 窗口重扫",
    )
    sync.add_argument("--json", action="store_true", help="输出解析结果 JSON")
    sync.set_defaults(func=cmd_sync)

    apple = sub.add_parser("list-apple", help="列出本机 Apple 日历名称")
    apple.set_defaults(func=cmd_list_apple)

    scan = sub.add_parser("scan-apple", help="列出目标日历里已有的日程（用于核对匹配）")
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
