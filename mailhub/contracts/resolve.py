from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

from .messages import SourceRef

JSONValue = Any


@dataclass
class TimeConstraint:
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    deadline: Optional[str] = None
    timezone: Optional[str] = None
    precision: str = "unknown"  # fixed | window | date | unknown


@dataclass
class ResolvedMail:
    source: SourceRef
    kind: str
    change: str  # new | updated | cancelled | unknown
    title: str
    summary: str
    importance: str  # normal | high | critical
    time: Optional[TimeConstraint]
    location: Optional[str]
    links: list[str] = field(default_factory=list)
    correlation_key: Optional[str] = None
    attributes: dict[str, JSONValue] = field(default_factory=dict)
    confidence: float = 0.5


@dataclass
class IgnoredMail:
    source: SourceRef
    reason: str


@dataclass
class ResolveFailure:
    source: SourceRef
    error: str


ResolveResult = Union[ResolvedMail, IgnoredMail, ResolveFailure]
