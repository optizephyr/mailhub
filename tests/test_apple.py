from types import SimpleNamespace

import pytest

from mail_to_calendar import apple
from mail_to_calendar.models import CandidateEvent


def _event(**kwargs) -> CandidateEvent:
    fields = {
        "message_id": "<mail@qq.com>",
        "subject": "面试通知",
        "title": "[面试] 美团",
        "event_type": "interview",
        "start_at": "2026-08-20T10:00:00",
        "end_at": "2026-08-20T11:00:00",
        "location": "https://meeting.tencent.com/dm/test",
        "description": "不应写入日历",
    }
    fields.update(kwargs)
    return CandidateEvent(**fields)


def test_create_apple_event_uses_empty_description(monkeypatch):
    scripts: list[str] = []

    def fake_run(command, **_kwargs):
        scripts.append(command[-1])
        return SimpleNamespace(returncode=0, stdout="uid-1\n", stderr="")

    monkeypatch.setattr(apple.subprocess, "run", fake_run)

    assert apple.create_apple_event(_event(), "日历", 30) == "uid-1"
    assert 'description:""' in scripts[0]
    assert "不应写入日历" not in scripts[0]
    assert "[mail-to-calendar]" not in scripts[0]


def test_create_apple_event_requires_location():
    with pytest.raises(ValueError, match="地点"):
        apple.create_apple_event(_event(location=""), "日历", 30)
