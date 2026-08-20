from __future__ import annotations

from pathlib import Path

import requests
import pytest

from mailhub.contracts.messages import IngestBatch, MailMessage, SourceRef
from mailhub.plugins.policies.qiuzhao import QiuzhaoResolver
from mailhub.runtime.config import Settings
from mailhub.runtime.context import RunContext
from mailhub.runtime.engine import run_once
from mailhub.store.sqlite import EventStore
from tests.eml_loader import EMAIL_EXAMPLE_DIR, load_eml


class OneBatchSource:
    def __init__(self, message: MailMessage) -> None:
        self.message = message

    def fetch(self, _checkpoint: str | None) -> IngestBatch:
        return IngestBatch(messages=[self.message], next_checkpoint="1")


def _message_from_fixture(filename: str) -> MailMessage:
    mail = load_eml(EMAIL_EXAMPLE_DIR / filename)
    return MailMessage(
        source=SourceRef(source_id="qq.default", message_id=mail.message_id),
        subject=mail.subject,
        sender=mail.from_,
        sent_at=mail.date,
        text=mail.text,
        html=mail.html,
        references=list(mail.references),
    )


def test_schedule_invite_fixture_pushes_bark_once(
    tmp_path: Path, monkeypatch
) -> None:
    message = _message_from_fixture("【阿里巴巴校园招聘】阿里云面试邀约.eml")
    settings = Settings(
        data_dir=tmp_path,
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
        bark_key="device-key",
        bark_server_url="https://bark.example.com",
    )
    store = EventStore(tmp_path / "synced.sqlite")
    calls: list[tuple[str, dict, int]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"code": 200, "message": "success"}

    def post(url: str, *, json: dict, timeout: int) -> Response:
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setattr(requests, "post", post)
    ctx = RunContext(
        run_id="test-run",
        dry_run=False,
        full=True,
        source=OneBatchSource(message),
        resolver=QiuzhaoResolver(settings),
        planners=[],
        handlers={},
        store=store,
        source_id="qq.default",
        lifecycle_log_path=settings.lifecycle_log_path,
        extras={"settings": settings},
    )

    result = run_once(ctx)

    assert result.failed == []
    assert result.action_count == 1
    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "https://bark.example.com/push"
    assert timeout == 10
    assert payload["device_key"] == "device-key"
    assert "阿里" in payload["title"]
    assert "请预约" in payload["title"]
    assert "2026-04-08 18:00" in payload["body"]
    assert payload["url"].startswith("http")
    assert store.already_processed(message.message_id, "qq.default")
    store.close()


def test_schedule_invite_dry_run_previews_without_contacting_bark(
    tmp_path: Path, monkeypatch
) -> None:
    message = _message_from_fixture("美团校园招聘-面试邀请.eml")
    settings = Settings(
        data_dir=tmp_path,
        calendar_name="秋招",
        calendar_scan_days=0,
        bark_key="device-key",
        bark_server_url="https://bark.example.com",
    )
    store = EventStore(tmp_path / "synced.sqlite")
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("dry-run 不应请求 Bark 服务器")
        ),
    )
    ctx = RunContext(
        run_id="test-dry-run",
        dry_run=True,
        full=True,
        source=OneBatchSource(message),
        resolver=QiuzhaoResolver(settings),
        planners=[],
        handlers={},
        store=store,
        source_id="qq.default",
        lifecycle_log_path=settings.lifecycle_log_path,
        extras={"settings": settings},
    )

    result = run_once(ctx)

    assert result.failed == []
    assert result.action_count == 1
    assert result.dry_run_reports[0]["apply"] == "would_push"
    assert result.dry_run_reports[0]["event"]["event_type"] == "schedule_invite"
    assert "美团" in result.dry_run_reports[0]["push"]["title"]
    assert "请预约" in result.dry_run_reports[0]["push"]["title"]
    assert "2026-04-16 20:21" in result.dry_run_reports[0]["push"]["body"]
    assert result.dry_run_reports[0]["push"]["url"].startswith("http")
    assert not store.already_processed(message.message_id, "qq.default")
    store.close()


