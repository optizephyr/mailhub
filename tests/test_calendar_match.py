from pathlib import Path

from core import cli
from core.calendar_match import (
    extract_marker_message_id,
    marker_line,
    match_calendar_event,
    split_title,
)
from core.config import Settings
from core.models import AppleEventRef, CandidateEvent
from core.store import EventStore


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
        calendar_scan_days=90,
    )
    base.update(kwargs)
    return Settings(**base)


def _event(**kwargs) -> CandidateEvent:
    base = dict(
        message_id="<new@qq.com>",
        subject="面试改期通知",
        title="[面试] 美团",
        event_type="interview",
        action="reschedule",
        start_at="2026-08-26T10:00:00",
        end_at="2026-08-26T11:00:00",
        company="美团",
    )
    base.update(kwargs)
    return CandidateEvent(**base)


def _ref(**kwargs) -> AppleEventRef:
    base = dict(
        uid="uid-1",
        summary="[面试] 美团",
        start_at="2026-08-21T14:00:00",
        end_at="2026-08-21T15:00:00",
    )
    base.update(kwargs)
    return AppleEventRef(**base)


def test_marker_round_trip():
    desc = f"来源邮件: xxx\n\n{marker_line('<old@qq.com>')}"
    assert extract_marker_message_id(desc) == "<old@qq.com>"
    assert extract_marker_message_id("没有标记") == ""
    # 旧前缀仍可读
    assert (
        extract_marker_message_id("[mail-to-calendar] mid=<legacy@qq.com>")
        == "<legacy@qq.com>"
    )


def test_split_title():
    assert split_title("[面试] 美团") == ("面试", "美团")
    assert split_title("[测评] 腾讯") == ("测评", "腾讯")
    assert split_title("[取消] 字节跳动") == ("取消", "字节跳动")
    assert split_title("[interview] 美团") == ("interview", "美团")
    assert split_title("和朋友吃饭") == ("", "")


def test_match_prefers_reply_chain_over_company():
    chained = _ref(
        uid="uid-chain", summary="[面试] 某司", marker_message_id="<old@qq.com>"
    )
    same_company = _ref(uid="uid-company", summary="[面试] 美团")
    event = _event(references=["<old@qq.com>"])

    assert match_calendar_event(event, [same_company, chained]) is chained


def test_match_by_company_picks_latest():
    early = _ref(uid="uid-early", start_at="2026-08-20T10:00:00")
    late = _ref(uid="uid-late", start_at="2026-08-22T10:00:00")

    matched = match_calendar_event(_event(), [early, late])
    assert matched is not None and matched.uid == "uid-late"


def test_match_skips_other_type_and_foreign_titles():
    exam = _ref(uid="uid-exam", summary="[笔试] 美团")
    manual = _ref(uid="uid-manual", summary="美团 面试")

    assert match_calendar_event(_event(event_type="interview"), [exam, manual]) is None
    # 学段未知时不拦：cancel / other 这类标题也能接管
    cancelled = _ref(uid="uid-cancel", summary="[取消] 美团")
    matched = match_calendar_event(_event(event_type="other"), [exam, cancelled])
    assert matched is not None and matched.uid in ("uid-exam", "uid-cancel")


def test_match_needs_company_when_no_chain():
    assert match_calendar_event(_event(company=""), [_ref()]) is None


def test_find_target_adopts_existing_calendar_event(tmp_path: Path, monkeypatch):
    store = EventStore(tmp_path / "t.sqlite")
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        cli,
        "list_apple_events",
        lambda *_args, **_kw: [
            _ref(uid="uid-apple-9", start_at="2026-08-21T14:00:00"),
            _ref(
                uid="uid-other",
                summary="[面试] 京东",
                start_at="2026-08-23T14:00:00",
            ),
        ],
    )

    target, via = cli._find_target(store, _event(), settings)
    assert target is not None
    assert via == "calendar_adopt"
    assert target.sinks["apple"] == "uid-apple-9"
    assert target.start_at == "2026-08-21T14:00:00"

    # 接管后写回本地库，下次不必再扫日历
    monkeypatch.setattr(
        cli, "list_apple_events", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError)
    )
    again, via2 = cli._find_target(store, _event(), settings)
    assert again is not None and again.id == target.id
    assert via2 == "company_type"
    store.close()


def test_find_target_skips_calendar_when_disabled(tmp_path: Path, monkeypatch):
    store = EventStore(tmp_path / "t.sqlite")
    monkeypatch.setattr(
        cli, "list_apple_events", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError)
    )

    target, via = cli._find_target(
        store, _event(), _settings(tmp_path, calendar_scan_days=0)
    )
    assert target is None
    assert via == "none"
    store.close()


def test_adopted_event_updates_existing_apple_uid(tmp_path: Path, monkeypatch):
    store = EventStore(tmp_path / "t.sqlite")
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        cli, "list_apple_events", lambda *_a, **_k: [_ref(uid="uid-apple-9")]
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli,
        "update_apple_event",
        lambda uid, event, cal: calls.append((uid, event.start_at)),
    )
    monkeypatch.setattr(
        cli,
        "create_apple_event",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("应更新而非新建")),
    )

    event = _event()
    target, via = cli._find_target(store, event, settings)
    assert target is not None
    assert via == "calendar_adopt"
    cli._apply_update(target, event, settings, store)

    assert calls == [("uid-apple-9", "2026-08-26T10:00:00")]
    refreshed = store.get_event(target.id)
    assert refreshed is not None and refreshed.start_at == "2026-08-26T10:00:00"
    store.close()
