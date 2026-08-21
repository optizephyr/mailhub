from __future__ import annotations

import argparse
import base64
import html
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import mailhub.cli.main as cli
import pytest
import requests

from mailhub.contracts.messages import IngestBatch
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent, MailItem
from mailhub.runtime.config import Settings
from tests.test_sync_lifecycle import _interview_mail, _to_message
from tests.test_bark_dispatch import _message_from_fixture


class _CalDavState:
    def __init__(self) -> None:
        self.resources: dict[str, str] = {}
        self.etags: dict[str, str] = {}
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[tuple[str, str, str]] = []
        self.fail_puts = 0
        self.conflict_puts = 0
        self.duplicate_calendar = False


@pytest.fixture
def caldav_server() -> Iterator[tuple[str, _CalDavState]]:
    state = _CalDavState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send_xml(self, body: str, status: int = 207) -> None:
            raw = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            expected = "Basic " + base64.b64encode(b"user:secret").decode()
            if self.headers.get("Authorization") == expected:
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="CalDAV"')
            self.end_headers()
            return False

        def do_PROPFIND(self) -> None:
            if not self._authorized():
                return
            state.requests.append(("PROPFIND", self.path))
            if self.path == "/":
                self._send_xml(
                    """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response><d:href>/</d:href><d:propstat><d:prop>
    <d:current-user-principal><d:href>/principals/user/</d:href></d:current-user-principal>
  </d:prop></d:propstat></d:response>
</d:multistatus>"""
                )
            elif self.path == "/principals/user/":
                self._send_xml(
                    """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response><d:href>/principals/user/</d:href><d:propstat><d:prop>
    <c:calendar-home-set><d:href>/calendars/user/</d:href></c:calendar-home-set>
  </d:prop></d:propstat></d:response>
</d:multistatus>"""
                )
            else:
                duplicate = (
                    """<d:response><d:href>/calendars/user/calendar-2/</d:href><d:propstat><d:prop>
    <d:displayname>秋招</d:displayname>
    <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
    <c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
  </d:prop></d:propstat></d:response>"""
                    if state.duplicate_calendar
                    else ""
                )
                self._send_xml(
                    """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:response><d:href>/calendars/user/calendar/</d:href><d:propstat><d:prop>
    <d:displayname>秋招</d:displayname>
    <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
    <c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
  </d:prop></d:propstat></d:response>
  <d:response><d:href>/calendars/user/tasks/</d:href><d:propstat><d:prop>
    <d:displayname>秋招提醒</d:displayname>
    <d:resourcetype><d:collection/><c:calendar/></d:resourcetype>
    <c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>
  </d:prop></d:propstat></d:response>"""
                    + duplicate
                    + "</d:multistatus>"
                )

        def do_REPORT(self) -> None:
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode()
            state.requests.append(("REPORT", self.path))
            state.bodies.append(("REPORT", self.path, body))
            responses = []
            for href, data in state.resources.items():
                if href.startswith(self.path.rstrip("/") + "/"):
                    etag = state.etags.get(href, '"v1"')
                    responses.append(
                        "<d:response>"
                        f"<d:href>{html.escape(href)}</d:href>"
                        "<d:propstat><d:prop>"
                        f"<d:getetag>{html.escape(etag)}</d:getetag>"
                        f"<c:calendar-data>{html.escape(data)}</c:calendar-data>"
                        "</d:prop></d:propstat></d:response>"
                    )
            self._send_xml(
                """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">"""
                + "".join(responses)
                + "</d:multistatus>"
            )

        def do_PUT(self) -> None:
            if not self._authorized():
                return
            if state.fail_puts > 0:
                state.fail_puts -= 1
                self.send_response(503)
                self.end_headers()
                return
            if state.conflict_puts > 0:
                state.conflict_puts -= 1
                self.send_response(412)
                self.end_headers()
                return
            current_etag = state.etags.get(self.path)
            if current_etag and self.headers.get("If-Match") != current_etag:
                self.send_response(412)
                self.end_headers()
                return
            if not current_etag and self.headers.get("If-None-Match") != "*":
                self.send_response(412)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode()
            state.resources[self.path] = body
            state.bodies.append(("PUT", self.path, body))
            version = int((current_etag or '"v0"').strip('"v')) + 1
            state.etags[self.path] = f'"v{version}"'
            state.requests.append(("PUT", self.path))
            self.send_response(201)
            self.send_header("ETag", state.etags[self.path])
            self.end_headers()

        def do_GET(self) -> None:
            if not self._authorized():
                return
            state.requests.append(("GET", self.path))
            if self.path not in state.resources:
                self.send_response(404)
                self.end_headers()
                return
            raw = state.resources[self.path].encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("ETag", state.etags.get(self.path, '"v1"'))
            self.end_headers()
            self.wfile.write(raw)

        def do_DELETE(self) -> None:
            if not self._authorized():
                return
            state.requests.append(("DELETE", self.path))
            if self.path not in state.resources:
                self.send_response(404)
                self.end_headers()
                return
            if self.headers.get("If-Match") != state.etags.get(self.path, '"v1"'):
                self.send_response(412)
                self.end_headers()
                return
            del state.resources[self.path]
            state.etags.pop(self.path, None)
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        thread.join()


