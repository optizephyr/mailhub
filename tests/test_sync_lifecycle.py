"""cmd_sync 生命周期日志：覆盖 apply 终态。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import mailhub.cli.main as cli
import pytest
import requests
from mailhub.contracts.messages import IngestBatch, MailMessage, SourceRef
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent, MailItem
from mailhub.runtime.config import Settings
from mailhub.store.sqlite import EventStore


def _settings(tmp_path: Path, **kwargs) -> Settings:
    base = dict(
        qq_email="a@qq.com",
        qq_auth_code="x",
        calendar_name="日历",
        reminders_list="提醒事项",
        lookback_days=14,
        mail_limit=80,
        reminder_minutes=30,
        llm_api_base="",
        llm_api_key="",
        llm_model="gpt-4o-mini",
        data_dir=tmp_path,
        calendar_scan_days=0,
        source_id="qq.default",
    )
    base.update(kwargs)
    return Settings(**base)


def _to_message(mail: MailItem, source_id: str = "qq.default") -> MailMessage:
    return MailMessage(
        source=SourceRef(source_id=source_id, message_id=mail.message_id),
        subject=mail.subject,
        sender=mail.from_,
        sent_at=mail.date,
        text=mail.text,
        html=mail.html,
        references=list(mail.references),
    )


def _interview_mail(**kwargs) -> MailItem:
    base = dict(
        message_id="<sync@qq.com>",
        subject="【美团】校招技术一面通知",
        from_="hr@meituan.com",
        date=None,
        text=(
            "您好，面试时间已确认，请于2026年8月25日 10:00准时参加技术一面，"
            "会议链接 https://meeting.tencent.com/dm/xxx"
        ),
        html="",
        uid=42,
    )
    base.update(kwargs)
    return MailItem(**base)


def _read_lifecycle(tmp_path: Path) -> list[dict]:
    path = tmp_path / "logs" / "mail_lifecycle.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run_sync(
    tmp_path: Path,
    monkeypatch,
    *,
    mails: list[MailItem],
    dry_run: bool = False,
    create_uid: str = "uid-calendar-1",
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)

    batch = IngestBatch(
        messages=[_to_message(m) for m in mails],
        next_checkpoint="99",
    )
    cli.cmd_sync._test_fetch = lambda _cp: batch  # type: ignore[attr-defined]
    cli.cmd_sync._test_create_calendar_event = lambda *_a, **_k: create_uid  # type: ignore[attr-defined]
    cli.cmd_sync._test_update_calendar_event = lambda *_a, **_k: None  # type: ignore[attr-defined]
    cli.cmd_sync._test_delete_calendar_event = lambda *_a, **_k: None  # type: ignore[attr-defined]
    cli.cmd_sync._test_list_calendar_events = lambda *_a, **_k: []  # type: ignore[attr-defined]
    cli.cmd_sync._test_create_reminder = lambda *_a, **_k: "rem-1"  # type: ignore[attr-defined]
    cli.cmd_sync._test_update_reminder = lambda *_a, **_k: None  # type: ignore[attr-defined]
    cli.cmd_sync._test_delete_reminder = lambda *_a, **_k: None  # type: ignore[attr-defined]

    try:
        args = argparse.Namespace(dry_run=dry_run, full=True, json=False)
        cli.cmd_sync(args)
    finally:
        for name in (
            "_test_fetch",
            "_test_create_calendar_event",
            "_test_update_calendar_event",
            "_test_delete_calendar_event",
            "_test_list_calendar_events",
            "_test_create_reminder",
            "_test_update_reminder",
            "_test_delete_reminder",
        ):
            if hasattr(cli.cmd_sync, name):
                delattr(cli.cmd_sync, name)


@pytest.mark.parametrize(
    ("dry_run", "settings_overrides", "error"),
    [
        (False, {}, "密钥和服务器地址"),
        (True, {}, "密钥和服务器地址"),
        (False, {"bark_server_url": "https://bark.example.com"}, "缺少 Bark 密钥$"),
        (True, {"bark_server_url": "https://bark.example.com"}, "缺少 Bark 密钥$"),
        (False, {"bark_key": "test-device-key"}, "缺少 Bark 服务器地址$"),
        (True, {"bark_key": "test-device-key"}, "缺少 Bark 服务器地址$"),
    ],
)
def test_sync_rejects_enabled_bark_without_delivery_config_before_fetch(
    tmp_path: Path, monkeypatch, dry_run, settings_overrides, error
):
    settings = _settings(tmp_path, bark_enabled=True, **settings_overrides)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)
    fetched = False

    def fetch(_checkpoint):
        nonlocal fetched
        fetched = True
        return IngestBatch(messages=[], next_checkpoint=None)

    cli.cmd_sync._test_fetch = fetch  # type: ignore[attr-defined]
    try:
        with pytest.raises(SystemExit, match=error):
            cli.cmd_sync(argparse.Namespace(dry_run=dry_run, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")

    assert not fetched


def test_sync_dry_run_with_bark_enabled_does_not_contact_server(
    tmp_path: Path, monkeypatch
):
    settings = _settings(
        tmp_path,
        bark_enabled=True,
        bark_key="test-device-key",
        bark_server_url="https://bark.example.com",
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_a, **_k: pytest.fail("dry-run 不应请求 Bark 服务器"),
    )
    batch = IngestBatch(
        messages=[_to_message(_interview_mail())],
        next_checkpoint="99",
    )
    cli.cmd_sync._test_fetch = lambda _cp: batch  # type: ignore[attr-defined]
    cli.cmd_sync._test_list_calendar_events = lambda *_a, **_k: []  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=True, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")
        delattr(cli.cmd_sync, "_test_list_calendar_events")

    records = _read_lifecycle(tmp_path)
    assert records[0]["outcome"]["status"] == "dry_run"
    apply = next(s for s in records[0]["stages"] if s["name"] == "apply")
    assert apply["result"] == "would_create"


@pytest.mark.parametrize(
    ("dry_run", "expected_status"),
    [(False, "applied"), (True, "dry_run")],
)
def test_sync_with_bark_disabled_needs_no_delivery_config(
    tmp_path: Path, monkeypatch, dry_run, expected_status
):
    _run_sync(tmp_path, monkeypatch, mails=[_interview_mail()], dry_run=dry_run)

    records = _read_lifecycle(tmp_path)
    assert records[0]["outcome"]["status"] == expected_status


def test_sync_dry_run_logs_apply_would_create(tmp_path: Path, monkeypatch):
    _run_sync(tmp_path, monkeypatch, mails=[_interview_mail()], dry_run=True)
    records = _read_lifecycle(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"]["status"] == "dry_run"
    apply = next(s for s in records[0]["stages"] if s["name"] == "apply")
    assert apply["result"] == "would_create"
    assert apply["match"] == {"via": "none"}
    assert "planned_event" in apply
    assert apply["planned_event"]["title"]
    assert "event_row_id" not in apply

    store = EventStore(tmp_path / "synced.sqlite")
    assert not store.already_processed("<sync@qq.com>")
    store.close()


def test_sync_dry_run_logs_would_skip_duplicate(tmp_path: Path, monkeypatch):
    mail = _interview_mail()
    _run_sync(tmp_path, monkeypatch, mails=[mail])
    _run_sync(tmp_path, monkeypatch, mails=[mail], dry_run=True)
    records = _read_lifecycle(tmp_path)
    assert records[-1]["outcome"]["status"] == "dry_run"
    apply = next(s for s in records[-1]["stages"] if s["name"] == "apply")
    assert apply["result"] == "would_skip_duplicate"
    assert "planned_event" in apply


def test_sync_dry_run_logs_would_skip_same(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    store = EventStore(tmp_path / "synced.sqlite")
    store.create_event(
        company="美团",
        event_type="interview",
        title="[interview] 美团",
        start_at="2026-08-25T10:00:00",
        end_at="2026-08-25T11:00:00",
        source_message_id="<older@qq.com>",
        sinks={"calendar": "uid-existing"},
    )
    store.close()

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)
    batch = IngestBatch(
        messages=[_to_message(_interview_mail(message_id="<newer@qq.com>"))],
        next_checkpoint="5",
    )
    cli.cmd_sync._test_fetch = lambda _cp: batch  # type: ignore[attr-defined]
    cli.cmd_sync._test_list_calendar_events = lambda *_a, **_k: []  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=True, full=True, json=False))
    finally:
        delattr(cli.cmd_sync, "_test_fetch")
        delattr(cli.cmd_sync, "_test_list_calendar_events")

    records = _read_lifecycle(tmp_path)
    assert records[-1]["outcome"]["status"] == "dry_run"
    apply = next(s for s in records[-1]["stages"] if s["name"] == "apply")
    assert apply["result"] == "would_skip_same"
    assert apply["match"]["via"] == "company_type"
    assert apply["event_row_id"] == 1


def test_sync_create_logs_applied(tmp_path: Path, monkeypatch):
    _run_sync(tmp_path, monkeypatch, mails=[_interview_mail()])
    records = _read_lifecycle(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"]["status"] == "applied"
    apply = next(s for s in records[0]["stages"] if s["name"] == "apply")
    assert apply["result"] == "created"
    assert apply["sinks"]["calendar"] == "uid-calendar-1"
    assert apply["event_row_id"] == 1


def test_sync_duplicate_logs_skipped(tmp_path: Path, monkeypatch):
    mail = _interview_mail()
    _run_sync(tmp_path, monkeypatch, mails=[mail])
    _run_sync(tmp_path, monkeypatch, mails=[mail], create_uid="uid-should-not")
    records = _read_lifecycle(tmp_path)
    assert len(records) == 2
    assert records[1]["outcome"]["status"] == "skipped_duplicate"
    apply = next(s for s in records[1]["stages"] if s["name"] == "apply")
    assert apply["result"] == "skipped_duplicate"


def test_sync_same_time_logs_skipped_same(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    store = EventStore(tmp_path / "synced.sqlite")
    store.create_event(
        company="美团",
        event_type="interview",
        title="[interview] 美团",
        start_at="2026-08-25T10:00:00",
        end_at="2026-08-25T11:00:00",
        source_message_id="<older@qq.com>",
        sinks={"calendar": "uid-existing"},
    )
    store.close()

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)
    batch = IngestBatch(
        messages=[_to_message(_interview_mail(message_id="<newer@qq.com>"))],
        next_checkpoint="5",
    )
    cli.cmd_sync._test_fetch = lambda _cp: batch  # type: ignore[attr-defined]
    cli.cmd_sync._test_create_calendar_event = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        AssertionError("不应新建")
    )
    cli.cmd_sync._test_list_calendar_events = lambda *_a, **_k: []  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        for name in ("_test_fetch", "_test_create_calendar_event", "_test_list_calendar_events"):
            if hasattr(cli.cmd_sync, name):
                delattr(cli.cmd_sync, name)

    records = _read_lifecycle(tmp_path)
    assert records[-1]["outcome"]["status"] == "skipped_same"
    apply = next(s for s in records[-1]["stages"] if s["name"] == "apply")
    assert apply["result"] == "skipped_same"


def test_sync_create_failure_logs_failed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)
    batch = IngestBatch(
        messages=[_to_message(_interview_mail())],
        next_checkpoint="1",
    )
    cli.cmd_sync._test_fetch = lambda _cp: batch  # type: ignore[attr-defined]
    cli.cmd_sync._test_create_calendar_event = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[attr-defined]
        RuntimeError("caldav down")
    )
    cli.cmd_sync._test_list_calendar_events = lambda *_a, **_k: []  # type: ignore[attr-defined]
    try:
        cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    finally:
        for name in ("_test_fetch", "_test_create_calendar_event", "_test_list_calendar_events"):
            if hasattr(cli.cmd_sync, name):
                delattr(cli.cmd_sync, name)

    records = _read_lifecycle(tmp_path)
    assert records[-1]["outcome"]["status"] == "failed"
    apply = next(s for s in records[-1]["stages"] if s["name"] == "apply")
    assert apply["result"] == "failed"
    assert "caldav down" in apply["error"]


def _pdd_exam_mail(**kwargs) -> MailItem:
    base = dict(
        message_id="<pdd1@nowcoder.net>",
        subject="【拼多多集团-PDD】2027届校园招聘-在线笔试邀请",
        from_="support@batchmail.nowcoder.net",
        date=None,
        text=(
            "感谢投递拼多多集团-PDD，笔试时间为2026年8月16日 19:00，"
            "链接 https://hr.nowcoder.com/v1/s/cn5D56G7#"
        ),
        html="",
        uid=3823,
    )
    base.update(kwargs)
    return MailItem(**base)


def test_store_finds_fuzzy_company(tmp_path: Path):
    store = EventStore(tmp_path / "t.sqlite")
    eid = store.create_event(
        company="拼多多集团-PDD",
        event_type="exam",
        title="[exam] 拼多多集团-PDD",
        start_at="2026-08-16T19:00:00",
        end_at="2026-08-16T21:00:00",
        source_message_id="<old@nowcoder.net>",
        sinks={"calendar": "uid-1"},
    )
    found = store.find_active_event(company="拼多多", event_type="exam")
    assert found is not None and found.id == eid
    store.close()


def test_dry_run_merges_duplicate_pdd_invites(tmp_path: Path, monkeypatch):
    from mailhub.plugins.policies.qiuzhao import parser as parser_mod

    mails = [
        _pdd_exam_mail(
            message_id="<1786696273191@nowcoder.net>",
            uid=3823,
        ),
        _pdd_exam_mail(
            message_id="<1786626160926@nowcoder.net>",
            uid=3821,
            text=(
                "恭喜通过筛选，笔试时间为2026年8月16日 19:00，"
                "链接 https://hr.nowcoder.com/v1/s/cn5D56G7#"
            ),
        ),
    ]
    parsed = [
        CandidateEvent(
            message_id=mails[0].message_id,
            subject=mails[0].subject,
            title="[exam] 拼多多集团-PDD",
            event_type="exam",
            action="create",
            start_at="2026-08-16T19:00:00",
            end_at="2026-08-16T21:00:00",
            company="拼多多集团-PDD",
            confidence=0.9,
        ),
        CandidateEvent(
            message_id=mails[1].message_id,
            subject=mails[1].subject,
            title="[exam] 拼多多",
            event_type="exam",
            action="create",
            start_at="2026-08-16T19:00:00",
            end_at="2026-08-16T21:00:00",
            company="拼多多",
            confidence=0.95,
        ),
    ]
    idx = {"n": 0}

    def fake_parse(mail, settings, *, trace=None):
        event = parsed[idx["n"]]
        idx["n"] += 1
        if trace is not None:
            trace.add_stage(
                {
                    "name": "coarse_filter",
                    "result": "pass",
                    "reason": "recruit_keyword",
                }
            )
            trace.add_stage(
                {
                    "name": "parse",
                    "engine": "test",
                    "result": "accept",
                    "event": {
                        "action": event.action,
                        "event_type": event.event_type,
                        "company": event.company,
                        "title": event.title,
                        "start_at": event.start_at,
                        "end_at": event.end_at,
                    },
                }
            )
        return event

    monkeypatch.setattr(parser_mod, "parse_mail", fake_parse)
    _run_sync(tmp_path, monkeypatch, mails=mails, dry_run=True)
    records = _read_lifecycle(tmp_path)
    assert len(records) == 2
    apply0 = next(s for s in records[0]["stages"] if s["name"] == "apply")
    apply1 = next(s for s in records[1]["stages"] if s["name"] == "apply")
    assert apply0["result"] == "would_create"
    assert apply1["result"] == "would_skip_same"
    assert apply1["match"]["via"] == "session"


def test_sync_merges_fuzzy_company_across_runs(tmp_path: Path, monkeypatch):
    from mailhub.plugins.policies.qiuzhao import parser as parser_mod

    first = _pdd_exam_mail(message_id="<first@nowcoder.net>")
    second = _pdd_exam_mail(message_id="<second@nowcoder.net>")

    events = {
        first.message_id: CandidateEvent(
            message_id=first.message_id,
            subject=first.subject,
            title="[exam] 拼多多集团-PDD",
            event_type="exam",
            action="create",
            start_at="2026-08-16T19:00:00",
            end_at="2026-08-16T21:00:00",
            company="拼多多集团-PDD",
            confidence=0.9,
        ),
        second.message_id: CandidateEvent(
            message_id=second.message_id,
            subject=second.subject,
            title="[exam] 拼多多",
            event_type="exam",
            action="create",
            start_at="2026-08-16T19:00:00",
            end_at="2026-08-16T21:00:00",
            company="拼多多",
            confidence=0.95,
        ),
    }

    def fake_parse(mail, settings, *, trace=None):
        event = events[mail.message_id]
        if trace is not None:
            trace.add_stage(
                {"name": "coarse_filter", "result": "pass", "reason": "recruit_keyword"}
            )
            trace.add_stage(
                {
                    "name": "parse",
                    "engine": "test",
                    "result": "accept",
                    "event": {
                        "action": event.action,
                        "event_type": event.event_type,
                        "company": event.company,
                        "title": event.title,
                        "start_at": event.start_at,
                        "end_at": event.end_at,
                    },
                }
            )
        return event

    monkeypatch.setattr(parser_mod, "parse_mail", fake_parse)
    creates: list[str] = []

    def track_create(*_a, **_k):
        creates.append("x")
        return f"uid-{len(creates)}"

    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)

    def run_with(mails: list[MailItem], uid: str) -> None:
        batch = IngestBatch(
            messages=[_to_message(m) for m in mails],
            next_checkpoint=uid,
        )
        cli.cmd_sync._test_fetch = lambda _cp: batch  # type: ignore[attr-defined]
        cli.cmd_sync._test_create_calendar_event = track_create  # type: ignore[attr-defined]
        cli.cmd_sync._test_update_calendar_event = lambda *_a, **_k: None  # type: ignore[attr-defined]
        cli.cmd_sync._test_delete_calendar_event = lambda *_a, **_k: None  # type: ignore[attr-defined]
        cli.cmd_sync._test_list_calendar_events = lambda *_a, **_k: []  # type: ignore[attr-defined]
        try:
            cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
        finally:
            for name in (
                "_test_fetch",
                "_test_create_calendar_event",
                "_test_update_calendar_event",
                "_test_delete_calendar_event",
                "_test_list_calendar_events",
            ):
                if hasattr(cli.cmd_sync, name):
                    delattr(cli.cmd_sync, name)

    run_with([first], "1")
    run_with([second], "2")

    assert len(creates) == 1
    records = _read_lifecycle(tmp_path)
    assert records[-1]["outcome"]["status"] == "skipped_same"
    apply = next(s for s in records[-1]["stages"] if s["name"] == "apply")
    assert apply["match"]["via"] == "company_type"
