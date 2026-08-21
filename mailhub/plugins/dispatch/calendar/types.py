from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CalendarEventRef:
    # Historical name: this is the CalDAV resource href, not iCalendar UID.
    uid: str
    summary: str
    start_at: str = ""
    end_at: str = ""
    marker_message_id: str = ""
    item_uid: str = ""
