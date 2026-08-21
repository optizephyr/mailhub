from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Optional

from mailhub.contracts.messages import SourceRef
from mailhub.plugins.caldav import (
    CalDavClient,
    component_datetime,
    component_text,
    parse_component,
)
from mailhub.plugins.dispatch.calendar.match import companies_match
from mailhub.plugins.dispatch.reminders.planner import SINK_REMINDERS
from mailhub.store.sqlite import EventStore, StoredEvent


@dataclass(frozen=True)
class ItemIdentityChange:
    event_row_id: int
    item_uid: str


@dataclass(frozen=True)
class AdoptedSink:
    event_row_id: int
    sink: str
    href: str
    item_uid: str
    match_via: str


@dataclass(frozen=True)
class _RemoteTodo:
    href: str
    item_uid: str
    summary: str
    due: str
    message_id: str


@dataclass
class IdentityMigrationResult:
    changes: list[ItemIdentityChange] = field(default_factory=list)
    adopted_sinks: list[AdoptedSink] = field(default_factory=list)
    linked_messages: int = 0
    missing_resources: list[str] = field(default_factory=list)
    ambiguous_matches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def migrate_identities(
    store: EventStore,
    client: CalDavClient,
    *,
    source_id: str,
    dry_run: bool,
    reminders_list: str = "",
) -> IdentityMigrationResult:
    result = IdentityMigrationResult()
    active_rows = store.list_active_events()
    for row in (item for item in active_rows if item.sinks):
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

    adopted_row_ids: set[int] = set()
    if reminders_list:
        adopted_row_ids = _adopt_orphan_reminders(
            store,
            client,
            active_rows,
            reminders_list=reminders_list,
            dry_run=dry_run,
            result=result,
        )

    for row in active_rows:
        has_sink = bool(row.sinks) or row.id in adopted_row_ids
        if (
            has_sink
            and row.source_message_id
            and not store.list_event_messages(row.id)
        ):
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


def _adopt_orphan_reminders(
    store: EventStore,
    client: CalDavClient,
    active_rows: Iterable[StoredEvent],
    *,
    reminders_list: str,
    dry_run: bool,
    result: IdentityMigrationResult,
) -> set[int]:
    active_rows = list(active_rows)
    try:
        collection = client.collection(reminders_list, "VTODO")
        resources = client.query(collection, "VTODO")
    except RuntimeError as exc:
        result.errors.append(f"扫描提醒事项失败：{exc}")
        return set()

    occupied_hrefs = store.list_sink_external_ids(SINK_REMINDERS)
    remote_items: list[_RemoteTodo] = []
    for resource in resources:
        if resource.href in occupied_hrefs:
            continue
        try:
            item = parse_component(resource.data, "VTODO")
            item_uid = component_text(item, "UID").strip()
        except RuntimeError as exc:
            result.errors.append(f"{resource.href}: {exc}")
            continue
        if not item_uid:
            result.missing_resources.append(f"VTODO {resource.href}")
            continue
        remote_items.append(
            _RemoteTodo(
                href=resource.href,
                item_uid=item_uid,
                summary=component_text(item, "SUMMARY").strip(),
                due=component_datetime(item, "DUE"),
                message_id=component_text(item, "X-MAILHUB-MESSAGE-ID").strip(),
            )
        )

    candidates: dict[int, list[tuple[_RemoteTodo, str]]] = {}
    for row in active_rows:
        if row.sinks.get(SINK_REMINDERS):
            continue
        matches = [
            match
            for remote in remote_items
            if (match := _match_orphan(row, remote)) is not None
        ]
        if matches:
            candidates[row.id] = matches

    unique_by_row: dict[int, tuple[_RemoteTodo, str]] = {}
    for row_id, matches in candidates.items():
        best_rank = max(_match_rank(via) for _, via in matches)
        best = [match for match in matches if _match_rank(match[1]) == best_rank]
        if len(best) != 1:
            hrefs = ", ".join(sorted(remote.href for remote, _ in best))
            result.ambiguous_matches.append(f"#{row_id} 匹配到多个 VTODO：{hrefs}")
            continue
        unique_by_row[row_id] = best[0]

    rows_by_href: dict[str, list[int]] = {}
    for row_id, (remote, _) in unique_by_row.items():
        rows_by_href.setdefault(remote.href, []).append(row_id)

    rows_by_id = {row.id: row for row in active_rows}
    adopted: set[int] = set()
    for row_id, (remote, match_via) in unique_by_row.items():
        competing_rows = rows_by_href[remote.href]
        if len(competing_rows) > 1:
            if row_id == min(competing_rows):
                ids = ", ".join(f"#{value}" for value in sorted(competing_rows))
                result.ambiguous_matches.append(
                    f"{remote.href} 同时匹配 {ids}"
                )
            continue
        row = rows_by_id[row_id]
        if row.item_uid and row.item_uid != remote.item_uid:
            result.errors.append(
                f"#{row_id} 已有 item_uid={row.item_uid}，"
                f"拒绝认领 UID={remote.item_uid}"
            )
            continue
        change = AdoptedSink(
            event_row_id=row_id,
            sink=SINK_REMINDERS,
            href=remote.href,
            item_uid=remote.item_uid,
            match_via=match_via,
        )
        if not dry_run:
            try:
                store.adopt_event_sink(
                    row_id,
                    sink=SINK_REMINDERS,
                    external_id=remote.href,
                    item_uid=remote.item_uid,
                )
            except ValueError as exc:
                result.errors.append(f"#{row_id} 认领失败：{exc}")
                continue
        result.adopted_sinks.append(change)
        if not row.item_uid:
            result.changes.append(ItemIdentityChange(row_id, remote.item_uid))
        adopted.add(row_id)
    return adopted


def _match_orphan(
    row: StoredEvent, remote: _RemoteTodo
) -> Optional[tuple[_RemoteTodo, str]]:
    if row.item_uid:
        return (remote, "item_uid") if row.item_uid == remote.item_uid else None
    if remote.message_id:
        return (
            (remote, "message_id")
            if row.source_message_id == remote.message_id
            else None
        )
    local_due = row.end_at or row.start_at
    if (
        companies_match(row.company, remote.summary)
        and _same_due(local_due, remote.due)
    ):
        return remote, "company_due"
    return None


def _match_rank(match_via: str) -> int:
    return {"company_due": 1, "message_id": 2, "item_uid": 3}[match_via]


def _same_due(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        left_value = datetime.fromisoformat(left)
        right_value = datetime.fromisoformat(right)
    except ValueError:
        try:
            return date.fromisoformat(left) == date.fromisoformat(right)
        except ValueError:
            return False
    if left_value.tzinfo:
        left_value = left_value.replace(tzinfo=None)
    if right_value.tzinfo:
        right_value = right_value.replace(tzinfo=None)
    return abs((left_value - right_value).total_seconds()) <= 60
