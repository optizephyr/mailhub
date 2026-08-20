from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from mailhub.plugins.caldav import (
    CalDavClient,
    build_event_ical,
    component_datetime,
    component_text,
    parse_component,
)
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent
from mailhub.runtime.config import Settings

from .match import extract_marker_message_id
from .types import CalendarEventRef


def create_calendar_event(
    event: CandidateEvent,
    settings: Settings,
    client: Optional[CalDavClient] = None,
) -> str:
    client = client or CalDavClient(settings)
    collection = client.collection(settings.calendar_name, "VEVENT")
    uid = str(uuid.uuid4())
    return client.put_new(
        collection, build_event_ical(event, uid, settings.reminder_minutes)
    )


def update_calendar_event(
    href: str,
    event: CandidateEvent,
    settings: Settings,
    client: Optional[CalDavClient] = None,
) -> None:
    client = client or CalDavClient(settings)
    current = client.get(href)
    item = parse_component(current.data, "VEVENT")
    uid = component_text(item, "UID") or str(uuid.uuid4())
    client.put_existing(
        href, build_event_ical(event, uid, settings.reminder_minutes)
    )


def delete_calendar_event(
    href: str, settings: Settings, client: Optional[CalDavClient] = None
) -> None:
    (client or CalDavClient(settings)).delete(href)


def list_calendar_events(
    settings: Settings,
    window_start: datetime,
    window_end: datetime,
    client: Optional[CalDavClient] = None,
) -> list[CalendarEventRef]:
    client = client or CalDavClient(settings)
    collection = client.collection(settings.calendar_name, "VEVENT")
    resources = client.query(collection, "VEVENT", window_start, window_end)
    events: list[CalendarEventRef] = []
    for resource in resources:
        item = parse_component(resource.data, "VEVENT")
        start_at = component_datetime(item, "DTSTART")
        end_at = component_datetime(item, "DTEND")
        description = component_text(item, "DESCRIPTION")
        events.append(
            CalendarEventRef(
                uid=resource.href,
                summary=component_text(item, "SUMMARY"),
                start_at=start_at,
                end_at=end_at,
                marker_message_id=extract_marker_message_id(description),
            )
        )
    return events


def list_calendars(settings: Settings) -> list[str]:
    return [
        collection.name
        for collection in CalDavClient(settings).collections()
        if "VEVENT" in collection.components
    ]
