from types import SimpleNamespace

from mailhub.plugins.dispatch.apple_reminders import reminder_io as reminders
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent


def _event(**kwargs) -> CandidateEvent:
    fields = {
        "message_id": "<mail@qq.com>",
        "subject": "测评通知",
        "title": "[测评] 京东",
        "event_type": "assessment",
        "action": "create",
        "start_at": "",
        "end_at": "2026-08-21T03:00:00",
        "location": "",
        "meeting_url": "https://example.com/a",
        "time_precision": "window",
    }
    fields.update(kwargs)
    return CandidateEvent(**fields)


def test_create_apple_reminder_sets_due_and_url(monkeypatch):
    scripts: list[str] = []

    def fake_run(command, **_kwargs):
        scripts.append(command[-1])
        return SimpleNamespace(returncode=0, stdout="x-apple-reminder://abc\n", stderr="")

    monkeypatch.setattr(reminders.subprocess, "run", fake_run)
    assert reminders.create_apple_reminder(_event(), "提醒事项") == "x-apple-reminder://abc"
    script = scripts[0]
    assert "Reminders" in script
    assert "[测评] 京东" in script
    assert "https://example.com/a" in script
    assert "set year of dueDate to 2026" in script
    assert "不应出现正文" not in script


def test_create_apple_reminder_without_due(monkeypatch):
    scripts: list[str] = []

    def fake_run(command, **_kwargs):
        scripts.append(command[-1])
        return SimpleNamespace(returncode=0, stdout="id-1\n", stderr="")

    monkeypatch.setattr(reminders.subprocess, "run", fake_run)
    reminders.create_apple_reminder(_event(end_at="", start_at=""), "提醒事项")
    assert "dueDate" not in scripts[0]
