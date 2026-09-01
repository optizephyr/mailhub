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


def test_alibaba_same_company_different_business_lines_do_not_overwrite(tmp_path: Path):
    """千问 与 淘天 阿里巴巴邮件应该作为两条独立日程，互不覆盖。

    验证点：
    - 两封邮件解析后 title 不同（都含 ·业务线 后缀）；
    - match_calendar_event 在两者同时出现时不会把它们错认为是同一条；
    - 两封邮件都送进 store 后产生活跃 event 各一条，title 互不相同。
    """
    store = EventStore(tmp_path / "synced.sqlite")
    try:
        from tests.eml_loader import EMAIL_EXAMPLE_DIR
        from mailhub.plugins.policies.qiuzhao.parser import heuristic_parse

        qianwen = heuristic_parse(_mail_item(EMAIL_EXAMPLE_DIR / "【阿里巴巴校园招聘】千问事业部面试通知.eml"))
        taobao = heuristic_parse(_mail_item(EMAIL_EXAMPLE_DIR / "【阿里巴巴校园招聘】淘天集团面试通知.eml"))

        assert qianwen is not None and taobao is not None
        # 核心：两封同公司不同业务线的邮件 title 不同，避免被 dedup 合并
        assert qianwen.title != taobao.title, (
            f"同公司不同业务线 title 应不同，实际都是 {qianwen.title!r}"
        )
        assert qianwen.business_line == "千问事业部"
        assert taobao.business_line == "淘天集团"
        # company 仍是「阿里巴巴」以保持 labels.json 中 company_contains=[阿里] 约定
        assert qianwen.company == taobao.company == "阿里巴巴"
        # 起止时间不同、互不覆盖
        assert qianwen.start_at != taobao.start_at
        assert qianwen.start_at.startswith("2026-09-07T15:00")
        assert taobao.start_at.startswith("2026-09-04T16:00")

        # match_calendar_event：千问 已写入日历后再来淘天，不应误中 千问 那条
        qianwen_ref = _ref(uid="uid-qianwen", summary=qianwen.title, start_at=qianwen.start_at)
        hit = match_calendar_event(taobao, [qianwen_ref])
        assert hit is None, (
            f"公司相同但业务线不同时不该合并，但 match 返回了 {hit}"
        )

        # 反过来淘天 已写入日历后再来千问，也不应误中
        taobao_ref = _ref(uid="uid-taobao", summary=taobao.title, start_at=taobao.start_at)
        hit = match_calendar_event(qianwen, [taobao_ref])
        assert hit is None, (
            f"公司相同但业务线不同时不该合并，但 match 返回了 {hit}"
        )

        # 两封都走 planner：千问、淘天都应产出 ACTION_CREATE，后到的不会覆盖前者
        from mailhub.plugins.dispatch.calendar.planner import (
            CalendarPlanner,
            ACTION_CREATE,
            ACTION_UPDATE,
        )
        settings = _settings(tmp_path)
        planner = CalendarPlanner(
            store, settings, session=[], dry_run=True, source_id=settings.source_id,
        )
        # 首次推送：当作新邮件，session 为空
        actions_q = planner.plan(_resolved(qianwen))
        assert actions_q and actions_q[0].type == ACTION_CREATE, (
            f"千问首次推送应 create，实际 {actions_q[0].type if actions_q else 'none'}"
        )

        # 把千问加入 session，模拟「千问已创建」后续推送淘天
        from mailhub.plugins.dispatch.calendar.planner import session_event_from_candidate
        session = [session_event_from_candidate(qianwen)]
        planner2 = CalendarPlanner(
            store, settings, session=session, dry_run=True, source_id=settings.source_id,
        )
        actions_t = planner2.plan(_resolved(taobao))
        # 核心诉求：淘天不与千问同 target，不走 ACTION_UPDATE
        assert actions_t, "淘天应有动作"
        assert actions_t[0].type == ACTION_CREATE, (
            f"淘天后续推送应 create（不同业务线），实际 {actions_t[0].type}："
            "同公司不同业务线不应触发 update"
        )

        # 反向验证：把淘天加入 session 后推送千问，也仍是 create
        session = [session_event_from_candidate(taobao)]
        planner3 = CalendarPlanner(
            store, settings, session=session, dry_run=True, source_id=settings.source_id,
        )
        actions_q2 = planner3.plan(_resolved(qianwen))
        assert actions_q2 and actions_q2[0].type == ACTION_CREATE, (
            f"千问后续推送应 create（不同业务线），实际 {actions_q2[0].type}"
        )
    finally:
        store.close()


def _mail_item(path):
    """加载 .eml 为 MailItem（仅给测试用，跳过 SourceRef 等包装）。"""
    import email as _email
    from email import policy as _policy
    from mailhub.plugins.policies.qiuzhao.types import MailItem

    with open(path, "rb") as f:
        msg = _email.message_from_binary_file(f, policy=_policy.default)
    text, html = "", ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not text:
            text = part.get_content()
        if part.get_content_type() == "text/html" and not html:
            html = part.get_content()
    return MailItem(
        message_id=str(msg["Message-ID"] or ""),
        subject=str(msg["Subject"] or ""),
        from_=str(msg["From"] or ""),
        date=str(msg["Date"] or ""),
        text=text,
        html=html,
    )


def _resolved(event):
    """把 CandidateEvent 包装成 ResolvedMail，供 CalendarPlanner.plan 使用。"""
    from datetime import datetime
    from mailhub.contracts.resolve import ResolvedMail, TimeConstraint
    from mailhub.contracts.messages import SourceRef

    time = TimeConstraint(
        start_at=event.start_at or None,
        end_at=event.end_at or None,
        timezone="Asia/Shanghai",
        precision=event.time_precision or "fixed",
    )
    source = SourceRef(source_id=event.source_id or "qq.test", message_id=event.message_id)
    return ResolvedMail(
        source=source,
        kind=event.event_type or "interview",
        change="new",
        title=event.title,
        summary=event.subject,
        importance="high",
        time=time,
        location=event.location or None,
        links=[event.meeting_url] if event.meeting_url else [],
        correlation_key=f"{event.company}|{event.event_type}",
        attributes={
            "company": event.company,
            "action": event.action,
            "event_type": event.event_type,
            "meeting_url": event.meeting_url,
            "description": "",
            "references": list(event.references or []),
            "subject": event.subject,
            "source_snippet": event.source_snippet,
            "candidate": event.to_dict(),
            "time_precision": event.time_precision,
            "deadline": event.deadline,
        },
        confidence=event.confidence,
    )