def test_sync_creates_event_in_configured_caldav_calendar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server
) -> None:
    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    batch = IngestBatch(
        messages=[_to_message(_interview_mail())],
        next_checkpoint="99",
    )
    cli.cmd_sync._test_fetch = lambda _checkpoint: batch  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert len(state.resources) == 1
    resource = next(iter(state.resources.values()))
    assert "BEGIN:VEVENT" in resource
    assert "SUMMARY:[面试] 美团" in resource
    assert "DTSTART:20260825T100000" in resource
    assert "DTEND:20260825T110000" in resource
    assert "BEGIN:VALARM" in resource
    assert "X-MAILHUB-ITEM-ID:" in resource
    assert "X-MAILHUB-MESSAGE-ID:" in resource

    from mailhub.plugins.caldav import component_text, parse_component
    from mailhub.store.sqlite import EventStore

    uid = component_text(parse_component(resource, "VEVENT"), "UID")
    store = EventStore(tmp_path / "synced.sqlite")
    row = store.get_event(1)
    assert row is not None and row.item_uid == uid
    assert [ref.message_id for ref in store.list_event_messages(1)] == [
        _interview_mail().message_id
    ]
    store.close()


def test_sync_creates_todo_in_configured_caldav_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server
) -> None:
    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    mail = MailItem(
        message_id="<assessment@qq.com>",
        subject="【京东校招】测评通知",
        from_="campus@jd.com",
        date="Wed, 19 Aug 2026 03:00:00 +0800",
        text="建议您在48小时内完成测评。https://example.com/a",
        html="",
        uid=43,
    )
    batch = IngestBatch(messages=[_to_message(mail)], next_checkpoint="100")
    cli.cmd_sync._test_fetch = lambda _checkpoint: batch  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert len(state.resources) == 1
    resource = next(iter(state.resources.values()))
    assert "BEGIN:VTODO" in resource
    assert "SUMMARY:[测评] 京东" in resource
    assert "DUE:20260821T030000" in resource
    assert "URL:https://example.com/a" in resource


def test_sync_updates_then_deletes_existing_caldav_todo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server
) -> None:
    from mailhub.plugins.policies.qiuzhao import parser as parser_mod

    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    events = {
        "<todo-create@qq.com>": CandidateEvent(
            message_id="<todo-create@qq.com>",
            subject="测评通知",
            title="[测评] 京东",
            event_type="assessment",
            action="create",
            end_at="2026-08-21T03:00:00",
            company="京东",
            meeting_url="https://example.com/a",
            time_precision="window",
        ),
        "<todo-update@qq.com>": CandidateEvent(
            message_id="<todo-update@qq.com>",
            subject="测评延期通知",
            title="[测评] 京东",
            event_type="assessment",
            action="reschedule",
            end_at="2026-08-22T03:00:00",
            company="京东",
            meeting_url="https://example.com/a",
            time_precision="window",
            references=["<todo-create@qq.com>"],
        ),
        "<todo-cancel@qq.com>": CandidateEvent(
            message_id="<todo-cancel@qq.com>",
            subject="测评取消通知",
            title="[取消] 京东",
            event_type="assessment",
            action="cancel",
            company="京东",
            time_precision="window",
            references=["<todo-update@qq.com>"],
        ),
    }
    monkeypatch.setattr(
        parser_mod,
        "parse_mail",
        lambda mail, _settings, trace=None: events[mail.message_id],
    )
    mails = [
        MailItem(
            message_id=message_id,
            subject=event.subject,
            from_="campus@jd.com",
            date="Wed, 19 Aug 2026 03:00:00 +0800",
            text="校园招聘测评安排",
            html="",
            uid=uid,
            references=list(event.references),
        )
        for uid, (message_id, event) in enumerate(events.items(), start=43)
    ]
    batches = [
        IngestBatch(messages=[_to_message(mail)], next_checkpoint=str(mail.uid))
        for mail in mails
    ]
    cli.cmd_sync._test_fetch = lambda _checkpoint: batches.pop(0)  # type: ignore[attr-defined]
    try:
        for _ in range(3):
            cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    put_bodies = [body for method, _path, body in state.bodies if method == "PUT"]
    assert len(put_bodies) == 2
    assert "DUE:20260822T030000" in put_bodies[-1]
    assert any(method == "DELETE" for method, _path in state.requests)
    assert state.resources == {}


