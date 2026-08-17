from __future__ import annotations

import subprocess
from datetime import datetime

from .models import CandidateEvent


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


def create_apple_event(
    event: CandidateEvent,
    calendar_name: str,
    reminder_minutes: int,
) -> str:
    """Create an event; return Calendar event uid."""
    start = datetime.fromisoformat(event.start_at)
    end = datetime.fromisoformat(event.end_at)
    alarm = -abs(reminder_minutes)

    script = f'''
set calName to "{_as_escape(calendar_name)}"
set evTitle to "{_as_escape(event.title)}"
set evDesc to "{_as_escape(event.description[:1200])}"
set evLoc to "{_as_escape(event.location)}"

tell application "Calendar"
  set calList to every calendar whose name is calName
  if (count of calList) is 0 then
    error "找不到名为「" & calName & "」的日历，请先运行: python3 -m mail_to_calendar list-apple"
  end if
  set theCal to item 1 of calList
{_date_block("startDate", start)}
{_date_block("endDate", end)}
  tell theCal
    set newEvent to make new event with properties {{summary:evTitle, start date:startDate, end date:endDate, description:evDesc, location:evLoc}}
    try
      make new display alarm at end of display alarms of newEvent with properties {{trigger interval:{alarm}}}
    end try
    return uid of newEvent
  end tell
end tell
'''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"写入 Apple 日历失败: {err}")
    uid = (result.stdout or "").strip()
    if not uid:
        raise RuntimeError("写入 Apple 日历成功但未返回 uid")
    return uid


def update_apple_event(
    uid: str,
    event: CandidateEvent,
    calendar_name: str,
) -> None:
    start = datetime.fromisoformat(event.start_at)
    end = datetime.fromisoformat(event.end_at)
    script = f'''
set targetUid to "{_as_escape(uid)}"
set calName to "{_as_escape(calendar_name)}"
set evTitle to "{_as_escape(event.title)}"
set evDesc to "{_as_escape(event.description[:1200])}"
set evLoc to "{_as_escape(event.location)}"

tell application "Calendar"
{_date_block("startDate", start)}
{_date_block("endDate", end)}
  set found to false
  repeat with c in calendars
    set evs to (every event of c whose uid is targetUid)
    if (count of evs) > 0 then
      set ev to item 1 of evs
      set summary of ev to evTitle
      set start date of ev to startDate
      set end date of ev to endDate
      set description of ev to evDesc
      set location of ev to evLoc
      set found to true
      exit repeat
    end if
  end repeat
  if not found then error "找不到 uid=" & targetUid & " 的 Apple 日程"
end tell
'''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"更新 Apple 日历失败: {err}")


def delete_apple_event(uid: str) -> None:
    script = f'''
set targetUid to "{_as_escape(uid)}"
tell application "Calendar"
  set found to false
  repeat with c in calendars
    set evs to (every event of c whose uid is targetUid)
    if (count of evs) > 0 then
      delete (item 1 of evs)
      set found to true
      exit repeat
    end if
  end repeat
  if not found then error "找不到 uid=" & targetUid & " 的 Apple 日程"
end tell
'''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"删除 Apple 日历失败: {err}")


def list_apple_calendars() -> list[str]:
    script = '''
tell application "Calendar"
  set names to {}
  repeat with c in calendars
    set end of names to name of c
  end repeat
  set AppleScript's text item delimiters to linefeed
  return names as text
end tell
'''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
