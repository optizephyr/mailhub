"""端到端验证：真实【Shopee】预约面试成功通知.eml 走完 resolve + dispatch，应在 EventStore 产生一条日历。"""

from __future__ import annotations

import email
from email import policy
from pathlib import Path

from mailhub.contracts.messages import SourceRef
from mailhub.plugins.policies.qiuzhao import QiuzhaoResolver
from mailhub.runtime.config import Settings
from mailhub.store.sqlite import EventStore

FIXTURE = Path(__file__).parent / "fixtures" / "email_corpus" / "【Shopee】预约面试成功通知.eml"


def _load_fixture() -> "MailMessage":  # type: ignore[name-defined]
    """把 .eml 装成 MailMessage。"""
    from mailhub.contracts.messages import MailMessage

    with FIXTURE.open("rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)

    text = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not text:
                try:
                    text = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    text = payload.decode(errors="replace") if isinstance(payload, bytes) else ""
            elif ctype == "text/html" and not html:
                try:
                    html = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    html = payload.decode(errors="replace") if isinstance(payload, bytes) else ""
    else:
        try:
            content = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            content = payload.decode(errors="replace") if isinstance(payload, bytes) else ""
        if msg.get_content_type() == "text/html":
            html = content if isinstance(content, str) else str(content)
        else:
            text = content if isinstance(content, str) else str(content)

    return MailMessage(
        source=SourceRef(
            source_id="qq.default",
            message_id=str(msg["message-id"] or "").strip(),
            source_key="imap:INBOX:1485419719:72121",
        ),
        subject=str(msg["subject"] or ""),
        sender=str(msg["from"] or ""),
        sent_at=str(msg["date"] or ""),
        text=text or "",
        html=html or "",
        references=[],
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
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
    )


def test_shopee_booking_confirmed_resolves_to_event(tmp_path: Path):
    """真实邮件：resolve 阶段应该产出 CandidateEvent，start_at=2026-09-05T15:30。"""
    settings = _settings(tmp_path)
    store = EventStore(tmp_path / "synced.sqlite")
    try:
        message = _load_fixture()
        resolver = QiuzhaoResolver(settings)
        result = resolver.resolve(message)

        # 不应该是 IgnoredMail / ResolveFailure
        from mailhub.contracts.resolve import ResolvedMail

        assert isinstance(result, ResolvedMail), (
            f"resolve 未通过: {type(result).__name__}: "
            f"{getattr(result, 'reason', getattr(result, 'error', ''))}"
        )
        # kind 应该是 interview（不是 schedule_invite，因为新启发式把它归 confirmed）
        assert result.kind == "interview"
        # 时间应从正文抽出
        assert result.time is not None
        assert result.time.start_at is not None
        assert result.time.start_at.startswith("2026-09-05T15:30"), (
            f"start_at={result.time.start_at!r}"
        )
        # end_at 是 start + 1h（default interview duration）
        assert result.time.end_at is not None
        assert result.time.end_at.startswith("2026-09-05T16:30"), (
            f"end_at={result.time.end_at!r}"
        )
        # location/meeting_url 至少有一个
        assert result.links, "应当有 meeting_url"
        assert "mokahr.com" in result.links[0]
        # company
        assert "Shopee" in result.attributes.get("company", "")
    finally:
        store.close()


def test_shopee_booking_confirmed_creates_calendar_row(tmp_path: Path):
    """完整：resolve → CalendarPlanner.plan → EventStore.create_event → 真在 sqlite 里有 row。"""
    from mailhub.plugins.dispatch.calendar.planner import CalendarPlanner

    settings = _settings(tmp_path)
    store = EventStore(tmp_path / "synced.sqlite")
    try:
        message = _load_fixture()
        resolver = QiuzhaoResolver(settings)
        result = resolver.resolve(message)
        from mailhub.contracts.resolve import ResolvedMail

        assert isinstance(result, ResolvedMail)
        planner = CalendarPlanner(
            store, settings, session=[], dry_run=True, source_id="qq.default"
        )
        requests = planner.plan(result)

        # 应当产生 1 个 ACTION_CREATE
        from mailhub.plugins.dispatch.calendar.planner import ACTION_CREATE

        create_reqs = [r for r in requests if r.type == ACTION_CREATE]
        assert len(create_reqs) == 1, (
            f"应产生 1 个 calendar.create，实际 {len(create_reqs)}："
            f"{[r.type for r in requests]}"
        )
        req = create_reqs[0]
        # result 应是 'would_create'（dry-run）
        assert req.payload.get("result") in ("would_create", "create")
        # summary 不应是 "would_skip_disabled"
        assert "未启用" not in req.payload.get("summary", ""), req.payload.get("summary")

        # 真在 sqlite 里写一条（模拟 handler.create_event）—— 这里直接用 store 验
        assert result.time is not None
        start_at = result.time.start_at or ""
        end_at = result.time.end_at or ""
        store.create_event(
            company=result.attributes["company"],
            event_type=result.kind,
            title=result.title,
            start_at=start_at,
            end_at=end_at,
            source_message_id=result.source.message_id,
        )
        rows = store.list_active_events()
        shopee = [r for r in rows if r.company == "Shopee"]
        assert len(shopee) == 1
        assert shopee[0].start_at.startswith("2026-09-05T15:30")
        assert shopee[0].title.startswith("[面试]")
    finally:
        store.close()