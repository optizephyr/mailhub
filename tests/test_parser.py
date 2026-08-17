import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mail_to_calendar.config import Settings, load_settings
from mail_to_calendar.mail_qq import MailItem
from mail_to_calendar.parser import (
    classify_stage,
    detect_action,
    heuristic_parse,
    parse_datetime,
    parse_llm_json,
    parse_mail,
)
from mail_to_calendar.rules import coarse_filter
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
    )
    base.update(kwargs)
    return Settings(**base)


def _mail(**kwargs) -> MailItem:
    defaults = dict(
        message_id="<m@qq.com>",
        subject="hello",
        from_="hr@x.com",
        date=None,
        text="",
        html="",
    )
    defaults.update(kwargs)
    return MailItem(**defaults)


def test_parse_chinese_datetime():
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 1, 12, 0, tzinfo=tz)
    dt = parse_datetime("请于2026年8月20日 14:30参加线上面试", now=now)
    assert dt is not None
    assert dt.year == 2026 and dt.month == 8 and dt.day == 20
    assert dt.hour == 14 and dt.minute == 30


def test_heuristic_interview_mail():
    mail = MailItem(
        message_id="<abc@qq.com>",
        subject="【字节跳动】校招技术一面通知",
        from_="hr@bytedance.com",
        date=None,
        text=(
            "您好，面试时间已确认，请于2026年8月25日 10:00准时参加技术一面，"
            "会议链接 https://meeting.tencent.com/dm/xxx"
        ),
        html="",
    )
    event = heuristic_parse(mail)
    assert event is not None
    assert event.action == "create"
    assert event.event_type == "interview"
    assert "字节跳动" in event.title or "字节" in event.company or "字节" in event.title
    assert event.start_at.startswith("2026-08-25T10:00")
    assert "meeting.tencent.com" in event.meeting_url


def test_skip_schedule_invite_with_candidate_slots():
    mail = MailItem(
        message_id="<invite@qq.com>",
        subject="【美团】请选择面试时间",
        from_="campus@meituan.com",
        date=None,
        text=(
            "恭喜通过简历筛选，请点击链接选择面试时间。\n"
            "可选时段：2026年8月20日 10:00、2026年8月21日 14:00、2026年8月22日 16:00\n"
            "请于2026年8月19日 18:00前完成预约。"
        ),
        html="",
    )
    assert classify_stage(f"{mail.subject}\n{mail.text}") == "schedule_invite"
    assert heuristic_parse(mail) is None


def test_confirmed_notice_after_invite_still_works():
    mail = MailItem(
        message_id="<notice@qq.com>",
        subject="【美团】面试通知",
        from_="campus@meituan.com",
        date=None,
        text=(
            "您的面试时间已确认：2026年8月21日 14:00，请准时参加。"
            "会议号 123456，入会密码 0000。"
        ),
        html="",
    )
    assert classify_stage(f"{mail.subject}\n{mail.text}") == "confirmed"
    event = heuristic_parse(mail)
    assert event is not None
    assert event.start_at.startswith("2026-08-21T14:00")


def test_cancel_mail_action():
    mail = MailItem(
        message_id="<cancel@qq.com>",
        subject="【美团】面试取消通知",
        from_="campus@meituan.com",
        date=None,
        text="很抱歉，原定面试已取消，您无需参加。",
        html="",
    )
    assert detect_action(f"{mail.subject}\n{mail.text}") == "cancel"
    event = heuristic_parse(mail)
    assert event is not None
    assert event.action == "cancel"
    assert event.company == "美团"


def test_reschedule_mail_action():
    mail = MailItem(
        message_id="<re@qq.com>",
        subject="【美团】面试改期通知",
        from_="campus@meituan.com",
        date=None,
        text="原面试改期，新的面试时间为2026年8月28日 15:00，请准时参加。",
        html="",
    )
    assert detect_action(f"{mail.subject}\n{mail.text}") == "reschedule"
    event = heuristic_parse(mail)
    assert event is not None
    assert event.action == "reschedule"
    assert event.start_at.startswith("2026-08-28T15:00")