@pytest.mark.parametrize(
    "filename",
    [
        "腾讯校园招聘——校招面试邀请函.eml",
        "【京东校招】2027_JDS测评通知.eml",
    ],
)
def test_other_resolved_mail_does_not_push_bark(
    filename: str, tmp_path: Path, monkeypatch
) -> None:
    message = _message_from_fixture(filename)
    settings = Settings(
        data_dir=tmp_path,
        calendar_name="秋招",
        reminders_list="秋招提醒",
        calendar_scan_days=0,
        bark_key="device-key",
        bark_server_url="https://bark.example.com",
    )
    store = EventStore(tmp_path / "synced.sqlite")
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("非 schedule_invite 不应请求 Bark")
        ),
    )
    ctx = RunContext(
        run_id="test-other-mail",
        dry_run=True,
        full=True,
        source=OneBatchSource(message),
        resolver=QiuzhaoResolver(settings),
        planners=[],
        handlers={},
        store=store,
        source_id="qq.default",
        lifecycle_log_path=settings.lifecycle_log_path,
        extras={"settings": settings},
    )

    result = run_once(ctx)

    assert result.failed == []
    assert result.dry_run_reports[0]["apply"] == "would_create"
    assert "push" not in result.dry_run_reports[0]
    store.close()


def test_schedule_invite_does_not_push_when_bark_is_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    message = _message_from_fixture("美团校园招聘-面试邀请.eml")
    settings = Settings(data_dir=tmp_path, calendar_scan_days=0)
    store = EventStore(tmp_path / "synced.sqlite")
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("未启用 Bark 时不应请求服务器")
        ),
    )
    ctx = RunContext(
        run_id="test-disabled",
        dry_run=False,
        full=True,
        source=OneBatchSource(message),
        resolver=QiuzhaoResolver(settings),
        planners=[],
        handlers={},
        store=store,
        source_id="qq.default",
        lifecycle_log_path=settings.lifecycle_log_path,
        extras={"settings": settings},
    )

    result = run_once(ctx)

    assert result.failed == []
    assert result.action_count == 0
    assert result.ignored_count == 1
    store.close()


def test_default_appearance_time_uses_calendar_instead_of_bark(
    tmp_path: Path, monkeypatch
) -> None:
    message = MailMessage(
        source=SourceRef(source_id="qq.default", message_id="<default-slot@example.com>"),
        subject="【美团】请选择面试时间",
        sender="campus@example.com",
        sent_at="2026-08-18T09:00:00+08:00",
        text=(
            "请于2026年8月19日 18:00前选择面试时间；"
            "逾期将安排在2026年8月20日 10:00。"
            "会议链接 https://meeting.example.com/default"
        ),
        html="",
    )
    settings = Settings(
        data_dir=tmp_path,
        calendar_name="秋招",
        calendar_scan_days=0,
        bark_key="device-key",
        bark_server_url="https://bark.example.com",
    )
    store = EventStore(tmp_path / "synced.sqlite")
    monkeypatch.setattr(
        requests,
        "post",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("保底出场时间应走日历，不应请求 Bark")
        ),
    )
    ctx = RunContext(
        run_id="test-default-slot",
        dry_run=True,
        full=True,
        source=OneBatchSource(message),
        resolver=QiuzhaoResolver(settings),
        planners=[],
        handlers={},
        store=store,
        source_id="qq.default",
        lifecycle_log_path=settings.lifecycle_log_path,
        extras={"settings": settings},
    )

    result = run_once(ctx)

    assert result.failed == []
    assert result.dry_run_reports[0]["apply"] == "would_create"
    assert result.dry_run_reports[0]["event"]["start_at"] == "2026-08-20T10:00:00"
    assert "push" not in result.dry_run_reports[0]
    store.close()


def test_failed_bark_push_leaves_message_unprocessed(
    tmp_path: Path, monkeypatch
) -> None:
    message = _message_from_fixture("美团校园招聘-面试邀请.eml")
    settings = Settings(
        data_dir=tmp_path,
        calendar_scan_days=0,
        bark_key="device-key",
        bark_server_url="https://bark.example.com",
    )
    store = EventStore(tmp_path / "synced.sqlite")

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"code": 500, "message": "temporary failure"}

    monkeypatch.setattr(requests, "post", lambda *_a, **_k: Response())
    ctx = RunContext(
        run_id="test-failed-push",
        dry_run=False,
        full=True,
        source=OneBatchSource(message),
        resolver=QiuzhaoResolver(settings),
        planners=[],
        handlers={},
        store=store,
        source_id="qq.default",
        lifecycle_log_path=settings.lifecycle_log_path,
        extras={"settings": settings},
    )

    result = run_once(ctx)

    assert result.failed_count == 1
    assert "temporary failure" in result.failed[0]
    assert not store.already_processed(message.message_id, "qq.default")
    store.close()
