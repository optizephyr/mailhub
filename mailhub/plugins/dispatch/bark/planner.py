from __future__ import annotations

import uuid
from datetime import datetime

from mailhub.contracts.actions import ActionRequest
from mailhub.contracts.resolve import ResolvedMail
from mailhub.runtime.config import Settings
from mailhub.store.sqlite import EventStore

ACTION_PUSH = "bark.push"


class BarkPlanner:
    def __init__(
        self,
        store: EventStore,
        settings: Settings,
        *,
        dry_run: bool = False,
        source_id: str = "",
    ) -> None:
        self.store = store
        self.settings = settings
        self.dry_run = dry_run
        self.source_id = source_id

    def plan(self, resolved: ResolvedMail) -> list[ActionRequest]:
        if not self.settings.bark_enabled or resolved.kind != "schedule_invite":
            return []
        if self.store.already_processed(resolved.source.message_id, self.source_id):
            return []

        company = str(resolved.attributes.get("company") or "").strip()
        name = company or resolved.summary.strip()[:40] or "秋招"
        deadline = resolved.time.deadline if resolved.time else None
        body = "请去预约"
        if deadline:
            try:
                shown = datetime.fromisoformat(deadline).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                shown = deadline
            body = f"预约截止：{shown}"

        payload = {
            "dry_run": self.dry_run,
            "result": "would_push" if self.dry_run else "push",
            "summary": f"将通过 Bark 推送「{name} 请预约」"
            if self.dry_run
            else f"通过 Bark 推送「{name} 请预约」",
            "title": f"{name} 请预约",
            "body": body,
            "url": resolved.links[0] if resolved.links else "",
            "message_id": resolved.source.message_id,
            "source_id": self.source_id,
        }
        return [
            ActionRequest(
                id=str(uuid.uuid4()),
                type=ACTION_PUSH,
                idempotency_key=f"bark:{self.source_id}:{resolved.source.message_id}",
                payload=payload,
            )
        ]
