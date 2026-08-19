from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class MailItem:
    """Qiuzhao parser internal mail view."""

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
        from bs4 import BeautifulSoup

        if self.text.strip():
            return self.text.strip()
        if self.html.strip():
            soup = BeautifulSoup(self.html, "lxml")
            return soup.get_text("\n", strip=True)
        return ""


@dataclass
class CandidateEvent:
    message_id: str
    subject: str
    title: str
    event_type: str
    action: str = "create"
    start_at: str = ""
    end_at: str = ""
    location: str = ""
    company: str = ""
    description: str = ""
    meeting_url: str = ""
    time_precision: str = "fixed"  # fixed | window
    confidence: float = 0.5
    source_snippet: str = ""
    references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LlmParseResult:
    decision: str
    event: Optional[CandidateEvent] = None
    error: Optional[str] = None
    latency_ms: Optional[int] = None
    reject_reason: Optional[str] = None
