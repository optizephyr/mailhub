from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from mailhub.contracts.messages import MailMessage, SourceRef
from mailhub.plugins.caldav import CalDavClient
from mailhub.plugins.policies.qiuzhao import mail_message_to_item
from mailhub.plugins.policies.qiuzhao.parser import (
    build_reminder_title,
    extract_task_duration_minutes,
    llm_parse,
)
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
    message_fetcher: Optional[
        Callable[[Iterable[str]], list[MailMessage]]
    ] = None,
    source_ref_fetcher: Optional[
        Callable[[Iterable[SourceRef]], list[MailMessage]]
    ] = None,
    missing_message_ids: Optional[list[str]] = None,
) -> list[ReminderTitleChange]:
    changes: list[ReminderTitleChange] = []
    rows = store.list_active_events_for_sink(SINK_REMINDERS)
    messages: dict[str, MailMessage] = {}
    refs_by_row: dict[int, list[SourceRef]] = {}
    if source_ref_fetcher is not None:
        for row in rows:
            refs = store.list_event_messages(row.id)
            if not refs and row.source_message_id:
                refs = [
                    SourceRef(
                        source_id=settings.source_id,
                        message_id=row.source_message_id,
                    )
                ]
            refs_by_row[row.id] = refs
        fetched = source_ref_fetcher(
            ref for refs in refs_by_row.values() for ref in refs
        )
        messages = {message.source.message_id: message for message in fetched}
    elif message_fetcher is not None:
        fetched = message_fetcher(row.source_message_id for row in rows)
        messages = {message.source.message_id: message for message in fetched}

    for row in rows:
        task_duration_minutes = None
        if source_ref_fetcher is not None or message_fetcher is not None:
            wanted_ids = [row.source_message_id]
            wanted_ids.extend(ref.message_id for ref in refs_by_row.get(row.id, []))
            message = next(
                (messages[mid] for mid in wanted_ids if mid in messages),
                None,
            )
            if message is None:
                if missing_message_ids is not None:
                    missing_message_ids.append(row.source_message_id)
                continue
            item = mail_message_to_item(message)
            task_duration_minutes = extract_task_duration_minutes(
                f"{item.subject}\n{item.body}"
            )
            if task_duration_minutes is None and settings.llm_enabled:
                llm_result = llm_parse(item, settings)
                if llm_result.event is not None:
                    task_duration_minutes = (
                        llm_result.event.task_duration_minutes
                    )
        new_title = build_reminder_title(
            row.event_type,
            row.company,
            "create",
            row.start_at,
            row.end_at,
            row.title,
            task_duration_minutes,
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
