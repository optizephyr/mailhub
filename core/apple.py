from __future__ import annotations

import subprocess
from datetime import datetime

from .calendar_match import extract_marker_message_id
from .models import AppleEventRef, CandidateEvent

# 读回日程时用的分隔符：正文里几乎不可能出现
_FIELD_SEP = "<<M2C_FS>>"
_RECORD_SEP = "<<M2C_RS>>"
_READ_TIMEOUT_SECONDS = 90


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
    if not event.start_at or not event.end_at or not event.location:
        raise ValueError("新建日程必须包含开始时间、结束时间和地点")
    start = datetime.fromisoformat(event.start_at)
    end = datetime.fromisoformat(event.end_at)
    alarm = -abs(reminder_minutes)

    script = f'''
set calName to "{_as_escape(calendar_name)}"
set evTitle to "{_as_escape(event.title)}"
set evLoc to "{_as_escape(event.location)}"

tell application "Calendar"
  set calList to every calendar whose name is calName
  if (count of calList) is 0 then
    error "找不到名为「" & calName & "」的日历，请先运行: python3 -m core list-apple"
  end if
  set theCal to item 1 of calList
{_date_block("startDate", start)}
{_date_block("endDate", end)}
  tell theCal
    set newEvent to make new event with properties {{summary:evTitle, start date:startDate, end date:endDate, description:"", location:evLoc}}
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
    if not event.start_at or not event.end_at or not event.location:
        raise ValueError("更新日程必须包含开始时间、结束时间和地点")
    start = datetime.fromisoformat(event.start_at)
    end = datetime.fromisoformat(event.end_at)
    script = f'''
set targetUid to "{_as_escape(uid)}"
set calName to "{_as_escape(calendar_name)}"
set evTitle to "{_as_escape(event.title)}"
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
      -- 顺序很关键：改到更晚要先挪结束时间，否则中途出现 start > end 会存不进去
      if startDate >= (start date of ev) then
        set end date of ev to endDate
        set start date of ev to startDate
      else
        set start date of ev to startDate
        set end date of ev to endDate
      end if
      set description of ev to ""
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


def list_apple_events(
    calendar_name: str,
    window_start: datetime,
    window_end: datetime,
) -> list[AppleEventRef]:
    """读出指定日历在 [window_start, window_end] 内按开始时间落下的日程。"""
    script = f'''
on padTwo(n)
  set s to (n as integer) as string
  if length of s < 2 then set s to "0" & s
  return s
end padTwo

on isoOf(d)
  set y to (year of d) as string
  set mo to my padTwo((month of d) * 1)
  set dy to my padTwo(day of d)
  set hh to my padTwo(hours of d)
  set mi to my padTwo(minutes of d)
  set ss to my padTwo(seconds of d)
  return y & "-" & mo & "-" & dy & "T" & hh & ":" & mi & ":" & ss
end isoOf

set calName to "{_as_escape(calendar_name)}"
set fs to "{_FIELD_SEP}"
set rs to "{_RECORD_SEP}"
{_date_block("rangeStart", window_start)}
{_date_block("rangeEnd", window_end)}

tell application "Calendar"
  set calList to every calendar whose name is calName
  if (count of calList) is 0 then
    error "找不到名为「" & calName & "」的日历，请先运行: python3 -m core list-apple"
  end if
  set theCal to item 1 of calList
  tell theCal
    set evs to (every event whose start date >= rangeStart and start date <= rangeEnd)
  end tell
  set out to ""
  repeat with ev in evs
    set sumTxt to ""
    try
      set sumTxt to (summary of ev) as string
    end try
    set descTxt to ""
    try
      set descTxt to (description of ev) as string
    end try
    set out to out & ((uid of ev) as string) & fs & sumTxt & fs & my isoOf(start date of ev) & fs & my isoOf(end date of ev) & fs & descTxt & rs
  end repeat
  return out
end tell
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=_READ_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"读取 Apple 日历超时（{_READ_TIMEOUT_SECONDS}s），可缩小 CALENDAR_SCAN_DAYS"
        ) from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"读取 Apple 日历失败: {err}")

    events: list[AppleEventRef] = []
    for record in (result.stdout or "").split(_RECORD_SEP):
        if not record.strip():
            continue
        fields = record.split(_FIELD_SEP)
        if len(fields) < 5:
            continue
        uid, summary, start_at, end_at = (f.strip() for f in fields[:4])
        description = _FIELD_SEP.join(fields[4:])
        if not uid:
            continue
        events.append(
            AppleEventRef(
                uid=uid,
                summary=summary,
                start_at=start_at,
                end_at=end_at,
                marker_message_id=extract_marker_message_id(description),
            )
        )
    return events


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