def test_update_reminder_preserves_user_completion_state(
    tmp_path: Path, caldav_server
) -> None:
    from mailhub.plugins.caldav import CalDavClient
    from mailhub.plugins.dispatch.reminders.reminder_io import (
        create_reminder,
        update_reminder,
    )

    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        reminders_list="秋招提醒",
    )
    client = CalDavClient(settings)
    original = CandidateEvent(
        message_id="<todo-complete@qq.com>",
        subject="测评通知",
        title="[测评] 京东",
        event_type="assessment",
        end_at="2026-08-21T03:00:00",
        company="京东",
        meeting_url="https://example.com/a",
        time_precision="window",
    )
    href = create_reminder(original, settings, client)
    resource_path = next(path for path in state.resources if path.endswith(".ics"))
    state.resources[resource_path] = (
        state.resources[resource_path]
        .replace("STATUS:NEEDS-ACTION", "STATUS:COMPLETED")
        .replace(
            "SUMMARY:[测评] 京东",
            "SUMMARY:[测评] 京东\r\n"
            "COMPLETED:20260821T010000Z\r\n"
            "PERCENT-COMPLETE:100",
        )
    )

    updated = CandidateEvent(
        message_id="<todo-complete-update@qq.com>",
        subject="测评延期通知",
        title="[测评] 京东",
        event_type="assessment",
        action="reschedule",
        end_at="2026-08-22T03:00:00",
        company="京东",
        meeting_url="https://example.com/a",
        time_precision="window",
    )
    update_reminder(href, updated, settings, client)

    resource = state.resources[resource_path]
    assert "DUE:20260822T030000" in resource
    assert "STATUS:COMPLETED" in resource
    assert "COMPLETED:20260821T010000Z" in resource
    assert "PERCENT-COMPLETE:100" in resource


def test_migrate_reminder_titles_updates_existing_resource_without_duplicate(
    tmp_path: Path, caldav_server
) -> None:
    from mailhub.plugins.caldav import CalDavClient
    from mailhub.plugins.dispatch.reminders.migrate import migrate_reminder_titles
    from mailhub.plugins.dispatch.reminders.reminder_io import create_reminder
    from mailhub.store.sqlite import EventStore

    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        reminders_list="秋招提醒",
    )
    client = CalDavClient(settings)
    event = CandidateEvent(
        message_id="<old-window@qq.com>",
        subject="测评通知",
        title="[测评] 京东",
        event_type="assessment",
        end_at="2026-08-21T18:00:00",
        company="京东",
        meeting_url="https://example.com/a",
        time_precision="window",
    )
    href = create_reminder(event, settings, client)
    resource_path = next(path for path in state.resources if path.endswith(".ics"))
    store = EventStore(tmp_path / "synced.sqlite")
    row_id = store.create_event(
        company=event.company,
        event_type=event.event_type,
        title=event.title,
        start_at=event.start_at,
        end_at=event.end_at,
        source_message_id=event.message_id,
        sinks={"reminders": href},
    )

    preview = migrate_reminder_titles(store, settings, dry_run=True, client=client)
    assert preview[0].new_title == "[测评] 京东 截止8月21日 18:00"
    assert "SUMMARY:[测评] 京东\r\n" in state.resources[resource_path]

    changes = migrate_reminder_titles(store, settings, dry_run=False, client=client)

    assert changes == preview
    assert len(state.resources) == 1
    resource = state.resources[resource_path]
    assert "SUMMARY:[测评] 京东 截止8月21日 18:00" in resource
    assert "DUE:20260821T180000" in resource
    assert "URL:https://example.com/a" in resource
    assert store.get_event(row_id).title == "[测评] 京东 截止8月21日 18:00"
    store.close()


