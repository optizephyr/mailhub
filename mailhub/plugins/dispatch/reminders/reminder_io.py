from __future__ import annotations

import uuid
from typing import Optional

from icalendar import Calendar

from mailhub.plugins.caldav import (
    CalDavClient,
    build_todo_ical,
    component_text,
    parse_component,
)
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent
from mailhub.runtime.config import Settings


def create_reminder(
    event: CandidateEvent,
    settings: Settings,
    client: Optional[CalDavClient] = None,
) -> str:
    client = client or CalDavClient(settings)
    collection = client.collection(settings.reminders_list, "VTODO")
    return client.put_new(collection, build_todo_ical(event, str(uuid.uuid4())))


def update_reminder(
    href: str,
    event: CandidateEvent,
    settings: Settings,
    client: Optional[CalDavClient] = None,
) -> None:
    client = client or CalDavClient(settings)
    current = client.get(href)
    item = parse_component(current.data, "VTODO")
    uid = component_text(item, "UID") or str(uuid.uuid4())
    client.put_existing(href, build_todo_ical(event, uid, existing=item))


def update_reminder_title(
    href: str,
    title: str,
    settings: Settings,
    client: Optional[CalDavClient] = None,
) -> None:
    client = client or CalDavClient(settings)
    current = client.get(href)
    calendar = Calendar.from_ical(current.data)
    for component in calendar.walk():
        if component.name == "VTODO":
            component["SUMMARY"] = title
            client.put_existing(href, calendar.to_ical().decode())
            return
    raise RuntimeError("CalDAV 资源中没有 VTODO")


def delete_reminder(
    href: str, settings: Settings, client: Optional[CalDavClient] = None
) -> None:
    (client or CalDavClient(settings)).delete(href)


def list_reminder_lists(settings: Settings) -> list[str]:
    return [
        collection.name
        for collection in CalDavClient(settings).collections()
        if "VTODO" in collection.components
    ]
