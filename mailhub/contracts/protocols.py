from __future__ import annotations

from typing import Optional, Protocol

from .actions import ActionReceipt, ActionRequest
from .messages import IngestBatch, MailMessage
from .resolve import ResolvedMail, ResolveResult


class IngestSource(Protocol):
    def fetch(self, checkpoint: Optional[str]) -> IngestBatch: ...


class MailResolver(Protocol):
    def resolve(self, message: MailMessage) -> ResolveResult: ...


class DispatchPlanner(Protocol):
    def plan(self, resolved: ResolvedMail) -> list[ActionRequest]: ...


class ActionHandler(Protocol):
    def handle(self, request: ActionRequest) -> ActionReceipt: ...