def test_migrate_reminder_titles_refetches_original_mail_for_duration(
    tmp_path: Path, caldav_server
) -> None:
    from mailhub.plugins.caldav import CalDavClient
    from mailhub.plugins.dispatch.reminders.migrate import migrate_reminder_titles
    from mailhub.plugins.dispatch.reminders.reminder_io import create_reminder
    from mailhub.store.sqlite import EventStore

    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        reminders_list="秋招提醒",
    )
    client = CalDavClient(settings)
    event = CandidateEvent(
        message_id="<exam-window@qq.com>",
        subject="笔试通知",
        title="[笔试] 文远知行",
        event_type="exam",
        start_at="2026-05-17T08:00:00",
        end_at="2026-05-17T21:00:00",
        company="文远知行",
        time_precision="window",
    )
    href = create_reminder(event, settings, client)
    resource_path = next(path for path in state.resources if path.endswith(".ics"))
    store = EventStore(tmp_path / "synced.sqlite")
    store.create_event(
        company=event.company,
        event_type=event.event_type,
        title=event.title,
        start_at=event.start_at,
        end_at=event.end_at,
        source_message_id=event.message_id,
        sinks={"reminders": href},
    )
    original = _to_message(
        MailItem(
            message_id=event.message_id,
            subject=event.subject,
            from_="campus@example.com",
            date=None,
            text="请在开放时间范围内任选两小时完成笔试。",
            html="",
        )
    )

    missing_ids: list[str] = []
    missing = migrate_reminder_titles(
        store,
        settings,
        dry_run=True,
        client=client,
        message_fetcher=lambda _ids: [],
        missing_message_ids=missing_ids,
    )
    assert missing == []
    assert missing_ids == [event.message_id]

    changes = migrate_reminder_titles(
        store,
        settings,
        dry_run=False,
        client=client,
        message_fetcher=lambda _ids: [original],
    )

    assert changes[0].new_title == "[笔试·2小时] 文远知行 5月17日 08:00-21:00"
    assert "SUMMARY:[笔试·2小时] 文远知行 5月17日 08:00-21:00" in state.resources[
        resource_path
    ]
    store.close()


def test_identity_migration_reuses_caldav_uid_and_links_legacy_mail(
    tmp_path: Path, caldav_server
) -> None:
    from mailhub.plugins.caldav import CalDavClient
    from mailhub.plugins.dispatch.reminders.reminder_io import create_reminder
    from mailhub.runtime.identity_migrate import migrate_identities
    from mailhub.store.sqlite import EventStore

    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        reminders_list="秋招提醒",
        source_id="qq.default",
    )
    client = CalDavClient(settings)
    event = CandidateEvent(
        message_id="<identity@qq.com>",
        source_id="qq.default",
        source_key="imap:INBOX:99:42",
        item_uid="stable-item-uid",
        subject="测评通知",
        title="[测评] 京东",
        event_type="assessment",
        end_at="2026-08-21T18:00:00",
        company="京东",
        time_precision="window",
    )
    href = create_reminder(event, settings, client)
    resource = next(iter(state.resources.values()))
    assert "UID:stable-item-uid" in resource
    assert "X-MAILHUB-ITEM-ID:stable-item-uid" in resource
    assert "X-MAILHUB-SOURCE-ID:qq.default" in resource
    assert "X-MAILHUB-MESSAGE-ID:<identity@qq.com>" in resource
    assert "X-MAILHUB-SOURCE-KEY:imap:INBOX:99:42" in resource

    store = EventStore(tmp_path / "synced.sqlite")
    row_id = store.create_event(
        company=event.company,
        event_type=event.event_type,
        title=event.title,
        start_at=event.start_at,
        end_at=event.end_at,
        source_message_id=event.message_id,
        sinks={"reminders": href},
    )

    preview = migrate_identities(
        store, client, source_id=settings.source_id, dry_run=True
    )
    assert preview.changes[0].item_uid == "stable-item-uid"
    assert store.get_event(row_id).item_uid == ""

    result = migrate_identities(
        store, client, source_id=settings.source_id, dry_run=False
    )
    assert result.errors == []
    assert store.get_event(row_id).item_uid == "stable-item-uid"
    refs = store.list_event_messages(row_id)
    assert [(ref.source_id, ref.message_id) for ref in refs] == [
        ("qq.default", "<identity@qq.com>")
    ]
    store.close()


