"""把邮件解析结果和「日历里已有的日程」对上（不依赖 macOS，可单测）。"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from mailhub.plugins.policies.qiuzhao.types import CandidateEvent

from .types import AppleEventRef

MARKER_PREFIX = "[mailhub]"
# 旧前缀保留读取，兼容此前写入的日程描述。
_MARKER_RE = re.compile(
    r"\[(?:mailhub|qiuzhao-mail2calendar|mail-to-calendar)\]\s*mid=(\S+)"
)

# 当前标题使用中文标签；英文标签保留用于兼容既有日程。
LABEL_TO_TYPE = {
    "interview": "interview",
    "exam": "exam",
    "assessment": "assessment",
    "面试": "interview",
    "笔试": "exam",
    "测评": "assessment",
}
_TITLE_RE = re.compile(r"^\s*\[([^\]]{1,16})\]\s*(.+)$")


def marker_line(message_id: str) -> str:
    return f"{MARKER_PREFIX} mid={message_id}"


def extract_marker_message_id(description: str) -> str:
    match = _MARKER_RE.search(description or "")
    return match.group(1) if match else ""


def companies_match(company: str, title_company: str) -> bool:
    left, right = company.strip(), title_company.strip()
    if not left or not right:
        return False
    return left in right or right in left


def split_title(summary: str) -> tuple[str, str]:
    """`[面试] 美团` → `("面试", "美团")`；非本工具格式返回两个空串。"""
    match = _TITLE_RE.match(summary or "")
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _type_conflicts(label: str, event_type: str) -> bool:
    label_type = LABEL_TO_TYPE.get(label)
    if not label_type or event_type in ("", "other"):
        return False
    return label_type != event_type


def _latest(items: list[AppleEventRef]) -> AppleEventRef:
    return max(items, key=lambda c: (c.start_at, c.uid))


def match_calendar_event(
    event: CandidateEvent,
    candidates: Iterable[AppleEventRef],
) -> Optional[AppleEventRef]:
    """在日历已有日程里找本封邮件该改动的那条；找不到返回 None。

    优先回复链（描述里埋的 message-id），其次同公司 + 同学段里开始时间最晚的一场。
    """
    items = [c for c in candidates if c.uid]
    refs = {r for r in (event.references or []) if r}

    if refs:
        chained = [
            c for c in items if c.marker_message_id and c.marker_message_id in refs
        ]
        if chained:
            return _latest(chained)

    company = (event.company or "").strip()
    if not company:
        return None

    same_company = []
    for candidate in items:
        label, title_company = split_title(candidate.summary)
        if not label or not companies_match(company, title_company):
            continue
        if _type_conflicts(label, event.event_type):
            continue
        same_company.append(candidate)

    if not same_company:
        return None
    return _latest(same_company)
