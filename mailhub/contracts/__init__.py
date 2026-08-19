from __future__ import annotations

from .actions import ActionReceipt, ActionRequest, DispatchResult
from .messages import IngestBatch, MailMessage, SourceRef
from .protocols import ActionHandler, DispatchPlanner, IngestSource, MailResolver
from .resolve import (
    IgnoredMail,
    ResolveFailure,
    ResolvedMail,
    ResolveResult,
    TimeConstraint,
)

__all__ = [
    "ActionHandler",
    "ActionReceipt",
    "ActionRequest",
    "DispatchPlanner",
    "DispatchResult",
    "IgnoredMail",
    "IngestBatch",
    "IngestSource",
    "MailMessage",
    "MailResolver",
    "ResolveFailure",
    "ResolveResult",
    "ResolvedMail",
    "SourceRef",
    "TimeConstraint",
]
