from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

JSONValue = Any


@dataclass
class ActionRequest:
    id: str
    type: str
    idempotency_key: str
    payload: dict[str, JSONValue] = field(default_factory=dict)


@dataclass
class ActionReceipt:
    action_id: str
    status: str  # succeeded | skipped | failed | would_execute
    external_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class DispatchResult:
    status: str  # succeeded | partial | failed | skipped | dry_run
    receipts: list[ActionReceipt] = field(default_factory=list)
