"""cmd_sync 生命周期日志：覆盖 apply 终态。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mail_to_calendar import cli
from mail_to_calendar.config import Settings
from mail_to_calendar.mail_qq import FetchResult, MailItem
from mail_to_calendar.store import EventStore


def _settings(tmp_path: Path, **kwargs) -> Settings:
    base = dict(
        qq_email="a@qq.com",
        qq_auth_code="x",
        apple_calendar_name="日历",
        lookback_days=14,
        mail_limit=80,
        reminder_minutes=30,
        llm_api_base="",
        llm_api_key="",
        llm_model="gpt-4o-mini",
        data_dir=tmp_path,
        calendar_scan_days=0,
    )
    base.update(kwargs)
    return Settings(**base)


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
    create_uid: str = "uid-apple-1",
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)
    monkeypatch.setattr(
        cli,
        "fetch_mails",
        lambda *_a, **_k: FetchResult(
            mails=mails, max_uid=99, mode="full", examined=len(mails)
        ),
    )
    monkeypatch.setattr(cli, "create_apple_event", lambda *_a, **_k: create_uid)
    monkeypatch.setattr(
        cli, "update_apple_event", lambda *_a, **_k: None
    )
    monkeypatch.setattr(cli, "delete_apple_event", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "list_apple_events", lambda *_a, **_k: [])

    args = argparse.Namespace(dry_run=dry_run, full=True, json=False)
    cli.cmd_sync(args)


def test_sync_dry_run_logs_apply_dry_run(tmp_path: Path, monkeypatch):
    _run_sync(tmp_path, monkeypatch, mails=[_interview_mail()], dry_run=True)
    records = _read_lifecycle(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"]["status"] == "dry_run"
    apply = next(s for s in records[0]["stages"] if s["name"] == "apply")
    assert apply == {"name": "apply", "result": "dry_run"}


def test_sync_create_logs_applied(tmp_path: Path, monkeypatch):
    _run_sync(tmp_path, monkeypatch, mails=[_interview_mail()])
    records = _read_lifecycle(tmp_path)
    assert len(records) == 1
    assert records[0]["outcome"]["status"] == "applied"
    apply = next(s for s in records[0]["stages"] if s["name"] == "apply")
    assert apply["result"] == "created"
    assert apply["sinks"]["apple"] == "uid-apple-1"
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
        title="[面试] 美团",
        start_at="2026-08-25T10:00:00",
        end_at="2026-08-25T11:00:00",
        source_message_id="<older@qq.com>",
        sinks={"apple": "uid-existing"},
    )
    store.close()

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)
    monkeypatch.setattr(
        cli,
        "fetch_mails",
        lambda *_a, **_k: FetchResult(
            mails=[_interview_mail(message_id="<newer@qq.com>")],
            max_uid=5,
            mode="full",
            examined=1,
        ),
    )
    monkeypatch.setattr(
        cli,
        "create_apple_event",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不应新建")),
    )
    monkeypatch.setattr(cli, "list_apple_events", lambda *_a, **_k: [])

    cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    records = _read_lifecycle(tmp_path)
    assert records[-1]["outcome"]["status"] == "skipped_same"
    apply = next(s for s in records[-1]["stages"] if s["name"] == "apply")
    assert apply["result"] == "skipped_same"


def test_sync_create_failure_logs_failed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)
    monkeypatch.setattr(
        cli,
        "fetch_mails",
        lambda *_a, **_k: FetchResult(
            mails=[_interview_mail()], max_uid=1, mode="full", examined=1
        ),
    )
    monkeypatch.setattr(
        cli,
        "create_apple_event",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("apple down")),
    )
    monkeypatch.setattr(cli, "list_apple_events", lambda *_a, **_k: [])

    cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))
    records = _read_lifecycle(tmp_path)
    assert records[-1]["outcome"]["status"] == "failed"
    apply = next(s for s in records[-1]["stages"] if s["name"] == "apply")
    assert apply["result"] == "failed"
    assert "apple down" in apply["error"]
