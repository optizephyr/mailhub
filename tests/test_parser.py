import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mailhub.runtime.config import Settings, load_settings
from mailhub.plugins.policies.qiuzhao.types import MailItem
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent
from mailhub.plugins.policies.qiuzhao.parser import (
    build_title,
    classify_stage,
    detect_action,
    heuristic_parse,
    normalize_event,
    parse_datetime,
    parse_llm_json,
    parse_mail,
)
from mailhub.plugins.policies.qiuzhao.rules import coarse_filter
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


def test_heuristic_assessment_window_without_clock():
    mail = _mail(
        subject="【京东校招】测评通知",
        text="建议您在48小时内完成测评，测评完成后，方可进入后续流程。https://example.com/a",
        date="Wed, 19 Aug 2026 03:00:00 +0800",
    )
    event = heuristic_parse(mail)
    assert event is not None
    assert event.time_precision == "window"
    assert event.event_type == "assessment"
    assert event.end_at.startswith("2026-08-21T03:00")


def test_heuristic_exam_open_window_uses_span():
    mail = _mail(
        subject="【笔试通知】文远知行邀请您参加笔试",
        text=(
            "考试开始时间（北京时间）：2026-05-17 08:00:00\n"
            "考试结束时间（北京时间）：2026-05-17 21:00:00\n"
            "此时间范围内任选两小时完成笔试。\n"
            "考试地址：https://exam.nowcoder.com/cts/x"
        ),
    )
    event = heuristic_parse(mail)
    assert event is not None
    assert event.time_precision == "window"
    assert event.start_at.startswith("2026-05-17T08:00")
    assert event.end_at.startswith("2026-05-17T21:00")
    assert event.deadline == event.end_at


def test_heuristic_long_exam_without_window_signal_stays_fixed():
    mail = _mail(
        subject="【笔试通知】请准时参加现场笔试",
        text=(
            "请于2026年5月17日 08:00准时参加，"
            "考试结束时间为2026年5月17日 14:00。"
            "考试地点：深圳市南山区科技园。"
        ),
    )

    event = heuristic_parse(mail)

    assert event is not None
    assert event.time_precision == "fixed"
    assert event.start_at.startswith("2026-05-17T08:00")
    assert event.end_at.startswith("2026-05-17T14:00")


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


def test_schedule_invite_recognizes_bare_booking_link_text():
    mail = _mail(
        subject="【阿里巴巴校园招聘】业务面试邀约",
        text="请在预约截止前处理：点此预约。",
    )

    event = heuristic_parse(mail)

    assert event is not None
    assert event.event_type == "schedule_invite"


def test_confirmed_notice_after_invite_still_works():
    mail = MailItem(
        message_id="<notice@qq.com>",
        subject="【美团】面试通知",
        from_="campus@meituan.com",
        date=None,
        text=(
            "您的面试时间已确认：2026年8月21日 14:00，请准时参加。"
            "会议链接 https://meeting.tencent.com/dm/confirmed，会议号 123456。"
        ),
        html="",
    )
    assert classify_stage(f"{mail.subject}\n{mail.text}") == "confirmed"
    event = heuristic_parse(mail)
    assert event is not None
    assert event.start_at.startswith("2026-08-21T14:00")


def test_confirmed_notice_without_location_is_rejected():
    mail = _mail(
        subject="【美团】面试通知",
        text="您的面试时间已确认：2026年8月21日 14:00，请准时参加。",
    )
    assert heuristic_parse(mail) is None


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
        text=(
            "原面试改期，新的面试时间为2026年8月28日 15:00，请准时参加。"
            "会议链接 https://meeting.tencent.com/dm/rescheduled"
        ),
        html="",
    )
    assert detect_action(f"{mail.subject}\n{mail.text}") == "reschedule"
    event = heuristic_parse(mail)
    assert event is not None
    assert event.action == "reschedule"
    assert event.start_at.startswith("2026-08-28T15:00")


_DEPLOY_ENV = (
    "QQ_EMAIL",
    "QQ_AUTH_CODE",
    "CALDAV_URL",
    "CALDAV_USERNAME",
    "CALDAV_PASSWORD",
    "LLM_API_BASE",
    "LLM_API_KEY",
    "BARK_SERVER_URL",
    "BARK_KEY",
)


@pytest.fixture
def isolated_deploy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _DEPLOY_ENV:
        monkeypatch.delenv(name, raising=False)


