from __future__ import annotations

import requests

from mailhub.contracts.actions import ActionReceipt, ActionRequest
from mailhub.runtime.config import Settings
from mailhub.store.sqlite import EventStore


class BarkHandler:
    def __init__(self, store: EventStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def handle(self, request: ActionRequest) -> ActionReceipt:
        if request.payload.get("dry_run"):
            return ActionReceipt(
                action_id=request.id,
                status="would_execute",
            )

        existing = self.store.get_action_receipt(request.idempotency_key)
        if existing and existing.get("status") == "succeeded":
            return ActionReceipt(
                action_id=request.id,
                status="skipped",
                external_id=existing.get("external_id"),
            )

        payload = request.payload
        request_json = {
            "device_key": self.settings.bark_key,
            "title": str(payload.get("title") or ""),
            "body": str(payload.get("body") or ""),
        }
        if payload.get("url"):
            request_json["url"] = str(payload["url"])

        try:
            response = requests.post(
                f"{self.settings.bark_server_url}/push",
                json=request_json,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and data.get("code") not in (None, 200):
                raise RuntimeError(str(data.get("message") or "Bark 推送失败"))
            receipt = ActionReceipt(
                action_id=request.id,
                status="succeeded",
                external_id="bark",
            )
            self.store.mark_processed(
                str(payload.get("message_id") or ""),
                "push",
                source_id=str(payload.get("source_id") or ""),
            )
        except Exception as exc:
            receipt = ActionReceipt(
                action_id=request.id,
                status="failed",
                error=str(exc),
            )

        self.store.save_action_receipt(
            idempotency_key=request.idempotency_key,
            action_type=request.type,
            status=receipt.status,
            external_id=receipt.external_id,
            error=receipt.error,
        )
        return receipt
