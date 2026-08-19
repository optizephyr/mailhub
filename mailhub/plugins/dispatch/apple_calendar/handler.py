from __future__ import annotations

from typing import Any, Optional

from mailhub.contracts.actions import ActionReceipt, ActionRequest
from mailhub.plugins.policies.qiuzhao.types import CandidateEvent
from mailhub.runtime.config import Settings
from mailhub.store.sqlite import EventStore, StoredEvent

from . import calendar_io
from .planner import (
    ACTION_CANCEL,
    ACTION_CREATE,
    ACTION_FAIL,
    ACTION_SKIP,
    ACTION_UPDATE,
)


class AppleCalendarHandler:
    def __init__(self, store: EventStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        # allow tests to patch these
        self.create_apple_event = calendar_io.create_apple_event
        self.update_apple_event = calendar_io.update_apple_event
        self.delete_apple_event = calendar_io.delete_apple_event

    def handle(self, request: ActionRequest) -> ActionReceipt:
        payload = request.payload
        dry_run = bool(payload.get("dry_run"))
        result = str(payload.get("result") or "")
        source_id = str(payload.get("source_id") or "")

        if dry_run or request.type in (ACTION_SKIP, ACTION_FAIL):
            if not dry_run:
                event = CandidateEvent(**payload["candidate"])
                target = self._target_from_payload(payload.get("target"))
                if request.type == ACTION_SKIP and result != "skipped_duplicate":
                    row_id = target.id if target else payload.get("event_row_id")
                    self.store.mark_processed(
                        event.message_id,
                        event.action,
                        int(row_id) if row_id else None,
                        source_id=source_id,
                    )
                elif request.type == ACTION_FAIL:
                    self.store.mark_processed(
                        event.message_id,
                        event.action,
                        None,
                        source_id=source_id,
                    )
            status = "would_execute" if dry_run else (
                "skipped" if request.type == ACTION_SKIP else "failed"
            )
            return ActionReceipt(
                action_id=request.id,
                status=status,
                error=payload.get("error"),
            )

        existing = self.store.get_action_receipt(request.idempotency_key)
        if existing and existing.get("status") == "succeeded":
            return ActionReceipt(
                action_id=request.id,
                status="skipped",
                external_id=existing.get("external_id"),
            )

        event = CandidateEvent(**payload["candidate"])
        target = self._target_from_payload(payload.get("target"))

        try:
            if request.type == ACTION_CREATE:
                row_id = self._apply_create(event)
                self.store.mark_processed(
                    event.message_id, event.action, row_id, source_id=source_id
                )
                receipt = ActionReceipt(
                    action_id=request.id, status="succeeded", external_id=str(row_id)
                )
            elif request.type == ACTION_UPDATE:
                assert target is not None
                self._apply_update(target, event)
                self.store.mark_processed(
                    event.message_id, event.action, target.id, source_id=source_id
                )
                receipt = ActionReceipt(
                    action_id=request.id,
                    status="succeeded",
                    external_id=str(target.id),
                )
            elif request.type == ACTION_CANCEL:
                assert target is not None
                self._apply_cancel(target, event)
                self.store.mark_processed(
                    event.message_id, "cancel", target.id, source_id=source_id
                )
                receipt = ActionReceipt(
                    action_id=request.id,
                    status="succeeded",
                    external_id=str(target.id),
                )
            else:
                receipt = ActionReceipt(
                    action_id=request.id,
                    status="failed",
                    error=f"unknown type {request.type}",
                )
        except Exception as exc:
            receipt = ActionReceipt(
                action_id=request.id, status="failed", error=str(exc)
            )

        self.store.save_action_receipt(
            idempotency_key=request.idempotency_key,
            action_type=request.type,
            status=receipt.status,
            external_id=receipt.external_id,
            error=receipt.error,
        )
        # stash extras for engine logging via payload mutation
        payload["_receipt_meta"] = {
            "result": self._result_label(request.type, receipt),
            "event_row_id": int(receipt.external_id)
            if receipt.external_id and str(receipt.external_id).isdigit()
            else payload.get("event_row_id"),
            "match_via": payload.get("match_via"),
            "summary": payload.get("summary"),
            "sinks": self._sinks_for(receipt),
        }
        return receipt

    def _result_label(self, action_type: str, receipt: ActionReceipt) -> str:
        if receipt.status == "failed":
            return "failed"
        return {
            ACTION_CREATE: "created",
            ACTION_UPDATE: "updated",
            ACTION_CANCEL: "cancelled",
            ACTION_SKIP: "skipped_same",
        }.get(action_type, receipt.status)

    def _sinks_for(self, receipt: ActionReceipt) -> Optional[dict[str, str]]:
        if not receipt.external_id or not str(receipt.external_id).isdigit():
            return None
        row = self.store.get_event(int(receipt.external_id))
        return row.sinks if row else None

    def _target_from_payload(self, raw: Any) -> Optional[StoredEvent]:
        if not isinstance(raw, dict):
            return None
        return StoredEvent(
            id=int(raw.get("id") or 0),
            company=str(raw.get("company") or ""),
            event_type=str(raw.get("event_type") or ""),
            title=str(raw.get("title") or ""),
            start_at=str(raw.get("start_at") or ""),
            end_at=str(raw.get("end_at") or ""),
            status=str(raw.get("status") or "active"),
            source_message_id=str(raw.get("source_message_id") or ""),
            sinks=dict(raw.get("sinks") or {}),
        )

    def _apply_create(self, event: CandidateEvent) -> int:
        apple_event_id = self.create_apple_event(
            event, self.settings.apple_calendar_name, self.settings.reminder_minutes
        )
        return self.store.create_event(
            company=event.company,
            event_type=event.event_type,
            title=event.title,
            start_at=event.start_at,
            end_at=event.end_at,
            source_message_id=event.message_id,
            sinks={"apple": apple_event_id},
        )

    def _apply_update(self, target: StoredEvent, event: CandidateEvent) -> None:
        sink_ids = dict(target.sinks)
        external_id = sink_ids.get("apple")
        if external_id and external_id != "apple-ok":
            self.update_apple_event(
                external_id, event, self.settings.apple_calendar_name
            )
        else:
            sink_ids["apple"] = self.create_apple_event(
                event, self.settings.apple_calendar_name, self.settings.reminder_minutes
            )
        self.store.update_event(
            target.id,
            title=event.title,
            start_at=event.start_at,
            end_at=event.end_at,
            source_message_id=event.message_id,
            sinks=sink_ids,
        )

    def _apply_cancel(self, target: StoredEvent, event: CandidateEvent) -> None:
        external_id = target.sinks.get("apple")
        if external_id and external_id != "apple-ok":
            self.delete_apple_event(external_id)
        self.store.cancel_event(target.id, event.message_id)
