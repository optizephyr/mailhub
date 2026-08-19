from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AppleReminderRef:
    uid: str
    name: str
    due_at: str = ""
