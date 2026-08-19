from __future__ import annotations

import re
from datetime import date, timedelta
from email.header import decode_header, make_header
from typing import Iterable, Optional

from imap_tools import AND, MailBox, MailMessage as ImapMailMessage, U

from mailhub.contracts.messages import IngestBatch, MailMessage, SourceRef

DEFAULT_SOURCE_ID = "qq.default"


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _header_values(msg: ImapMailMessage, *names: str) -> list[str]:
    out: list[str] = []
    for name in names:
        raw = msg.headers.get(name) or msg.headers.get(name.lower()) or ""
        if isinstance(raw, (list, tuple)):
            parts = [str(x) for x in raw]
        elif raw:
            parts = [str(raw)]
        else:
            continue
        for part in parts:
            out.extend(re.findall(r"<[^>]+>|[\w.+-]+@[\w.-]+", part))
    normalized = []
    for item in out:
        item = item.strip()
        if not item:
            continue
        if not item.startswith("<"):
            item = f"<{item}>"
        if item not in normalized:
            normalized.append(item)
    return normalized


def _message_id_of(msg: ImapMailMessage) -> str:
    raw_mid = msg.headers.get("message-id") or msg.headers.get("Message-ID") or ""
    if isinstance(raw_mid, (list, tuple)):
        message_id = str(raw_mid[0] if raw_mid else "").strip()
    else:
        message_id = str(raw_mid).strip()
    if message_id:
        return message_id
    subject = _decode(msg.subject)
    slug = re.sub(r"\s+", "", subject)[:40]
    return f"local-{msg.uid}-{slug}"


class QqImapSource:
    """QQ IMAP ingest. Checkpoint is opaque UID string. No topic filtering."""

    def __init__(
        self,
        email_addr: str,
        auth_code: str,
        *,
        source_id: str = DEFAULT_SOURCE_ID,
        lookback_days: int = 14,
        limit: int = 80,
        full: bool = False,
        host: str = "imap.qq.com",
    ) -> None:
        self.email_addr = email_addr
        self.auth_code = auth_code
        self.source_id = source_id
        self.lookback_days = lookback_days
        self.limit = limit
        self.full = full
        self.host = host

    def fetch(self, checkpoint: Optional[str]) -> IngestBatch:
        since_uid: Optional[int] = None
        if checkpoint and not self.full:
            try:
                since_uid = int(checkpoint)
            except ValueError:
                since_uid = None

        messages_out: list[MailMessage] = []
        max_uid = since_uid or 0
        use_incremental = bool(since_uid) and not self.full

        with MailBox(self.host).login(
            self.email_addr, self.auth_code, initial_folder="INBOX"
        ) as mailbox:
            if use_incremental:
                # IMAP UID <n>:* always includes the mailbox's last message,
                # even when that UID is below n. Filter those out below.
                criteria = AND(uid=U(str(int(since_uid) + 1), "*"))
                messages: Iterable[ImapMailMessage] = mailbox.fetch(
                    criteria,
                    reverse=False,
                    mark_seen=False,
                )
            else:
                since = date.today() - timedelta(days=self.lookback_days)
                messages = mailbox.fetch(
                    AND(date_gte=since),
                    reverse=True,
                    limit=self.limit,
                    mark_seen=False,
                )

            for msg in messages:
                try:
                    uid = int(msg.uid)
                except (TypeError, ValueError):
                    uid = 0
                if uid > max_uid:
                    max_uid = uid
                if use_incremental and since_uid is not None and uid <= since_uid:
                    continue

                subject = _decode(msg.subject)
                text = msg.text or ""
                html = msg.html or ""
                refs = _header_values(
                    msg, "In-Reply-To", "References", "in-reply-to", "references"
                )
                mid = _message_id_of(msg)
                messages_out.append(
                    MailMessage(
                        source=SourceRef(source_id=self.source_id, message_id=mid),
                        subject=subject,
                        sender=_decode(msg.from_) if msg.from_ else "",
                        sent_at=msg.date.isoformat() if msg.date else None,
                        text=text,
                        html=html,
                        references=refs,
                    )
                )

        next_checkpoint = str(max_uid) if max_uid else checkpoint
        return IngestBatch(messages=messages_out, next_checkpoint=next_checkpoint)