def test_openai_compatible_llm_settings(tmp_path: Path, isolated_deploy_env):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("llm_model: example-model\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "LLM_API_BASE=https://api.example.com/v1/\n"
        "LLM_API_KEY=test-key\n",
        encoding="utf-8",
    )

    settings = load_settings(config_file)

    assert settings.llm_enabled
    assert settings.llm_api_base == "https://api.example.com/v1"
    assert settings.llm_api_key == "test-key"
    assert settings.llm_model == "example-model"


def test_bark_settings_from_env_normalize_server_url(
    tmp_path: Path, isolated_deploy_env
):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("source_id: qq.default\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "BARK_KEY=test-device-key\n"
        "BARK_SERVER_URL=https://bark.example.com/\n",
        encoding="utf-8",
    )

    settings = load_settings(config_file)

    assert settings.bark_enabled is True
    assert settings.bark_key == "test-device-key"
    assert settings.bark_server_url == "https://bark.example.com"


def test_dotenv_does_not_override_existing_env(
    tmp_path: Path, isolated_deploy_env, monkeypatch: pytest.MonkeyPatch
):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("{}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("QQ_EMAIL=from-file@qq.com\n", encoding="utf-8")
    monkeypatch.setenv("QQ_EMAIL", "from-process@qq.com")

    settings = load_settings(config_file)

    assert settings.qq_email == "from-process@qq.com"


def test_yaml_rejects_deploy_keys(tmp_path: Path, isolated_deploy_env):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("qq_email: a@qq.com\n", encoding="utf-8")

    with pytest.raises(ValueError, match="未知配置项"):
        load_settings(config_file)


def test_settings_reject_unknown_key(tmp_path: Path, isolated_deploy_env):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("bark_sever_url: https://typo.example.com\n", encoding="utf-8")

    with pytest.raises(ValueError, match="未知配置项.*bark_sever_url"):
        load_settings(config_file)


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

    monkeypatch.setattr("mailhub.plugins.policies.qiuzhao.parser.llm_parse", boom)
    mail = _mail(subject="账单通知", text="本月话费 30 元。")
    assert parse_mail(mail, settings) is None
    assert called["n"] == 0
    log = (tmp_path / "logs" / "mail_lifecycle.jsonl").read_text(encoding="utf-8")
    assert "no_recruit_signal" in log
    assert "rejected_coarse" in log


def test_parse_mail_model_accepts_schedule_invite_without_heuristic(
    tmp_path: Path, monkeypatch
):
    settings = _settings(
        tmp_path,
        llm_api_base="https://api.example.com/v1",
        llm_api_key="k",
    )
    mail = _mail(
        message_id="<invite@qq.com>",
        subject="【美团】请选择面试时间",
        text="请点击链接选择面试时间。请于2026年8月19日 18:00前完成预约。",
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
                                        "relevant": True,
                                    "stage": "schedule_invite",
                                    "action": "create",
                                        "deadline": "2026-08-19T17:00:00",
                                }
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(
        "mailhub.plugins.policies.qiuzhao.parser.requests.post",
        lambda *a, **k: FakeResp(),
    )
    # stage=schedule_invite 是模型接受的已解析邮件，不应再走启发式。
    called = {"heuristic": 0}
    real_heuristic = heuristic_parse

    def wrapped(m):
        called["heuristic"] += 1
        return real_heuristic(m)

    monkeypatch.setattr("mailhub.plugins.policies.qiuzhao.parser.heuristic_parse", wrapped)
    event = parse_mail(mail, settings)
    assert event is not None
    assert event.event_type == "schedule_invite"
    assert event.deadline == "2026-08-19T17:00:00"
    assert called["heuristic"] == 0

    lifecycle = [
        json.loads(line)
        for line in (tmp_path / "logs" / "mail_lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(lifecycle) == 1
    assert lifecycle[0]["outcome"]["status"] == "dry_run"
    parse_stage = next(s for s in lifecycle[0]["stages"] if s["name"] == "parse")
    assert parse_stage["result"] == "accept"
    assert parse_stage["llm"] == {
        "decision": "accept",
        "latency_ms": parse_stage["llm"]["latency_ms"],
        "model": "gpt-4o-mini",
    }
    assert "reject_reason" not in parse_stage["llm"]

    records = [
        json.loads(line)
        for line in (tmp_path / "logs" / "llm_io.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["decision"] == "accept"
    assert records[0]["trace_id"] == lifecycle[0]["trace_id"]
    assert records[0]["input"]
    assert records[0]["output_raw"]
    assert records[0]["output_parsed"]["relevant"] is True


def test_parse_mail_keeps_think_in_raw_but_not_as_separate_field(
    tmp_path: Path, monkeypatch
):
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
                "location": "https://meeting.tencent.com/dm/test",
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
        "mailhub.plugins.policies.qiuzhao.parser.requests.post",
        lambda *a, **k: FakeResp(),
    )

    event = parse_mail(mail, settings)
    assert event is not None
    assert event.start_at == "2026-08-20T10:00:00"

    lifecycle = json.loads(
        (tmp_path / "logs" / "mail_lifecycle.jsonl").read_text(encoding="utf-8").strip()
    )
    assert lifecycle["outcome"]["status"] == "dry_run"
    parse_stage = next(s for s in lifecycle["stages"] if s["name"] == "parse")
    assert parse_stage["engine"] == "llm"
    assert parse_stage["result"] == "accept"
    assert lifecycle["stages"][-1]["name"] == "apply"
    assert lifecycle["stages"][-1]["result"] == "dry_run"
    assert "match" not in lifecycle["stages"][-1]

    record = json.loads(
        (tmp_path / "logs" / "llm_io.jsonl").read_text(encoding="utf-8").strip()
    )
    assert record["decision"] == "accept"
    assert record["trace_id"] == lifecycle["trace_id"]
    assert "output_reasoning" not in record
    # 原始响应完整保留（含 think）；解析结果不含思考过程
    assert "<think>" in record["output_raw"]
    assert reasoning in record["output_raw"]
    assert record["output_parsed"]["start_at"] == "2026-08-20T10:00:00"


def test_parse_mail_incomplete_keeps_llm_company_and_url(tmp_path: Path, monkeypatch):
    settings = _settings(
        tmp_path,
        llm_api_base="https://api.example.com/v1",
        llm_api_key="k",
    )
    mail = _mail(
        message_id="<kuaishou-assess@qq.com>",
        subject="【快手校园招聘】在线人才测评邀请",
        date="2026-08-17T15:41:44+08:00",
        text=(
            "请于3个工作日内完成测评。进入测评平台：\n"
            "https://datatalk360.com/225On"
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
                                    "relevant": True,
                                    "action": "create",
                                    "event_type": "assessment",
                                    "time_precision": "window",
                                    "company": "快手",
                                    "title": "快手2027届校园招聘在线人才测评",
                                    "start_at": "",
                                    "end_at": "",
                                    "deadline": "",
                                    "location": "进入测评平台：",
                                    "meeting_url": "https://datatalk360.com/225On",
                                    "confidence": 0.95,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(
        "mailhub.plugins.policies.qiuzhao.parser.requests.post",
        lambda *a, **k: FakeResp(),
    )
    event = parse_mail(mail, settings)
    assert event is not None
    assert event.company == "快手"
    assert event.event_type == "assessment"
    assert event.time_precision == "window"
    assert event.end_at.startswith("2026-08-20T15:41")
    assert "datatalk360.com" in event.meeting_url
    assert "datatalk360.com" in event.location
    assert "进入测评平台" not in event.location
    assert event.confidence < 0.95


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

    monkeypatch.setattr("mailhub.plugins.policies.qiuzhao.parser.requests.post", boom)
    event = parse_mail(mail, settings)
    assert event is not None
    assert event.start_at.startswith("2026-08-25T10:00")

    lifecycle = json.loads(
        (tmp_path / "logs" / "mail_lifecycle.jsonl").read_text(encoding="utf-8").strip()
    )
    parse_stage = next(s for s in lifecycle["stages"] if s["name"] == "parse")
    assert parse_stage["engine"] == "llm_then_heuristic"
    assert parse_stage["result"] == "error_fallback"

    record = json.loads(
        (tmp_path / "logs" / "llm_io.jsonl").read_text(encoding="utf-8").strip()
    )
    assert record["decision"] == "error"
    assert "network down" in (record["error"] or "")
    assert record["trace_id"] == lifecycle["trace_id"]


def test_parse_mail_llm_error_falls_back_to_schedule_invite(
    tmp_path: Path, monkeypatch
):
    settings = _settings(
        tmp_path,
        llm_api_base="https://api.example.com/v1",
        llm_api_key="k",
    )
    mail = _mail(
        message_id="<booking@example.com>",
        subject="【美团】请选择面试时间",
        text="请点击链接预约面试时间。预约截止：2026年8月19日 18:00。",
    )

    monkeypatch.setattr(
        "mailhub.plugins.policies.qiuzhao.parser.requests.post",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("network down")),
    )

    event = parse_mail(mail, settings)

    assert event is not None
    assert event.event_type == "schedule_invite"
    assert event.deadline == "2026-08-19T18:00:00"


def test_build_title_deterministic_format():
    assert build_title("interview", "美团", "create") == "[面试] 美团"
    assert build_title("exam", "字节", "create") == "[笔试] 字节"
    assert build_title("assessment", "腾讯", "create") == "[测评] 腾讯"
    assert build_title("other", "某机构", "create") == "[其他] 某机构"
    # reschedule 保留学段类型
    assert build_title("interview", "美团", "reschedule") == "[面试] 美团"
    # cancel → [取消]
    assert build_title("interview", "美团", "cancel") == "[取消] 美团"
    # 公司为空 → 回退主题前 40 字，仍带类型前缀
    assert build_title("interview", "", "create", subject="快手校招面试通知") == (
        "[面试] 快手校招面试通知"
    )


def test_normalize_rewrites_subject_like_title():
    """主题原文含「笔试/面试」也必须重建为 [type] 公司，而非沿用主题。"""
    event = CandidateEvent(
        message_id="<m@qq.com>",
        subject="2027届AI Agent研发工程师笔试（0816）",
        title="2027届AI Agent研发工程师笔试（0816）",
        event_type="exam",
        action="create",
        start_at="2026-08-16T19:00:00",
        end_at="2026-08-16T21:00:00",
        company="快手",
    )
    out = normalize_event(event)
    assert out.title == "[笔试] 快手"


def test_normalize_reschedule_keeps_stage_label():
    event = CandidateEvent(
        message_id="<m@qq.com>",
        subject="【美团】面试改期通知",
        title="【美团】面试改期通知",
        event_type="interview",
        action="reschedule",
        start_at="2026-08-28T15:00:00",
        end_at="2026-08-28T16:00:00",
        company="美团",
    )
    out = normalize_event(event)
    assert out.title == "[面试] 美团"


def test_normalize_empty_company_falls_back_to_subject():
    event = CandidateEvent(
        message_id="<m@qq.com>",
        subject="2027应届生校园招聘-AI应用开发工程师面试",
        title="随便的模型标题",
        event_type="interview",
        action="create",
        start_at="2026-08-24T16:00:00",
        end_at="2026-08-24T17:00:00",
        company="",
    )
    out = normalize_event(event)
    assert out.title.startswith("[面试] ")
    assert "2027应届生校园招聘" in out.title


def test_llm_empty_company_falls_back_to_guess(tmp_path: Path, monkeypatch):
    settings = _settings(
        tmp_path,
        llm_api_base="https://api.example.com/v1",
        llm_api_key="k",
    )
    mail = _mail(
        message_id="<c@qq.com>",
        subject="【美团】面试通知",
        text="面试时间：2026年8月20日 10:00，请准时参加。",
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
                                    "relevant": True,
                                    "action": "create",
                                    "stage": "confirmed",
                                    "event_type": "interview",
                                    "company": "",
                                    "start_at": "2026-08-20T10:00:00",
                                    "end_at": "2026-08-20T11:00:00",
                                    "location": "https://meeting.tencent.com/dm/test",
                                    "confidence": 0.9,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(
        "mailhub.plugins.policies.qiuzhao.parser.requests.post",
        lambda *a, **k: FakeResp(),
    )
    event = parse_mail(mail, settings)
    assert event is not None
    assert event.company == "美团"
    assert event.title == "[面试] 美团"


def test_store_cursor_and_active_event(tmp_path: Path):
    store = EventStore(tmp_path / "t.sqlite")
    assert store.get_last_uid() is None
    store.set_last_uid(42)
    assert store.get_last_uid() == 42

    eid = store.create_event(
        company="美团",
        event_type="interview",
        title="[interview] 美团",
        start_at="2026-08-21T14:00:00",
        end_at="2026-08-21T15:00:00",
        source_message_id="<notice@qq.com>",
        sinks={"calendar": "uid-1"},
    )
    found = store.find_active_event(company="美团", event_type="interview")
    assert found is not None and found.id == eid
    found2 = store.find_active_event(references=["<notice@qq.com>"])
    assert found2 is not None and found2.id == eid

    store.cancel_event(eid, "<cancel@qq.com>")
    assert store.find_active_event(company="美团") is None
    store.close()