def test_identity_migration_adopts_unique_orphan_reminder(
    tmp_path: Path, caldav_server
) -> None:
    from mailhub.plugins.caldav import CalDavClient
    from mailhub.runtime.identity_migrate import migrate_identities
    from mailhub.store.sqlite import EventStore

    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        reminders_list="秋招提醒",
        source_id="qq.default",
    )
    href = "/calendars/user/tasks/orphan.ics"
    state.resources[href] = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTODO
UID:legacy-orphan-uid
SUMMARY:[测评] 京东校招
DUE:20260821T180000
STATUS:COMPLETED
END:VTODO
END:VCALENDAR
"""
    state.etags[href] = '"v1"'
    store = EventStore(tmp_path / "synced.sqlite")
    row_id = store.create_event(
        company="京东校招",
        event_type="assessment",
        title="[测评] 京东校招",
        start_at="",
        end_at="2026-08-21T18:00:00",
        source_message_id="<orphan@qq.com>",
    )

    preview = migrate_identities(
        store,
        CalDavClient(settings),
        source_id=settings.source_id,
        dry_run=True,
        reminders_list=settings.reminders_list,
    )
    assert [
        (item.event_row_id, item.href, item.item_uid, item.match_via)
        for item in preview.adopted_sinks
    ] == [(row_id, href, "legacy-orphan-uid", "company_due")]
    assert store.get_event(row_id).sinks == {}
    assert store.get_event(row_id).item_uid == ""
    assert store.list_event_messages(row_id) == []

    result = migrate_identities(
        store,
        CalDavClient(settings),
        source_id=settings.source_id,
        dry_run=False,
        reminders_list=settings.reminders_list,
    )
    assert result.errors == []
    assert result.ambiguous_matches == []
    assert store.get_event(row_id).sinks == {"reminders": href}
    assert store.get_event(row_id).item_uid == "legacy-orphan-uid"
    assert [
        (ref.source_id, ref.message_id)
        for ref in store.list_event_messages(row_id)
    ] == [("qq.default", "<orphan@qq.com>")]
    store.close()


def test_identity_migration_refuses_ambiguous_orphan_reminder(
    tmp_path: Path, caldav_server
) -> None:
    from mailhub.plugins.caldav import CalDavClient
    from mailhub.runtime.identity_migrate import migrate_identities
    from mailhub.store.sqlite import EventStore

    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        reminders_list="秋招提醒",
        source_id="qq.default",
    )
    href = "/calendars/user/tasks/ambiguous.ics"
    state.resources[href] = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTODO
UID:ambiguous-uid
SUMMARY:[测评] 京东
DUE:20260821T180000
END:VTODO
END:VCALENDAR
"""
    state.etags[href] = '"v1"'
    store = EventStore(tmp_path / "synced.sqlite")
    row_ids = [
        store.create_event(
            company="京东",
            event_type="assessment",
            title="[测评] 京东",
            start_at="",
            end_at="2026-08-21T18:00:00",
            source_message_id=f"<ambiguous-{index}@qq.com>",
        )
        for index in range(2)
    ]

    result = migrate_identities(
        store,
        CalDavClient(settings),
        source_id=settings.source_id,
        dry_run=False,
        reminders_list=settings.reminders_list,
    )

    assert result.adopted_sinks == []
    assert len(result.ambiguous_matches) == 1
    assert all(store.get_event(row_id).sinks == {} for row_id in row_ids)
    store.close()


def test_disabled_calendar_consumes_mail_without_later_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server
) -> None:
    url, state = caldav_server
    batch = IngestBatch(
        messages=[_to_message(_interview_mail())],
        next_checkpoint="99",
    )
    cli.cmd_sync._test_fetch = lambda _checkpoint: batch  # type: ignore[attr-defined]
    disabled = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
    )
    enabled = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
    )
    try:
        monkeypatch.setattr(cli, "load_settings", lambda: disabled)
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
        monkeypatch.setattr(cli, "load_settings", lambda: enabled)
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert state.resources == {}


