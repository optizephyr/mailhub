from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from mailhub.contracts.protocols import (
    ActionHandler,
    DispatchPlanner,
    IngestSource,
    MailResolver,
)
from mailhub.store.sqlite import EventStore


@dataclass
class RunContext:
    run_id: str
    dry_run: bool
    full: bool
    source: IngestSource
    resolver: MailResolver
    planners: list[DispatchPlanner]
    handlers: dict[str, ActionHandler]
    store: EventStore
    source_id: str = "qq.default"
    lifecycle_log_path: Optional[Any] = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    received_count: int = 0
    resolved_count: int = 0
    ignored_count: int = 0
    action_count: int = 0
    failed_count: int = 0
    created: int = 0
    updated: int = 0
    cancelled: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)
    dry_run_reports: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    # legacy sync counters alias
    scanned: int = 0
    matched: int = 0
