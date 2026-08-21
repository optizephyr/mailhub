from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional


def parse_mail_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_stale_mail(incoming_sent_at: Optional[str], stored_sent_at: Optional[str]) -> bool:
    """True when incoming mail is strictly older than the row's last write.

    Equal timestamps still apply, so same-Date 改期/取消不会被挡住。
    Incoming without a stamp cannot overwrite a stamped row.
    """
    incoming = parse_mail_datetime(incoming_sent_at)
    stored = parse_mail_datetime(stored_sent_at)
    if incoming is None:
        return stored is not None
    if stored is None:
        return False
    return incoming < stored
