from pathlib import Path

from mailhub.contracts.messages import MailMessage, SourceRef
from mailhub.plugins.dispatch.calendar.planner import CalendarPlanner
from mailhub.plugins.dispatch.reminders.planner import (
    ACTION_CREATE,
    RemindersPlanner,
)
from mailhub.plugins.policies.qiuzhao import candidate_to_resolved
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent
from mailhub.runtime.config import Settings
from mailhub.store.sqlite import EventStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
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
        calendar_scan_days=0,
    )


def _resolved(event: CandidateEvent) -> MailMessage:
    message = MailMessage(
        source=SourceRef(source_id="qq.default", message_id=event.message_id),
        subject=event.subject,
        sender="hr@x.com",
        sent_at=None,
        text="",
        html="",
    )
    return candidate_to_resolved(event, message)


def test_window_task_goes_to_reminders_not_calendar(tmp_path: Path):
    store = EventStore(tmp_path / "db.sqlite")
    settings = _settings(tmp_path)
    session: list = []
    event = CandidateEvent(
        message_id="<a@qq.com>",
        subject="测评",
        title="[测评] 京东",
        event_type="assessment",
        action="create",
        end_at="2026-08-21T03:00:00",
        company="京东",
        time_precision="window",
    )
    resolved = _resolved(event)
    cal = CalendarPlanner(store, settings, session, dry_run=True, source_id="qq.default")
    rem = RemindersPlanner(store, settings, session, dry_run=True, source_id="qq.default")
    assert cal.plan(resolved) == []
    reqs = rem.plan(resolved)
    assert len(reqs) == 1
    assert reqs[0].type == ACTION_CREATE
    assert reqs[0].payload["result"] == "would_create"
    store.close()


def test_fixed_slot_skips_reminders_planner(tmp_path: Path):
    store = EventStore(tmp_path / "db.sqlite")
    settings = _settings(tmp_path)
    session: list = []
    event = CandidateEvent(
        message_id="<b@qq.com>",
        subject="面试",
        title="[面试] 美团",
        event_type="interview",
        action="create",
        start_at="2026-08-25T10:00:00",
        end_at="2026-08-25T11:00:00",
        location="https://meeting.tencent.com/x",
        company="美团",
        time_precision="fixed",
    )
    resolved = _resolved(event)
    rem = RemindersPlanner(store, settings, session, dry_run=True, source_id="qq.default")
    assert rem.plan(resolved) == []
    store.close()
