from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    message_id: str
    # Adapter-owned stable locator. Callers persist and return it unchanged;
    # only the source adapter interprets its contents.
    source_key: str = ""


@dataclass
class MailMessage:
    source: SourceRef
    subject: str
    sender: str
    sent_at: Optional[str]
    text: str
    html: str
    references: list[str] = field(default_factory=list)

    @property
    def body(self) -> str:
        if self.text.strip():
            return self.text.strip()
        if self.html.strip():
            soup = BeautifulSoup(self.html, "lxml")
            return soup.get_text("\n", strip=True)
        return ""

    @property
    def message_id(self) -> str:
        return self.source.message_id


@dataclass
class IngestBatch:
    messages: list[MailMessage]
    next_checkpoint: Optional[str]
