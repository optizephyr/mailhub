from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.header import decode_header, make_header
from typing import Iterable, Optional

from bs4 import BeautifulSoup
from imap_tools import AND, MailBox, MailMessage


RECRUIT_KEYWORDS = (
    "面试",
    "笔试",
    "测评",
    "机考",
    "线上面试",
    "视频面试",
    "面试邀约",
    "面试通知",
    "笔试通知",
    "在线测评",
    "能力测评",
    "取消面试",
    "面试取消",
    "改期",
    "时间调整",
    "assessment",
    "interview",
    "online test",
    "written test",
    "hirevue",
    "cancelled",
    "reschedule",
    "牛客",
    "赛码",
    "北森",
)


@dataclass
class MailItem:
    message_id: str
    subject: str
    from_: str
    date: Optional[str]
    text: str
    html: str
    uid: int = 0
    references: list[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        if self.text.strip():
            return self.text.strip()
        if self.html.strip():
            soup = BeautifulSoup(self.html, "lxml")
            return soup.get_text("\n", strip=True)
        return ""


@dataclass
class FetchResult:
    mails: list[MailItem]
    max_uid: int
    mode: str  # incremental | full
    examined: int = 0


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _header_values(msg: MailMessage, *names: str) -> list[str]:
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
    # normalize to angle-bracket form when bare
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


def _message_id_of(msg: MailMessage) -> str:
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


def looks_like_recruit_mail(subject: str, body: str) -> bool:
    blob = f"{subject}\n{body}"
    lower_map = blob.lower()
    for kw in RECRUIT_KEYWORDS:
        if kw.lower() in lower_map or kw in blob:
            return True
    return False


def fetch_mails(
    email_addr: str,
    auth_code: str,
    *,
    lookback_days: int = 14,
    limit: int = 80,
    since_uid: Optional[int] = None,
    full: bool = False,
) -> FetchResult:
    """
    Incremental: fetch UIDs > since_uid (all newer mails), return recruit hits.
    Full / first run: lookback by date with limit.
    Cursor bump uses max UID among *examined* messages (including non-recruit).
    """
    items: list[MailItem] = []
    examined = 0
    max_uid = since_uid or 0
    use_incremental = bool(since_uid) and not full
    mode = "incremental" if use_incremental else "full"

    with MailBox("imap.qq.com").login(email_addr, auth_code, initial_folder="INBOX") as mailbox:
        if use_incremental:
            criteria = AND(uid=f"{int(since_uid) + 1}:*")
            messages: Iterable[MailMessage] = mailbox.fetch(
                criteria,
                reverse=False,
                mark_seen=False,
            )
        else:
            since = date.today() - timedelta(days=lookback_days)
            messages = mailbox.fetch(
                AND(date_gte=since),
                reverse=True,
                limit=limit,
                mark_seen=False,
            )

        for msg in messages:
            examined += 1
            try:
                uid = int(msg.uid)
            except (TypeError, ValueError):
                uid = 0
            if uid > max_uid:
                max_uid = uid

            subject = _decode(msg.subject)
            text = msg.text or ""
            html = msg.html or ""
            if not looks_like_recruit_mail(subject, text or html):
                continue

            refs = _header_values(msg, "In-Reply-To", "References", "in-reply-to", "references")
            items.append(
                MailItem(
                    message_id=_message_id_of(msg),
                    subject=subject,
                    from_=_decode(msg.from_) if msg.from_ else "",
                    date=msg.date.isoformat() if msg.date else None,
                    text=text,
                    html=html,
                    uid=uid,
                    references=refs,
                )
            )

    return FetchResult(mails=items, max_uid=max_uid, mode=mode, examined=examined)


# backward-compatible alias
def fetch_recent_mails(
    email_addr: str,
    auth_code: str,
    lookback_days: int = 14,
    limit: int = 80,
) -> list[MailItem]:
    return fetch_mails(
        email_addr,
        auth_code,
        lookback_days=lookback_days,
        limit=limit,
        full=True,
    ).mails
