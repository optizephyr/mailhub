from __future__ import annotations

import re
from datetime import date, timedelta
from email.header import decode_header, make_header
from typing import Iterable, Optional
from urllib.parse import quote, unquote

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


def _source_key(folder: str, uidvalidity: int, uid: str) -> str:
    if not folder or uidvalidity <= 0 or not uid:
        return ""
    return f"imap:{quote(folder, safe='')}:{uidvalidity}:{uid}"


def _parse_source_key(value: str) -> Optional[tuple[str, int, str]]:
    parts = (value or "").split(":", 3)
    if len(parts) != 4 or parts[0] != "imap":
        return None
    try:
        uidvalidity = int(parts[2])
    except ValueError:
        return None
    folder, uid = unquote(parts[1]), parts[3]
    if not folder or uidvalidity <= 0 or not uid:
        return None
    return folder, uidvalidity, uid


def _mailbox_identity(mailbox) -> tuple[str, int]:
    folder_manager = getattr(mailbox, "folder", None)
    if folder_manager is None:
        return "INBOX", 0
    folder = folder_manager.get() or "INBOX"
    try:
        status = folder_manager.status(folder, ("UIDVALIDITY",))
    except Exception:
        return folder, 0
    return folder, int(status.get("UIDVALIDITY") or 0)


def _to_mail_message(
    msg: ImapMailMessage,
    source_id: str,
    *,
    folder: str = "",
    uidvalidity: int = 0,
) -> MailMessage:
    refs = _header_values(
        msg, "In-Reply-To", "References", "in-reply-to", "references"
    )
    return MailMessage(
        source=SourceRef(
            source_id=source_id,
            message_id=_message_id_of(msg),
            source_key=_source_key(folder, uidvalidity, str(msg.uid or "")),
        ),
        subject=_decode(msg.subject),
        sender=_decode(msg.from_) if msg.from_ else "",
        sent_at=msg.date.isoformat() if msg.date else None,
        text=msg.text or "",
        html=msg.html or "",
        references=refs,
    )


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
            folder, uidvalidity = _mailbox_identity(mailbox)
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

                messages_out.append(
                    _to_mail_message(
                        msg,
                        self.source_id,
                        folder=folder,
                        uidvalidity=uidvalidity,
                    )
                )

        next_checkpoint = str(max_uid) if max_uid else checkpoint
        return IngestBatch(messages=messages_out, next_checkpoint=next_checkpoint)

    def fetch_by_message_ids(self, message_ids: Iterable[str]) -> list[MailMessage]:
        """Fetch originals by client-side Message-ID matching.

        QQ IMAP accepts ``SEARCH HEADER Message-ID`` but may return unrelated
        recent messages. Scan a bounded date window and compare the actual
        header locally instead.
        """
        found: list[MailMessage] = []
        wanted = {
            message_id.strip()
            for message_id in message_ids
            if message_id.strip()
            and not message_id.strip().startswith("local-")
        }
        if not wanted:
            return found
        with MailBox(self.host).login(
            self.email_addr, self.auth_code, initial_folder="INBOX"
        ) as mailbox:
            folder, uidvalidity = _mailbox_identity(mailbox)
            since = date.today() - timedelta(days=max(self.lookback_days, 365))
            messages = mailbox.fetch(
                AND(date_gte=since),
                reverse=True,
                mark_seen=False,
            )
            for msg in messages:
                if _message_id_of(msg) not in wanted:
                    continue
                found.append(
                    _to_mail_message(
                        msg,
                        self.source_id,
                        folder=folder,
                        uidvalidity=uidvalidity,
                    )
                )
                if len(found) == len(wanted):
                    break
        return found

    def fetch_by_source_refs(self, refs: Iterable[SourceRef]) -> list[MailMessage]:
        """Fetch originals by native IMAP identity, with Message-ID fallback."""
        pending = [ref for ref in refs if ref.source_id == self.source_id]
        if not pending:
            return []
        found: dict[tuple[str, str], MailMessage] = {}
        with MailBox(self.host).login(
            self.email_addr, self.auth_code, initial_folder="INBOX"
        ) as mailbox:
            current_folder, current_uidvalidity = _mailbox_identity(mailbox)
            for ref in pending:
                parsed = _parse_source_key(ref.source_key)
                if parsed is None:
                    continue
                folder, uidvalidity, uid = parsed
                if folder != current_folder or uidvalidity != current_uidvalidity:
                    continue
                for msg in mailbox.fetch(
                    AND(uid=U(uid)),
                    reverse=False,
                    limit=1,
                    mark_seen=False,
                ):
                    if _message_id_of(msg) != ref.message_id:
                        break
                    message = _to_mail_message(
                        msg,
                        self.source_id,
                        folder=current_folder,
                        uidvalidity=current_uidvalidity,
                    )
                    found[(ref.source_id, ref.message_id)] = message
                    break

        missing_ids = [
            ref.message_id
            for ref in pending
            if (ref.source_id, ref.message_id) not in found
        ]
        for message in self.fetch_by_message_ids(missing_ids):
            found[(message.source.source_id, message.source.message_id)] = message
        return list(found.values())
