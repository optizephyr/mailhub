from __future__ import annotations

from mailhub.contracts.actions import ActionRequest
from mailhub.contracts.resolve import ResolvedMail

ACTION_PUSH = "bark.push"


class BarkPlanner:
    """Bark 选件规则尚未确定，因此暂不产生动作。"""

    def plan(self, resolved: ResolvedMail) -> list[ActionRequest]:
        return []
