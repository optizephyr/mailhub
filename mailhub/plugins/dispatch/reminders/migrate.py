from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mailhub.plugins.caldav import CalDavClient
from mailhub.plugins.policies.qiuzhao.parser import build_reminder_title
from mailhub.runtime.config import Settings
from mailhub.store.sqlite import EventStore

from .planner import SINK_REMINDERS
from .reminder_io import update_reminder_title


@dataclass(frozen=True)
class ReminderTitleChange:
    event_row_id: int
    old_title: str
    new_title: str


def migrate_reminder_titles(
    store: EventStore,
    settings: Settings,
    *,
    dry_run: bool,
    client: Optional[CalDavClient] = None,
) -> list[ReminderTitleChange]:
    changes: list[ReminderTitleChange] = []
    for row in store.list_active_events_for_sink(SINK_REMINDERS):
        new_title = build_reminder_title(
            row.event_type,
            row.company,
            "create",
            row.start_at,
            row.end_at,
            row.title,
        )
        if new_title == row.title:
            continue

        change = ReminderTitleChange(row.id, row.title, new_title)
        changes.append(change)
        if dry_run:
            continue

        update_reminder_title(
            row.sinks[SINK_REMINDERS],
            new_title,
            settings,
            client,
        )
        store.update_event_title(row.id, new_title)
    return changes
