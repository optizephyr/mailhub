from __future__ import annotations

from dataclasses import dataclass, field

from mailhub.contracts.messages import SourceRef
from mailhub.plugins.caldav import CalDavClient, component_text, parse_component
from mailhub.store.sqlite import EventStore


@dataclass(frozen=True)
class ItemIdentityChange:
    event_row_id: int
    item_uid: str


@dataclass
class IdentityMigrationResult:
    changes: list[ItemIdentityChange] = field(default_factory=list)
    linked_messages: int = 0
    missing_resources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def migrate_identities(
    store: EventStore,
    client: CalDavClient,
    *,
    source_id: str,
    dry_run: bool,
) -> IdentityMigrationResult:
    result = IdentityMigrationResult()
    for row in store.list_events_with_sinks():
        item_uid = row.item_uid
        discovered: set[str] = set()
        if not item_uid:
            for sink, href in row.sinks.items():
                component = "VTODO" if sink == "reminders" else "VEVENT"
                try:
                    resource = client.get(href)
                    item = parse_component(resource.data, component)
                    uid = component_text(item, "UID").strip()
                except RuntimeError as exc:
                    result.errors.append(f"#{row.id} {sink}: {exc}")
                    continue
                if not uid:
                    result.missing_resources.append(f"#{row.id} {sink} {href}")
                    continue
                discovered.add(uid)

            if len(discovered) > 1:
                result.errors.append(
                    f"#{row.id} 的多个 CalDAV 落点 UID 不一致：{sorted(discovered)}"
                )
                continue
            if discovered:
                item_uid = next(iter(discovered))
                result.changes.append(ItemIdentityChange(row.id, item_uid))
                if not dry_run:
                    store.set_event_item_uid(row.id, item_uid)

        if row.source_message_id and not store.list_event_messages(row.id):
            result.linked_messages += 1
            if not dry_run:
                store.link_event_message(
                    row.id,
                    SourceRef(
                        source_id=source_id,
                        message_id=row.source_message_id,
                    ),
                    relation="legacy",
                )
    return result
