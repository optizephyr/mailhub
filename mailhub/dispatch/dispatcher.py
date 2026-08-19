from __future__ import annotations

from typing import Optional

from mailhub.contracts.actions import ActionReceipt, ActionRequest, DispatchResult
from mailhub.contracts.protocols import ActionHandler, DispatchPlanner
from mailhub.contracts.resolve import ResolvedMail


class ActionDispatcher:
    """Collect ActionRequests from planners and route by type to handlers."""

    def __init__(
        self,
        planners: list[DispatchPlanner],
        handlers: dict[str, ActionHandler],
        *,
        dry_run: bool = False,
    ) -> None:
        self._planners = planners
        self._handlers = handlers
        self._dry_run = dry_run

    def dispatch(self, resolved: ResolvedMail) -> DispatchResult:
        requests: list[ActionRequest] = []
        for planner in self._planners:
            requests.extend(planner.plan(resolved))

        if not requests:
            return DispatchResult(status="skipped", receipts=[])

        receipts: list[ActionReceipt] = []
        for request in requests:
            handler = self._handlers.get(request.type)
            if handler is None:
                receipts.append(
                    ActionReceipt(
                        action_id=request.id,
                        status="failed",
                        error=f"no handler for type={request.type}",
                    )
                )
                continue
            if self._dry_run:
                # Planner already encodes would_* in payload when dry_run;
                # handler should honor dry_run via payload or we wrap here.
                receipt = handler.handle(request)
            else:
                receipt = handler.handle(request)
            receipts.append(receipt)

        statuses = {r.status for r in receipts}
        if self._dry_run or statuses <= {"would_execute", "skipped"}:
            status = "dry_run" if self._dry_run else "skipped"
            if self._dry_run:
                status = "dry_run"
            elif "failed" in statuses:
                status = "failed"
            elif statuses == {"skipped"}:
                status = "skipped"
            else:
                status = "succeeded"
        elif "failed" in statuses and (
            "succeeded" in statuses or "skipped" in statuses
        ):
            status = "partial"
        elif "failed" in statuses:
            status = "failed"
        elif statuses == {"skipped"}:
            status = "skipped"
        else:
            status = "succeeded"

        return DispatchResult(status=status, receipts=receipts)
