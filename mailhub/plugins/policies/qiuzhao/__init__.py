from __future__ import annotations

from typing import Optional

from mailhub.contracts.messages import MailMessage
from mailhub.contracts.resolve import (
    IgnoredMail,
    ResolveFailure,
    ResolvedMail,
    ResolveResult,
    TimeConstraint,
)
from mailhub.logging.lifecycle import MailTrace
from mailhub.runtime.config import Settings

from . import parser as parser_mod
from .types import CandidateEvent, MailItem


def mail_message_to_item(message: MailMessage) -> MailItem:
    return MailItem(
        message_id=message.source.message_id,
        subject=message.subject,
        from_=message.sender,
        date=message.sent_at,
        text=message.text,
        html=message.html,
        uid=0,
        references=list(message.references),
    )


def candidate_to_resolved(event: CandidateEvent, message: MailMessage) -> ResolvedMail:
    change_map = {
        "create": "new",
        "reschedule": "updated",
        "cancel": "cancelled",
    }
    precision = "fixed"
    if not event.start_at:
        precision = "unknown"
    time = None
    if event.start_at or event.end_at:
        time = TimeConstraint(
            start_at=event.start_at or None,
            end_at=event.end_at or None,
            timezone="Asia/Shanghai",
            precision=precision,
        )
    links = [event.meeting_url] if event.meeting_url else []
    return ResolvedMail(
        source=message.source,
        kind=event.event_type or "other",
        change=change_map.get(event.action, "unknown"),
        title=event.title,
        summary=event.subject,
        importance="high",
        time=time,
        location=event.location or None,
        links=links,
        correlation_key=f"{event.company}|{event.event_type}",
        attributes={
            "company": event.company,
            "action": event.action,
            "event_type": event.event_type,
            "meeting_url": event.meeting_url,
            "description": event.description,
            "references": list(event.references),
            "subject": event.subject,
            "source_snippet": event.source_snippet,
            "candidate": event.to_dict(),
        },
        confidence=event.confidence,
    )


def resolved_to_candidate(resolved: ResolvedMail) -> CandidateEvent:
    raw = resolved.attributes.get("candidate")
    if isinstance(raw, dict):
        return CandidateEvent(**{k: raw[k] for k in CandidateEvent.__dataclass_fields__ if k in raw})
    action_map = {"new": "create", "updated": "reschedule", "cancelled": "cancel"}
    return CandidateEvent(
        message_id=resolved.source.message_id,
        subject=str(resolved.attributes.get("subject") or resolved.summary),
        title=resolved.title,
        event_type=resolved.kind,
        action=action_map.get(resolved.change, "create"),
        start_at=(resolved.time.start_at if resolved.time and resolved.time.start_at else "")
        or "",
        end_at=(resolved.time.end_at if resolved.time and resolved.time.end_at else "") or "",
        location=resolved.location or "",
        company=str(resolved.attributes.get("company") or ""),
        description=str(resolved.attributes.get("description") or ""),
        meeting_url=(resolved.links[0] if resolved.links else "")
        or str(resolved.attributes.get("meeting_url") or ""),
        confidence=resolved.confidence,
        references=list(resolved.attributes.get("references") or []),
    )


class QiuzhaoResolver:
    """Autumn-recruit importance policy + parse → ResolveResult."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def resolve(
        self,
        message: MailMessage,
        *,
        trace: Optional[MailTrace] = None,
    ) -> ResolveResult:
        item = mail_message_to_item(message)
        try:
            event = parser_mod.parse_mail(item, self.settings, trace=trace)
        except Exception as exc:
            return ResolveFailure(source=message.source, error=str(exc))
        if event is None:
            # coarse/parse already logged on trace
            reason = "rejected_by_policy"
            if trace and trace.stages:
                last = trace.stages[-1]
                reason = str(last.get("reason") or last.get("result") or reason)
            return IgnoredMail(source=message.source, reason=reason)
        return candidate_to_resolved(event, message)
