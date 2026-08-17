"""cmd_sync 生命周期日志：覆盖 apply 终态。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from qiuzhao_mail2calendar import cli
from qiuzhao_mail2calendar.config import Settings
from qiuzhao_mail2calendar.mail_qq import FetchResult, MailItem
from qiuzhao_mail2calendar.store import EventStore


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

    # dry-run must not write store / mark processed
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
    monkeypatch.setattr(cli, "list_apple_events", lambda *_a, **_k: [])

    cli.cmd_sync(argparse.Namespace(dry_run=True, full=True, json=False))
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
        title="[interview] 美团",
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
        sinks={"apple": "uid-1"},
    )
    found = store.find_active_event(company="拼多多", event_type="exam")
    assert found is not None and found.id == eid
    store.close()


def test_dry_run_merges_duplicate_pdd_invites(tmp_path: Path, monkeypatch):
    """同批两封拼多多笔试邀请（公司名略不同）应合并：一新建一跳过。"""
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
    # 第二封启发式猜到的公司可能是「拼多多集团-PDD」或「拼多多」；
    # 用 monkeypatch 固定两封解析结果以覆盖「公司名不一致」场景。
    from qiuzhao_mail2calendar.models import CandidateEvent

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

    monkeypatch.setattr(cli, "parse_mail", fake_parse)
    _run_sync(tmp_path, monkeypatch, mails=mails, dry_run=True)
    records = _read_lifecycle(tmp_path)
    assert len(records) == 2
    apply0 = next(s for s in records[0]["stages"] if s["name"] == "apply")
    apply1 = next(s for s in records[1]["stages"] if s["name"] == "apply")
    assert apply0["result"] == "would_create"
    assert apply1["result"] == "would_skip_same"
    assert apply1["match"]["via"] == "session"


def test_sync_merges_fuzzy_company_across_runs(tmp_path: Path, monkeypatch):
    """上一轮已建成「拼多多集团-PDD」后，再来「拼多多」同时间应跳过。"""
    first = _pdd_exam_mail(message_id="<first@nowcoder.net>")
    second = _pdd_exam_mail(message_id="<second@nowcoder.net>")

    from qiuzhao_mail2calendar.models import CandidateEvent

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

    monkeypatch.setattr(cli, "parse_mail", fake_parse)
    creates: list[str] = []

    def track_create(*_a, **_k):
        creates.append("x")
        return f"uid-{len(creates)}"

    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "require_mail_credentials", lambda _s: None)
    monkeypatch.setattr(cli, "create_apple_event", track_create)
    monkeypatch.setattr(cli, "update_apple_event", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "delete_apple_event", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "list_apple_events", lambda *_a, **_k: [])

    monkeypatch.setattr(
        cli,
        "fetch_mails",
        lambda *_a, **_k: FetchResult(
            mails=[first], max_uid=1, mode="full", examined=1
        ),
    )
    cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))

    monkeypatch.setattr(
        cli,
        "fetch_mails",
        lambda *_a, **_k: FetchResult(
            mails=[second], max_uid=2, mode="full", examined=1
        ),
    )
    cli.cmd_sync(argparse.Namespace(dry_run=False, full=True, json=False))

    assert len(creates) == 1
    records = _read_lifecycle(tmp_path)
    assert records[-1]["outcome"]["status"] == "skipped_same"
    apply = next(s for s in records[-1]["stages"] if s["name"] == "apply")
    assert apply["match"]["via"] == "company_type"