def test_disabled_reminders_consumes_window_mail_without_rerouting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server
) -> None:
    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        reminders_list="",
        calendar_scan_days=0,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    mail = MailItem(
        message_id="<disabled-reminders@qq.com>",
        subject="【京东校招】测评通知",
        from_="campus@jd.com",
        date="Wed, 19 Aug 2026 03:00:00 +0800",
        text="建议您在48小时内完成测评。https://example.com/a",
        html="",
        uid=44,
    )
    cli.cmd_sync._test_fetch = lambda _checkpoint: IngestBatch(  # type: ignore[attr-defined]
        messages=[_to_message(mail)], next_checkpoint="44"
    )
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert state.resources == {}
    assert all(method != "PUT" for method, _path in state.requests)


def test_sync_runs_bark_only_without_caldav_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        bark_key="device-key",
        bark_server_url="https://bark.example.com",
        calendar_name="",
        reminders_list="",
        calendar_scan_days=0,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    message = _message_from_fixture("美团校园招聘-面试邀请.eml")
    cli.cmd_sync._test_fetch = lambda _checkpoint: IngestBatch(  # type: ignore[attr-defined]
        messages=[message], next_checkpoint="45"
    )
    calls = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"code": 200, "message": "success"}

    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Response(),
    )
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert len(calls) == 1


def test_sync_reschedules_existing_caldav_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server
) -> None:
    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    batches = [
        IngestBatch(
            messages=[_to_message(_interview_mail())],
            next_checkpoint="99",
        ),
        IngestBatch(
            messages=[
                _to_message(
                    _interview_mail(
                        message_id="<reschedule@qq.com>",
                        subject="【美团】面试改期通知",
                        text=(
                            "面试时间调整为2026年8月26日 14:00，"
                            "会议链接 https://meeting.tencent.com/dm/xxx"
                        ),
                        references=["<sync@qq.com>"],
                    )
                )
            ],
            next_checkpoint="100",
        ),
    ]
    cli.cmd_sync._test_fetch = lambda _checkpoint: batches.pop(0)  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert len(state.resources) == 1
    resource = next(iter(state.resources.values()))
    assert "DTSTART:20260826T140000" in resource
    assert sum(method == "PUT" for method, _path in state.requests) == 2


def test_concurrent_caldav_change_fails_instead_of_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server, capsys
) -> None:
    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    batches = [
        IngestBatch(
            messages=[_to_message(_interview_mail())],
            next_checkpoint="99",
        ),
        IngestBatch(
            messages=[
                _to_message(
                    _interview_mail(
                        message_id="<conflict@qq.com>",
                        subject="【美团】面试改期通知",
                        text=(
                            "面试时间调整为2026年8月26日 14:00，"
                            "会议链接 https://meeting.tencent.com/dm/conflict"
                        ),
                        references=["<sync@qq.com>"],
                    )
                )
            ],
            next_checkpoint="100",
        ),
    ]
    cli.cmd_sync._test_fetch = lambda _checkpoint: batches.pop(0)  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
        state.conflict_puts = 1
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    resource = next(iter(state.resources.values()))
    assert "DTSTART:20260825T100000" in resource
    from mailhub.store.sqlite import EventStore

    store = EventStore(tmp_path / "synced.sqlite")
    try:
        assert store.get_checkpoint("qq.default") == "99"
    finally:
        store.close()
    visible = capsys.readouterr().out
    lifecycle = settings.lifecycle_log_path.read_text(encoding="utf-8")
    assert "secret" not in visible + lifecycle
    assert "Authorization" not in visible + lifecycle


def test_sync_deletes_cancelled_caldav_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server
) -> None:
    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    batches = [
        IngestBatch(
            messages=[_to_message(_interview_mail())],
            next_checkpoint="99",
        ),
        IngestBatch(
            messages=[
                _to_message(
                    _interview_mail(
                        message_id="<cancel@qq.com>",
                        subject="【美团】面试取消通知",
                        text="原定面试因故取消，请勿参加。",
                        references=["<sync@qq.com>"],
                    )
                )
            ],
            next_checkpoint="100",
        ),
    ]
    cli.cmd_sync._test_fetch = lambda _checkpoint: batches.pop(0)  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert state.resources == {}
    assert any(method == "DELETE" for method, _path in state.requests)


