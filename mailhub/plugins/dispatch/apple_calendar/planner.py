from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from mailhub.contracts.actions import ActionRequest
from mailhub.contracts.resolve import ResolvedMail
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent
from mailhub.runtime.config import Settings
from mailhub.store.sqlite import EventStore, StoredEvent

from .calendar_io import list_apple_events
from .match import companies_match, match_calendar_event
from .types import AppleEventRef


SINK_REMINDERS = "reminders"


def _is_reminders_only(row: StoredEvent) -> bool:
    return bool(row.sinks.get(SINK_REMINDERS)) and not row.sinks.get("apple")


def _resolved_to_candidate(resolved: ResolvedMail) -> CandidateEvent:
    from mailhub.plugins.policies.qiuzhao import resolved_to_candidate

    return resolved_to_candidate(resolved)

ACTION_CREATE = "apple_calendar.create"
ACTION_UPDATE = "apple_calendar.update"
ACTION_CANCEL = "apple_calendar.cancel"
ACTION_SKIP = "apple_calendar.skip"
ACTION_FAIL = "apple_calendar.fail"


def _session_event_from_candidate(event: CandidateEvent) -> StoredEvent:
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
    for candidate in reversed(session):
        if _is_reminders_only(candidate):
            continue
        if want_type and candidate.event_type not in ("", "other", want_type):
            continue
        if companies_match(company, candidate.company):
            return candidate
    return None


def _scan_window(days: int) -> tuple[datetime, datetime]:
    now = datetime.now()
    return now - timedelta(days=1), now + timedelta(days=days)


def _peek_calendar_match(
    event: CandidateEvent, settings: Settings
) -> Optional[AppleEventRef]:
    if settings.calendar_scan_days <= 0:
        return None
    start, end = _scan_window(settings.calendar_scan_days)
    try:
        existing = list_apple_events(settings.apple_calendar_name, start, end)
    except RuntimeError:
        return None
    return match_calendar_event(event, existing)


def _adopt_from_calendar(
    store: EventStore,
    event: CandidateEvent,
    settings: Settings,
) -> Optional[StoredEvent]:
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
    return store.get_event(row_id)


def _virtual_from_calendar(matched: AppleEventRef, event: CandidateEvent) -> StoredEvent:
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


def find_target(
    store: EventStore,
    event: CandidateEvent,
    settings: Settings,
    *,
    adopt: bool = True,
    session: Optional[list[StoredEvent]] = None,
) -> tuple[Optional[StoredEvent], str]:
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


class AppleCalendarPlanner:
    def __init__(
        self,
        store: EventStore,
        settings: Settings,
        session: list[StoredEvent],
        *,
        dry_run: bool = False,
        source_id: str = "",
    ) -> None:
        self.store = store
        self.settings = settings
        self.session = session
        self.dry_run = dry_run
        self.source_id = source_id

    def plan(self, resolved: ResolvedMail) -> list[ActionRequest]:
        if resolved.kind == "schedule_invite":
            return []
        event = _resolved_to_candidate(resolved)
        if event.action != "cancel" and event.time_precision == "window":
            return []
        mid = event.message_id

        if self.store.already_processed(mid, self.source_id):
            return [
                self._req(
                    ACTION_SKIP,
                    event,
                    result="would_skip_duplicate" if self.dry_run else "skipped_duplicate",
                    summary="该邮件已处理过，将跳过" if self.dry_run else "该邮件已处理过，跳过",
                    match_via="none",
                )
            ]

        target, via = find_target(
            self.store,
            event,
            self.settings,
            adopt=not self.dry_run,
            session=self.session,
        )
        if target and _is_reminders_only(target):
            return []

        row_id = target.id if target and target.id > 0 else None

        if event.action == "cancel":
            if not target:
                return [
                    self._req(
                        ACTION_FAIL,
                        event,
                        result="would_fail" if self.dry_run else "failed",
                        summary="取消失败：未找到可取消的旧日程",
                        match_via=via,
                        error="no matching active event",
                    )
                ]
            return [
                self._req(
                    ACTION_CANCEL,
                    event,
                    result="would_cancel" if self.dry_run else "cancel",
                    summary="将取消匹配到的旧日程" if self.dry_run else "取消日程",
                    match_via=via,
                    event_row_id=row_id,
                    target=target,
                )
            ]

        if event.action == "reschedule":
            if target:
                return [
                    self._req(
                        ACTION_UPDATE,
                        event,
                        result="would_update" if self.dry_run else "update",
                        summary="将改期并更新匹配到的旧日程" if self.dry_run else "改期更新",
                        match_via=via,
                        event_row_id=row_id,
                        target=target,
                    )
                ]
            return [
                self._req(
                    ACTION_CREATE,
                    event,
                    result="would_create" if self.dry_run else "create",
                    summary="改期未匹配到旧日程，将新建" if self.dry_run else "改期新建",
                    match_via=via,
                )
            ]

        # create
        if target and target.start_at and target.start_at != event.start_at:
            return [
                self._req(
                    ACTION_UPDATE,
                    event,
                    result="would_update" if self.dry_run else "update",
                    summary="检测到时间变化，将更新匹配到的旧日程"
                    if self.dry_run
                    else "时间变化更新",
                    match_via=via,
                    event_row_id=row_id,
                    target=target,
                )
            ]
        if target and target.start_at == event.start_at:
            return [
                self._req(
                    ACTION_SKIP,
                    event,
                    result="would_skip_same" if self.dry_run else "skipped_same",
                    summary="已存在相同时间日程，将跳过" if self.dry_run else "相同时间跳过",
                    match_via=via,
                    event_row_id=row_id,
                    target=target,
                )
            ]
        return [
            self._req(
                ACTION_CREATE,
                event,
                result="would_create" if self.dry_run else "create",
                summary="将新建日程" if self.dry_run else "新建日程",
                match_via=via,
            )
        ]

    def _req(
        self,
        action_type: str,
        event: CandidateEvent,
        *,
        result: str,
        summary: str,
        match_via: str,
        event_row_id: Optional[int] = None,
        target: Optional[StoredEvent] = None,
        error: Optional[str] = None,
    ) -> ActionRequest:
        action_id = str(uuid.uuid4())
        payload = {
            "result": result,
            "summary": summary,
            "match_via": match_via,
            "event_row_id": event_row_id,
            "error": error,
            "candidate": event.to_dict(),
            "dry_run": self.dry_run,
            "source_id": self.source_id,
        }
        if target is not None:
            payload["target"] = {
                "id": target.id,
                "company": target.company,
                "event_type": target.event_type,
                "title": target.title,
                "start_at": target.start_at,
                "end_at": target.end_at,
                "status": target.status,
                "source_message_id": target.source_message_id,
                "sinks": dict(target.sinks),
            }
        return ActionRequest(
            id=action_id,
            type=action_type,
            idempotency_key=f"{self.source_id}:{event.message_id}:{action_type}:{result}",
            payload=payload,
        )


# re-export helpers for session updates in engine
session_event_from_candidate = _session_event_from_candidate
match_session = _match_session
