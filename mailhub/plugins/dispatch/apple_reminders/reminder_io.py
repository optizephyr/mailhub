from __future__ import annotations

import subprocess
from datetime import datetime
from typing import Optional

from mailhub.plugins.policies.qiuzhao.types import CandidateEvent


def _as_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _date_block(var: str, dt: datetime) -> str:
    return f"""
  set {var} to current date
  set year of {var} to {dt.year}
  set month of {var} to {dt.month}
  set day of {var} to {dt.day}
  set hours of {var} to {dt.hour}
  set minutes of {var} to {dt.minute}
  set seconds of {var} to 0
"""


def _due_datetime(event: CandidateEvent) -> Optional[datetime]:
    raw = event.end_at or event.start_at
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def _body_text(event: CandidateEvent) -> str:
    return (event.meeting_url or "").strip()


def _permission_hint(err: str) -> str:
    lower = err.lower()
    if "not authorized" in lower or "不允许" in err or "not allowed" in lower:
        return (
            f"{err}；请到系统设置 → 隐私与安全性 → 自动化 / 提醒事项，"
            "允许终端或 Python 访问「提醒事项」"
        )
    return err


def _run(script: str, *, fail_prefix: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"{fail_prefix}: {_permission_hint(err)}")
    return (result.stdout or "").strip()


def create_apple_reminder(event: CandidateEvent, list_name: str) -> str:
    due = _due_datetime(event)
    due_block = _date_block("dueDate", due) if due else ""
    due_assign = ""
    if due:
        due_assign = """
    set due date of newRem to dueDate
    try
      set remind me date of newRem to dueDate
    end try
"""
    script = f'''
set listName to "{_as_escape(list_name)}"
set remTitle to "{_as_escape(event.title)}"
set remBody to "{_as_escape(_body_text(event))}"
{due_block}

tell application "Reminders"
  set listHits to (every list whose name is listName)
  if (count of listHits) is 0 then
    error "找不到名为「" & listName & "」的提醒事项列表，请先运行: python3 -m mailhub list-reminders"
  end if
  tell item 1 of listHits
    set newRem to make new reminder with properties {{name:remTitle, body:remBody}}
{due_assign}
    return id of newRem
  end tell
end tell
'''
    uid = _run(script, fail_prefix="写入 Apple 提醒事项失败")
    if not uid:
        raise RuntimeError("写入 Apple 提醒事项成功但未返回 id")
    return uid


def update_apple_reminder(uid: str, event: CandidateEvent, list_name: str) -> None:
    due = _due_datetime(event)
    due_block = _date_block("dueDate", due) if due else ""
    due_assign = (
        """
      set due date of rem to dueDate
      try
        set remind me date of rem to dueDate
      end try
"""
        if due
        else ""
    )
    script = f'''
set targetId to "{_as_escape(uid)}"
set remTitle to "{_as_escape(event.title)}"
set remBody to "{_as_escape(_body_text(event))}"
{due_block}

tell application "Reminders"
  set found to false
  try
    set rem to reminder id targetId
    set name of rem to remTitle
    set body of rem to remBody
{due_assign}
    set found to true
  end try
  if not found then error "找不到 id=" & targetId & " 的提醒事项"
end tell
'''
    _run(script, fail_prefix="更新 Apple 提醒事项失败")


def delete_apple_reminder(uid: str) -> None:
    script = f'''
set targetId to "{_as_escape(uid)}"
tell application "Reminders"
  set found to false
  try
    delete (reminder id targetId)
    set found to true
  end try
  if not found then error "找不到 id=" & targetId & " 的提醒事项"
end tell
'''
    _run(script, fail_prefix="删除 Apple 提醒事项失败")


def list_apple_reminder_lists() -> list[str]:
    script = '''
tell application "Reminders"
  set names to {}
  repeat with lst in lists
    set end of names to name of lst
  end repeat
  set AppleScript's text item delimiters to linefeed
  return names as text
end tell
'''
    out = _run(script, fail_prefix="读取提醒事项列表失败")
    return [line.strip() for line in out.splitlines() if line.strip()]