def test_dry_run_discovers_caldav_without_mutating_or_advancing_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server, capsys
) -> None:
    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    batch = IngestBatch(
        messages=[_to_message(_interview_mail())],
        next_checkpoint="99",
    )
    cli.cmd_sync._test_fetch = lambda _checkpoint: batch  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=True, full=True, json=True))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert state.resources == {}
    assert all(method not in {"PUT", "DELETE"} for method, _ in state.requests)
    visible = capsys.readouterr().out
    assert "secret" not in visible
    assert "Authorization" not in visible
    from mailhub.store.sqlite import EventStore

    store = EventStore(tmp_path / "synced.sqlite")
    try:
        assert store.get_checkpoint("qq.default") is None
    finally:
        store.close()


def test_invalid_caldav_credentials_fail_before_mail_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server
) -> None:
    url, _state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="wrong",
        calendar_name="秋招",
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    fetched = False

    def fetch(_checkpoint):
        nonlocal fetched
        fetched = True
        return IngestBatch(messages=[], next_checkpoint=None)

    cli.cmd_sync._test_fetch = fetch  # type: ignore[attr-defined]
    try:
        with pytest.raises(RuntimeError, match="认证失败") as caught:
            cli.cmd_sync(argparse.Namespace(dry_run=True, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert fetched is False
    assert "wrong" not in str(caught.value)


def test_missing_and_ambiguous_collections_have_clear_errors(
    tmp_path: Path, caldav_server
) -> None:
    from mailhub.plugins.caldav import CalDavClient

    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
    )
    with pytest.raises(RuntimeError, match="找不到名为「不存在」"):
        CalDavClient(settings).collection("不存在", "VEVENT")

    state.duplicate_calendar = True
    with pytest.raises(RuntimeError, match="落点不唯一"):
        CalDavClient(settings).collection("秋招", "VEVENT")


def test_calendar_query_converts_scan_window_to_utc(
    tmp_path: Path, caldav_server
) -> None:
    from mailhub.plugins.caldav import CalDavClient

    url, state = caldav_server
    settings = Settings(
        data_dir=tmp_path,
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
    )
    client = CalDavClient(settings)
    collection = client.collection("秋招", "VEVENT")
    china = timezone(timedelta(hours=8))
    client.query(
        collection,
        "VEVENT",
        datetime(2026, 8, 20, 8, 0, tzinfo=china),
        datetime(2026, 8, 21, 8, 0, tzinfo=china),
    )

    report = next(body for method, _path, body in state.bodies if method == "REPORT")
    assert 'start="20260820T000000Z"' in report
    assert 'end="20260821T000000Z"' in report


def test_sync_adopts_existing_event_from_caldav_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server
) -> None:
    url, state = caldav_server
    href = "/calendars/user/calendar/existing.ics"
    state.resources[href] = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:external-1\r\n"
        "DTSTART:20260825T100000\r\n"
        "DTEND:20260825T110000\r\n"
        "SUMMARY:[面试] 美团\r\n"
        "LOCATION:https://meeting.example.com/old\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    state.etags[href] = '"v1"'
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=90,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    mail = _interview_mail(
        message_id="<reschedule-existing@qq.com>",
        subject="【美团】面试改期通知",
        text=(
            "面试时间调整为2026年8月26日 14:00，"
            "会议链接 https://meeting.tencent.com/dm/new"
        ),
    )
    cli.cmd_sync._test_fetch = lambda _checkpoint: IngestBatch(  # type: ignore[attr-defined]
        messages=[_to_message(mail)], next_checkpoint="101"
    )
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert list(state.resources) == [href]
    assert "UID:external-1" in state.resources[href]
    assert "DTSTART:20260826T140000" in state.resources[href]


def test_failed_caldav_write_keeps_checkpoint_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caldav_server
) -> None:
    url, state = caldav_server
    state.fail_puts = 1
    settings = Settings(
        data_dir=tmp_path,
        qq_email="a@qq.com",
        qq_auth_code="mail-secret",
        caldav_url=url,
        caldav_username="user",
        caldav_password="secret",
        calendar_name="秋招",
        calendar_scan_days=0,
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    batch = IngestBatch(
        messages=[_to_message(_interview_mail())],
        next_checkpoint="99",
    )
    cli.cmd_sync._test_fetch = lambda _checkpoint: batch  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=False, json=False))
        from mailhub.store.sqlite import EventStore

        store = EventStore(tmp_path / "synced.sqlite")
        try:
            assert store.get_checkpoint("qq.default") is None
        finally:
            store.close()

        cli.cmd_sync(argparse.Namespace(dry_run=False, full=False, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert len(state.resources) == 1
