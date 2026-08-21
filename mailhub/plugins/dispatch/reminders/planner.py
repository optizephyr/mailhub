from __future__ import annotations

import uuid
from typing import Optional

from mailhub.contracts.actions import ActionRequest
from mailhub.contracts.resolve import ResolvedMail
from mailhub.plugins.dispatch.calendar.match import companies_match
from mailhub.plugins.dispatch.mail_version import is_stale_mail
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent
from mailhub.runtime.config import Settings
from mailhub.store.sqlite import EventStore, StoredEvent

SINK_REMINDERS = "reminders"

ACTION_CREATE = "reminders.create"
ACTION_UPDATE = "reminders.update"
ACTION_CANCEL = "reminders.cancel"
ACTION_SKIP = "reminders.skip"
ACTION_FAIL = "reminders.fail"


def _resolved_to_candidate(resolved: ResolvedMail) -> CandidateEvent:
    from mailhub.plugins.policies.qiuzhao import resolved_to_candidate

    return resolved_to_candidate(resolved)


def _is_reminders_row(row: StoredEvent) -> bool:
    return bool(row.sinks.get(SINK_REMINDERS))


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
        sinks={SINK_REMINDERS: ""},
        last_mail_sent_at=event.sent_at,
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
        if not _is_reminders_row(candidate):
            continue
        if want_type and candidate.event_type not in ("", "other", want_type):
            continue
        if companies_match(company, candidate.company):
            return candidate
    return None


def find_target(
    store: EventStore,
    event: CandidateEvent,
    session: Optional[list[StoredEvent]] = None,
) -> tuple[Optional[StoredEvent], str]:
    refs = [r for r in event.references if r]
    if refs:
        target = store.find_active_event(references=refs)
        if target and _is_reminders_row(target):
            return target, "references"

    session_hit = _match_session(event, session or [])
    if session_hit:
        return session_hit, "session"

    target = store.find_active_event(
        company=event.company,
        event_type=event.event_type if event.event_type != "other" else "",
    )
    if target and _is_reminders_row(target):
        return target, "company_type"
    return None, "none"


def _same_window(event: CandidateEvent, target: StoredEvent) -> bool:
    if event.start_at or event.end_at:
        return target.start_at == event.start_at and target.end_at == event.end_at
    return True


class RemindersPlanner:
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
        if event.action != "cancel" and event.time_precision != "window":
            return []

        if not self.settings.reminders_list:
            return [
                self._req(
                    ACTION_SKIP,
                    event,
                    result="would_skip_disabled"
                    if self.dry_run
                    else "skipped_disabled",
                    summary="提醒事项未启用，本邮件不送达",
                    match_via="none",
                )
            ]

        if self.store.already_processed(event.message_id, self.source_id):
            return [
                self._req(
                    ACTION_SKIP,
                    event,
                    result="would_skip_duplicate" if self.dry_run else "skipped_duplicate",
                    summary="该邮件已处理过，将跳过" if self.dry_run else "该邮件已处理过，跳过",
                    match_via="none",
                )
            ]

        target, via = find_target(self.store, event, session=self.session)
        if event.action == "cancel" and event.time_precision != "window" and not target:
            return []

        row_id = target.id if target and target.id > 0 else None

        if target and is_stale_mail(event.sent_at, target.last_mail_sent_at):
            return [
                self._req(
                    ACTION_SKIP,
                    event,
                    result="would_skip_older" if self.dry_run else "skipped_older",
                    summary="更早的邮件，不覆盖已有提醒事项",
                    match_via=via,
                    event_row_id=row_id,
                    target=target,
                )
            ]

        if event.action == "cancel":
            if not target:
                return [
                    self._req(
                        ACTION_FAIL,
                        event,
                        result="would_fail" if self.dry_run else "failed",
                        summary="取消失败：未找到可取消的提醒事项",
                        match_via=via,
                        error="no matching active reminder",
                    )
                ]
            return [
                self._req(
                    ACTION_CANCEL,
                    event,
                    result="would_cancel" if self.dry_run else "cancel",
                    summary="将取消匹配到的提醒事项" if self.dry_run else "取消提醒事项",
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
                        summary="将更新匹配到的提醒事项" if self.dry_run else "更新提醒事项",
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
                    summary="改期未匹配到旧提醒，将新建" if self.dry_run else "改期新建提醒事项",
                    match_via=via,
                )
            ]

        if target and _same_window(event, target):
            return [
                self._req(
                    ACTION_SKIP,
                    event,
                    result="would_skip_same" if self.dry_run else "skipped_same",
                    summary="已存在相同窗口提醒，将跳过" if self.dry_run else "相同窗口跳过",
                    match_via=via,
                    event_row_id=row_id,
                    target=target,
                )
            ]
        if target:
            return [
                self._req(
                    ACTION_UPDATE,
                    event,
                    result="would_update" if self.dry_run else "update",
                    summary="检测到窗口变化，将更新提醒事项"
                    if self.dry_run
                    else "窗口变化更新",
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
                summary="将新建提醒事项" if self.dry_run else "新建提醒事项",
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
            "channel": "reminders",
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
                "item_uid": target.item_uid,
                "source_message_id": target.source_message_id,
                "last_mail_sent_at": target.last_mail_sent_at,
                "sinks": dict(target.sinks),
            }
        return ActionRequest(
            id=action_id,
            type=action_type,
            idempotency_key=f"{self.source_id}:{event.message_id}:{action_type}:{result}",
            payload=payload,
        )


session_event_from_candidate = _session_event_from_candidate
match_session = _match_session
