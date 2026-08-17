from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class CandidateEvent:
    """A calendar action extracted from one email."""

    message_id: str
    subject: str
    title: str
    event_type: str  # interview | exam | assessment | other
    action: str = "create"  # create | reschedule | cancel
    start_at: str = ""  # ISO-8601 local datetime; empty for cancel
    end_at: str = ""
    location: str = ""
    company: str = ""
    description: str = ""
    meeting_url: str = ""
    confidence: float = 0.5
    source_snippet: str = ""
    references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StoredEvent:
    id: int
    company: str
    event_type: str
    title: str
    start_at: str
    end_at: str
    status: str
    source_message_id: str
    sinks: dict[str, str] = field(default_factory=dict)  # sink -> external_id


@dataclass
class SyncResult:
    scanned: int = 0
    matched: int = 0
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    events: list[CandidateEvent] = field(default_factory=list)


@dataclass
class LlmParseResult:
    """LLM 精解析结果。reject_by_model 不走启发式；incomplete/error 可兜底。"""

    decision: str  # accept | reject_by_model | incomplete | error
    event: Optional[CandidateEvent] = None
    error: Optional[str] = None