def test_openai_compatible_llm_settings(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LLM_API_BASE", "https://api.example.com/v1/")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "example-model")

    settings = load_settings(str(tmp_path / "missing.env"))

    assert settings.llm_enabled
    assert settings.llm_api_base == "https://api.example.com/v1"
    assert settings.llm_api_key == "test-key"
    assert settings.llm_model == "example-model"


def test_parse_llm_json_accepts_markdown_fence():
    assert parse_llm_json('```json\n{"relevant": true}\n```') == {"relevant": True}


def test_parse_llm_json_strips_minimax_think_block():
    raw = (
        "<think>\n"
        '示例 {"relevant": false, "confidence": 0.1}\n'
        "长篇推理…\n"
        "</think>\n\n"
        '{"relevant": true, "action": "create", "confidence": 0.95}'
    )
    assert parse_llm_json(raw) == {
        "relevant": True,
        "action": "create",
        "confidence": 0.95,
    }


def test_parse_llm_json_prefers_last_object_when_examples_precede():
    raw = (
        '先看示例：{"relevant": false}\n'
        '最终：{"relevant": true, "stage": "confirmed"}'
    )
    assert parse_llm_json(raw) == {"relevant": True, "stage": "confirmed"}


def test_coarse_filter_rejects_unrelated():
    mail = _mail(subject="快递已签收", text="您的包裹已送达。")
    result = coarse_filter(mail)
    assert not result.passed
    assert result.reason == "no_recruit_signal"


def test_coarse_filter_passes_interview():
    mail = _mail(subject="【美团】面试通知", text="请准时参加。")
    assert coarse_filter(mail).passed


def test_parse_mail_coarse_reject_skips_llm(tmp_path: Path, monkeypatch):
    settings = _settings(
        tmp_path,
        llm_api_base="https://api.example.com/v1",
        llm_api_key="k",
    )
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("LLM should not be called")

    monkeypatch.setattr("mail_to_calendar.parser.llm_parse", boom)
    mail = _mail(subject="账单通知", text="本月话费 30 元。")
    assert parse_mail(mail, settings) is None
    assert called["n"] == 0
    log = (tmp_path / "logs" / "coarse_filter.jsonl").read_text(encoding="utf-8")
    assert "no_recruit_signal" in log


def test_parse_mail_model_reject_no_heuristic_fallback(tmp_path: Path, monkeypatch):
    settings = _settings(
        tmp_path,
        llm_api_base="https://api.example.com/v1",
        llm_api_key="k",
    )
    mail = _mail(
        message_id="<invite@qq.com>",
        subject="【美团】请选择面试时间",
        text=(
            "请点击链接选择面试时间。可选时段：2026年8月20日 10:00。"
            "请于2026年8月19日 18:00前完成预约。"
        ),
    )

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "relevant": False,
                                    "stage": "schedule_invite",
                                    "action": "create",
                                }
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(
        "mail_to_calendar.parser.requests.post",
        lambda *a, **k: FakeResp(),
    )
    # 若走启发式，这封信也会被 skip；用 spy 确认启发式未被调用
    called = {"heuristic": 0}
    real_heuristic = heuristic_parse

    def wrapped(m):
        called["heuristic"] += 1
        return real_heuristic(m)

    monkeypatch.setattr("mail_to_calendar.parser.heuristic_parse", wrapped)
    assert parse_mail(mail, settings) is None
    assert called["heuristic"] == 0

    records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "llm_parse.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["decision"] == "reject_by_model"
    assert records[0]["input"]
    assert records[0]["output_raw"]
    assert records[0]["output_parsed"]["relevant"] is False


def test_parse_mail_logs_think_block_as_reasoning(tmp_path: Path, monkeypatch):
    settings = _settings(
        tmp_path,
        llm_api_base="https://api.example.com/v1",
        llm_api_key="k",
    )
    mail = _mail(
        message_id="<think@qq.com>",
        subject="【美团】面试通知",
        text="面试时间：2026年8月20日 10:00。",
    )
    reasoning = "先判断是否为正式通知，再抽取时间。"
    content = (
        f"<think>\n{reasoning}\n</think>\n\n"
        + json.dumps(
            {
                "relevant": True,
                "action": "create",
                "stage": "confirmed",
                "event_type": "interview",
                "company": "美团",
                "start_at": "2026-08-20T10:00:00",
                "end_at": "2026-08-20T11:00:00",
                "confidence": 0.9,
            },
            ensure_ascii=False,
        )
    )

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(
        "mail_to_calendar.parser.requests.post",
        lambda *a, **k: FakeResp(),
    )

    event = parse_mail(mail, settings)
    assert event is not None
    assert event.start_at == "2026-08-20T10:00:00"

    record = json.loads(
        (tmp_path / "logs" / "llm_parse.jsonl").read_text(encoding="utf-8").strip()
    )
    assert record["decision"] == "accept"
    assert record["output_reasoning"] == reasoning
    # 原始响应仍完整保留 think 标签
    assert "<think>" in record["output_raw"]


def test_parse_mail_llm_error_falls_back_heuristic(tmp_path: Path, monkeypatch):
    settings = _settings(
        tmp_path,
        llm_api_base="https://api.example.com/v1",
        llm_api_key="k",
    )
    mail = _mail(
        message_id="<abc@qq.com>",
        subject="【字节跳动】校招技术一面通知",
        text=(
            "您好，面试时间已确认，请于2026年8月25日 10:00准时参加技术一面，"
            "会议链接 https://meeting.tencent.com/dm/xxx"
        ),
    )

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr("mail_to_calendar.parser.requests.post", boom)
    event = parse_mail(mail, settings)
    assert event is not None
    assert event.start_at.startswith("2026-08-25T10:00")
    record = json.loads(
        (tmp_path / "logs" / "llm_parse.jsonl").read_text(encoding="utf-8").strip()
    )
    assert record["decision"] == "error"
    assert "network down" in (record["error"] or "")


def test_store_cursor_and_active_event(tmp_path: Path):
    store = EventStore(tmp_path / "t.sqlite")
    assert store.get_last_uid() is None
    store.set_last_uid(42)
    assert store.get_last_uid() == 42

    eid = store.create_event(
        company="美团",
        event_type="interview",
        title="[面试] 美团",
        start_at="2026-08-21T14:00:00",
        end_at="2026-08-21T15:00:00",
        source_message_id="<notice@qq.com>",
        sinks={"apple": "uid-1"},
    )
    found = store.find_active_event(company="美团", event_type="interview")
    assert found is not None and found.id == eid
    found2 = store.find_active_event(references=["<notice@qq.com>"])
    assert found2 is not None and found2.id == eid

    store.cancel_event(eid, "<cancel@qq.com>")
    assert store.find_active_event(company="美团") is None
    store.close()
