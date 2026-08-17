"""Load .eml fixtures into MailItem for rule-engine corpus tests."""

from __future__ import annotations

import email
import json
from email import policy
from pathlib import Path
from typing import Any

from core.mail_qq import MailItem

EMAIL_EXAMPLE_DIR = Path(__file__).resolve().parent / "fixtures" / "email_corpus"
LABELS_PATH = EMAIL_EXAMPLE_DIR / "labels.json"


def load_eml(path: Path) -> MailItem:
    with path.open("rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)

    subject = str(msg["subject"] or "")
    from_ = str(msg["from"] or "")
    message_id = str(msg["message-id"] or f"<local-{path.name}>").strip()

    text = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not text:
                try:
                    text = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        text = payload.decode(errors="replace")
            elif ctype == "text/html" and not html:
                try:
                    html = part.get_content()
                except Exception:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        html = payload.decode(errors="replace")
    else:
        ctype = msg.get_content_type()
        try:
            content = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            content = (
                payload.decode(errors="replace") if isinstance(payload, bytes) else str(payload or "")
            )
        if ctype == "text/html":
            html = content if isinstance(content, str) else str(content)
        else:
            text = content if isinstance(content, str) else str(content)

    return MailItem(
        message_id=message_id,
        subject=subject,
        from_=from_,
        date=None,
        text=text or "",
        html=html or "",
    )


def load_labels(path: Path = LABELS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_labeled_cases(
    labels_path: Path = LABELS_PATH,
    example_dir: Path = EMAIL_EXAMPLE_DIR,
) -> list[tuple[dict[str, Any], MailItem]]:
    data = load_labels(labels_path)
    out: list[tuple[dict[str, Any], MailItem]] = []
    for case in data["cases"]:
        eml_path = example_dir / case["eml"]
        if not eml_path.is_file():
            raise FileNotFoundError(f"missing eml for case {case['id']}: {eml_path}")
        out.append((case, load_eml(eml_path)))
    return out
