from __future__ import annotations

from mailhub.contracts.actions import ActionReceipt, ActionRequest


class BarkHandler:
    def handle(self, request: ActionRequest) -> ActionReceipt:
        return ActionReceipt(
            action_id=request.id,
            status="failed",
            error="Bark 选件规则和动作载荷尚未定义",
        )
