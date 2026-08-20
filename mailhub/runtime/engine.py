from __future__ import annotations

from typing import Optional

from mailhub.contracts.messages import IngestBatch
from mailhub.contracts.resolve import IgnoredMail, ResolveFailure, ResolvedMail
from mailhub.logging.lifecycle import MailTrace, planned_event_brief
from mailhub.plugins.dispatch.apple_calendar import (
    ACTION_CANCEL,
    ACTION_CREATE,
    ACTION_FAIL,
    ACTION_SKIP,
    ACTION_UPDATE,
    AppleCalendarHandler,
    AppleCalendarPlanner,
    match_session,
    session_event_from_candidate,
)
from mailhub.plugins.dispatch.apple_reminders import (
    ACTION_CANCEL as REM_CANCEL,
    ACTION_CREATE as REM_CREATE,
    ACTION_FAIL as REM_FAIL,
    ACTION_SKIP as REM_SKIP,
    ACTION_UPDATE as REM_UPDATE,
    AppleRemindersHandler,
    AppleRemindersPlanner,
)
from mailhub.plugins.dispatch.apple_reminders import (
    match_session as rem_match_session,
    session_event_from_candidate as rem_session_event_from_candidate,
)
from mailhub.plugins.dispatch.bark import ACTION_PUSH, BarkHandler, BarkPlanner
from mailhub.plugins.policies.qiuzhao import resolved_to_candidate
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent
from mailhub.runtime.context import RunContext, RunResult
from mailhub.store.sqlite import StoredEvent


