from pathlib import Path

from mailhub.plugins.dispatch.calendar import planner as planner_mod
from mailhub.plugins.dispatch.calendar.handler import CalendarHandler
from mailhub.plugins.dispatch.calendar.match import (
    extract_marker_message_id,
    marker_line,
    match_calendar_event,
    split_title,
)
from mailhub.plugins.dispatch.calendar.planner import find_target
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent
from mailhub.plugins.dispatch.calendar.types import CalendarEventRef
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
        calendar_scan_days=90,
        source_id="qq.default",
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


def _ref(**kwargs) -> CalendarEventRef:
    base = dict(
        uid="uid-1",
        summary="[面试] 美团",
        start_at="2026-08-21T14:00:00",
        end_at="2026-08-21T15:00:00",
    )
    base.update(kwargs)
    return CalendarEventRef(**base)


def test_marker_round_trip():
    desc = f"来源邮件: xxx\n\n{marker_line('<old@qq.com>')}"
    assert extract_marker_message_id(desc) == "<old@qq.com>"
    assert extract_marker_message_id("没有标记") == ""
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
    company = _ref(uid="uid-co", summary="[面试] 美团")
    hit = match_calendar_event(
        _event(references=["<old@qq.com>"]),
        [company, chained],
    )
    assert hit is not None and hit.uid == "uid-chain"


def test_match_falls_back_to_same_company_latest():
    older = _ref(uid="uid-old", start_at="2026-08-20T14:00:00")
    newer = _ref(uid="uid-new", start_at="2026-08-22T14:00:00")
    hit = match_calendar_event(_event(), [older, newer])
    assert hit is not None and hit.uid == "uid-new"


def test_match_skips_other_company_and_type():
    other_co = _ref(uid="uid-jd", summary="[面试] 京东")
    other_type = _ref(uid="uid-exam", summary="[笔试] 美团")
    assert match_calendar_event(_event(), [other_co, other_type]) is None


def test_find_target_adopts_existing_calendar_event(tmp_path: Path, monkeypatch):
    store = EventStore(tmp_path / "t.sqlite")
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        planner_mod,
        "list_calendar_events",
        lambda *_args, **_kw: [
            _ref(uid="uid-calendar-9", start_at="2026-08-21T14:00:00"),
            _ref(
                uid="uid-other",
                summary="[面试] 京东",
                start_at="2026-08-23T14:00:00",
            ),
        ],
    )

    target, via = find_target(store, _event(), settings)
    assert target is not None
    assert via == "calendar_adopt"
    assert target.sinks["calendar"] == "uid-calendar-9"
    assert target.start_at == "2026-08-21T14:00:00"

    monkeypatch.setattr(
        planner_mod,
        "list_calendar_events",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError),
    )
    again, via2 = find_target(store, _event(), settings)
    assert again is not None and again.id == target.id
    assert via2 == "company_type"
    store.close()


def test_find_target_skips_calendar_when_disabled(tmp_path: Path, monkeypatch):
    store = EventStore(tmp_path / "t.sqlite")
    monkeypatch.setattr(
        planner_mod,
        "list_calendar_events",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError),
    )

    target, via = find_target(
        store, _event(), _settings(tmp_path, calendar_scan_days=0)
    )
    assert target is None
    assert via == "none"
    store.close()


def test_adopted_event_updates_existing_calendar_resource(tmp_path: Path, monkeypatch):
    store = EventStore(tmp_path / "t.sqlite")
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        planner_mod, "list_calendar_events", lambda *_a, **_k: [_ref(uid="uid-calendar-9")]
    )
    calls: list[tuple[str, str]] = []
    handler = CalendarHandler(store, settings)
    handler.update_calendar_event = lambda uid, event: calls.append(
        (uid, event.start_at)
    )
    handler.create_calendar_event = lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("应更新而非新建")
    )

    event = _event()
    target, via = find_target(store, event, settings)
    assert target is not None
    assert via == "calendar_adopt"
    handler._apply_update(target, event)

    assert calls == [("uid-calendar-9", "2026-08-26T10:00:00")]
    refreshed = store.get_event(target.id)
    assert refreshed is not None and refreshed.start_at == "2026-08-26T10:00:00"
    store.close()