def run_once(ctx: RunContext) -> RunResult:
    """Ingest → Resolve → Dispatch one batch."""
    result = RunResult()
    store = ctx.store
    dry_run = ctx.dry_run
    settings = ctx.extras["settings"]

    checkpoint = None if ctx.full else store.get_checkpoint(ctx.source_id)
    batch: IngestBatch = ctx.source.fetch(checkpoint)
    result.received_count = len(batch.messages)
    result.scanned = result.received_count

    CREATE_TYPES = {ACTION_CREATE, REM_CREATE}
    UPDATE_TYPES = {ACTION_UPDATE, REM_UPDATE}
    CANCEL_TYPES = {ACTION_CANCEL, REM_CANCEL}
    SKIP_TYPES = {ACTION_SKIP, REM_SKIP}
    FAIL_TYPES = {ACTION_FAIL, REM_FAIL}

    session: list[StoredEvent] = []
    handler = AppleCalendarHandler(store, settings)
    rem_handler = AppleRemindersHandler(store, settings)
    bark_handler = BarkHandler()
    if "create_apple_event" in ctx.extras:
        handler.create_apple_event = ctx.extras["create_apple_event"]
    if "update_apple_event" in ctx.extras:
        handler.update_apple_event = ctx.extras["update_apple_event"]
    if "delete_apple_event" in ctx.extras:
        handler.delete_apple_event = ctx.extras["delete_apple_event"]
    if ctx.extras.get("list_apple_events") is not None:
        import mailhub.plugins.dispatch.apple_calendar.planner as planner_mod

        planner_mod.list_apple_events = ctx.extras["list_apple_events"]
    if "create_apple_reminder" in ctx.extras:
        rem_handler.create_apple_reminder = ctx.extras["create_apple_reminder"]
    if "update_apple_reminder" in ctx.extras:
        rem_handler.update_apple_reminder = ctx.extras["update_apple_reminder"]
    if "delete_apple_reminder" in ctx.extras:
        rem_handler.delete_apple_reminder = ctx.extras["delete_apple_reminder"]

    planner = AppleCalendarPlanner(
        store, settings, session, dry_run=dry_run, source_id=ctx.source_id
    )
    rem_planner = AppleRemindersPlanner(
        store, settings, session, dry_run=dry_run, source_id=ctx.source_id
    )
    bark_planner = BarkPlanner()
    handlers = {
        ACTION_CREATE: handler,
        ACTION_UPDATE: handler,
        ACTION_CANCEL: handler,
        ACTION_SKIP: handler,
        ACTION_FAIL: handler,
        REM_CREATE: rem_handler,
        REM_UPDATE: rem_handler,
        REM_CANCEL: rem_handler,
        REM_SKIP: rem_handler,
        REM_FAIL: rem_handler,
        ACTION_PUSH: bark_handler,
    }
    resolver = ctx.resolver
    run_meta = {"dry_run": dry_run, "full": ctx.full, "source_id": ctx.source_id}

    for message in batch.messages:
        lifecycle_path = ctx.lifecycle_log_path or settings.lifecycle_log_path
        trace = MailTrace(
            lifecycle_path=lifecycle_path, mail=message, run=run_meta
        )
        if (not dry_run) and store.already_processed(
            message.source.message_id, ctx.source_id
        ):
            result.skipped += 1
            _finish_apply(
                trace,
                result="skipped_duplicate",
                status="skipped_duplicate",
                summary="该邮件已处理过，跳过",
            )
            continue

        resolve_result = resolver.resolve(message, trace=trace)  # type: ignore[call-arg]

        if isinstance(resolve_result, IgnoredMail):
            result.ignored_count += 1
            continue
        if isinstance(resolve_result, ResolveFailure):
            result.failed_count += 1
            result.failed.append(resolve_result.error)
            if not trace.finished:
                trace.finish("failed", resolve_result.error)
            continue

        assert isinstance(resolve_result, ResolvedMail)
        event = resolved_to_candidate(resolve_result)
        result.resolved_count += 1
        result.matched += 1
        result.events.append(event.to_dict())

        requests = (
            planner.plan(resolve_result)
            + rem_planner.plan(resolve_result)
            + bark_planner.plan(resolve_result)
        )
        if not requests:
            result.ignored_count += 1
            continue
        req = requests[0]
        payload = req.payload
        is_reminder = req.type.startswith("apple_reminders.")
        noun = "提醒事项" if is_reminder else "日程"

        if dry_run:
            _print_event_details(event)
            _tally_dry_run(result, str(payload.get("result")), payload.get("summary"))
            planned = planned_event_brief(event)
            result.dry_run_reports.append(
                {
                    "apply": payload.get("result"),
                    "match_via": payload.get("match_via"),
                    "event": event.to_dict(),
                }
            )
            if payload.get("result") == "would_create":
                if is_reminder:
                    session.append(rem_session_event_from_candidate(event))
                else:
                    session.append(session_event_from_candidate(event))
            elif (
                payload.get("result") == "would_update"
                and payload.get("match_via") == "session"
            ):
                hit = (
                    rem_match_session(event, session)
                    if is_reminder
                    else match_session(event, session)
                )
                if hit:
                    hit.start_at = event.start_at
                    hit.end_at = event.end_at
                    hit.title = event.title
            trace.finish_dry_run(
                str(payload.get("summary") or ""),
                result=str(payload.get("result")),
                match_via=str(payload.get("match_via") or "none"),
                event_row_id=payload.get("event_row_id"),
                planned_event=planned,
                error=payload.get("error"),
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

        receipt = handlers[req.type].handle(req)
        result.action_count += 1
        meta = payload.get("_receipt_meta") or {}
        match_via = str(payload.get("match_via") or "none")
        summary = str(payload.get("summary") or "")
        event_row_id = meta.get("event_row_id") or payload.get("event_row_id")
        sinks = meta.get("sinks")

        if receipt.status == "failed" or req.type in FAIL_TYPES:
            result.failed_count += 1
            err = receipt.error or summary
            result.failed.append(err)
            print(f"  - 失败: {err}" if receipt.error else f"  - {summary}")
            _finish_apply(
                trace,
                result="failed",
                status="failed",
                summary=summary or f"写入失败：{err}",
                match_via=match_via,
                error=receipt.error or payload.get("error"),
            )
            continue

        if req.type in SKIP_TYPES:
            result.skipped += 1
            print(f"  - {summary}")
            status = (
                "skipped_duplicate"
                if payload.get("result") == "skipped_duplicate"
                else "skipped_same"
            )
            target_sinks = (payload.get("target") or {}).get("sinks")
            _finish_apply(
                trace,
                result=str(payload.get("result")),
                status=status,
                summary=summary,
                match_via=match_via,
                event_row_id=event_row_id,
                sinks=sinks or target_sinks,
            )
            continue

        if req.type in CREATE_TYPES:
            result.created += 1
            row_id = int(receipt.external_id) if receipt.external_id else 0
            created = store.get_event(row_id) if row_id else None
            if created:
                session.append(created)
            if "未找到" in summary:
                print(f"  - 未找到旧{noun}，已新建")
            else:
                print("  - 已创建")
            _finish_applied(
                trace,
                store,
                result="created",
                summary=f"已新建{noun} #{row_id}"
                if "新建" in summary or summary in ("新建日程", "新建提醒事项")
                else summary,
                match_via=match_via,
                event_row_id=row_id,
            )
        elif req.type in UPDATE_TYPES:
            result.updated += 1
            row_id = int(receipt.external_id) if receipt.external_id else 0
            print(f"  - 已更新{noun} #{row_id}")
            _finish_applied(
                trace,
                store,
                result="updated",
                summary=summary if "更新" in summary else f"已更新{noun} #{row_id}",
                match_via=match_via,
                event_row_id=row_id,
            )
        elif req.type in CANCEL_TYPES:
            result.cancelled += 1
            target = payload.get("target") or {}
            tid = int(target.get("id") or receipt.external_id or 0)
            if tid > 0:
                session[:] = [s for s in session if s.id != tid]
            else:
                smid = target.get("source_message_id")
                session[:] = [
                    s for s in session if s.source_message_id != smid
                ]
            print(f"  - 已删除旧{noun} #{tid}")
            _finish_applied(
                trace,
                store,
                result="cancelled",
                summary=f"已取消{noun} #{tid}",
                match_via=match_via,
                event_row_id=tid,
                sinks=target.get("sinks"),
            )

    if not dry_run and batch.next_checkpoint:
        prev = store.get_checkpoint(ctx.source_id)
        try:
            new_uid = int(batch.next_checkpoint)
            prev_uid = int(prev) if prev else 0
            if new_uid >= prev_uid:
                store.set_checkpoint(ctx.source_id, batch.next_checkpoint)
                print(
                    f"\n游标已更新：{ctx.source_id} checkpoint={batch.next_checkpoint}"
                )
        except ValueError:
            store.set_checkpoint(ctx.source_id, batch.next_checkpoint)
            print(
                f"\n游标已更新：{ctx.source_id} checkpoint={batch.next_checkpoint}"
            )

    return result


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
    if getattr(event, "time_precision", "") == "window":
        lines.append("  渠道=提醒事项（窗口内完成）")
    if event.meeting_url:
        lines.append(f"  会议={event.meeting_url}")
    if desc:
        lines.append(f"  描述={desc}")
    lines.append(f"  置信度={event.confidence:.2f}  主题={event.subject[:60]}")
    print("\n".join(lines))


def _tally_dry_run(
    result: RunResult, plan_result: Optional[str], summary: Optional[str] = None
) -> None:
    if plan_result == "would_create":
        result.created += 1
    elif plan_result == "would_update":
        result.updated += 1
    elif plan_result == "would_cancel":
        result.cancelled += 1
    elif plan_result in ("would_skip_duplicate", "would_skip_same"):
        result.skipped += 1
    elif plan_result == "would_fail":
        result.failed.append(str(summary or "取消失败"))


def _finish_apply(
    trace: MailTrace,
    *,
    result: str,
    status: str,
    summary: str,
    match_via: str = "none",
    event_row_id: Optional[int] = None,
    sinks=None,
    error: Optional[str] = None,
) -> None:
    stage: dict = {
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
    store,
    *,
    result: str,
    summary: str,
    match_via: str,
    event_row_id: int,
    sinks=None,
) -> None:
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
