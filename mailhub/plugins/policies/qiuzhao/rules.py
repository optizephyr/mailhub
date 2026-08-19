from __future__ import annotations

from dataclasses import dataclass

from .types import MailItem

RECRUIT_KEYWORDS = (
    "面试",
    "笔试",
    "测评",
    "机考",
    "线上面试",
    "视频面试",
    "面试邀约",
    "面试通知",
    "笔试通知",
    "在线测评",
    "能力测评",
    "取消面试",
    "面试取消",
    "改期",
    "时间调整",
    "assessment",
    "interview",
    "online test",
    "written test",
    "hirevue",
    "cancelled",
    "reschedule",
    "牛客",
    "赛码",
    "北森",
)

# 粗过滤额外召回：生命周期 / 强确认（避免取消信等被挡）
COARSE_LIFECYCLE_SIGNALS = (
    "取消面试",
    "面试取消",
    "笔试取消",
    "测评取消",
    "已取消",
    "取消本次",
    "无需参加",
    "不用参加",
    "面试已取消",
    "行程取消",
    "cancelled",
    "canceled",
    "改期通知",
    "面试改期",
    "笔试改期",
    "时间调整",
    "时间变更",
    "面试时间变更为",
    "新的面试时间",
    "更新后的时间",
    "reschedule",
    "rescheduled",
)

COARSE_CONFIRMED_SIGNALS = (
    "时间已确认",
    "面试时间已确认",
    "已为您安排",
    "请准时参加",
    "请准时出席",
    "会议号",
    "入会密码",
    "interview confirmed",
    "has been scheduled",
)

# 明确的日程/流程动作：即使同信夹杂宣推话术也保留
COARSE_KEEP_SIGNALS = (
    "测评通知",
    "笔试通知",
    "面试通知",
    "面试邀请",
    "面试邀约",
    "预约面试",
    "请预约",
    "选择面试时间",
    "在线笔试",
    "在线考试",
    "在线测评",
    "人才测评",
    "考试时间",
    "考试开始时间",
    "面试时间",
    "取消面试",
    "面试取消",
    "改期通知",
    "面试改期",
    "新的面试时间",
    "岗位流转",
    "hirevue",
    "online test",
)

# 宣讲会 / 直播等群发活动：无个人场次，且正文常出现「面试官」等词导致误判为面试。
# 只认主题命中：正常面试信的页脚也常挂宣讲会推广。
COARSE_BROADCAST_SIGNALS = (
    "宣讲会",
    "空中宣讲",
    "线上宣讲",
    "招聘宣讲",
    "预约直播",
    "直播预约",
    "直播间",
)

# 投递确认 / 宣推 / 福利：默认挡掉（除非同时命中 KEEP）
COARSE_NOISE_SIGNALS = (
    "免费权益",
    "token plan",
    "送你一份小礼物",
    "已收到你的申请",
    "已收到申请",
    "简历投递成功",
    "校招启动",
    "正式启动啦",
    "赶紧冲",
    "秋季校招已正式启动",
    "邀请你参加27届秋季校招网申",
    "现邀请你参加27届秋季校招网申",
    "邀请你参加2027届秋季校招网申",
    "邀请你投递",
    "邀请投递通知",
    "定价调整",
    "峰谷定价",
)


@dataclass(frozen=True)
class CoarseFilterResult:
    passed: bool
    reason: str


def _contains_any(text: str, signals: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(s.lower() in lower or s in text for s in signals)


def coarse_filter(mail: MailItem) -> CoarseFilterResult:
    """Stage A: 规则粗过滤。只决定是否值得精解析，不抽完整日程。"""
    blob = f"{mail.subject}\n{mail.body}"

    has_keep_in_subject = _contains_any(mail.subject, COARSE_KEEP_SIGNALS)
    # 宣推/投递确认正文常罗列「笔试→面试」流程，KEEP 只认主题命中，避免误放行
    if _contains_any(blob, COARSE_NOISE_SIGNALS) and not has_keep_in_subject:
        return CoarseFilterResult(False, "noise_signal")
    if _contains_any(mail.subject, COARSE_BROADCAST_SIGNALS) and not has_keep_in_subject:
        return CoarseFilterResult(False, "broadcast_signal")

    if _contains_any(blob, RECRUIT_KEYWORDS):
        return CoarseFilterResult(True, "recruit_keyword")
    if _contains_any(blob, COARSE_LIFECYCLE_SIGNALS):
        return CoarseFilterResult(True, "lifecycle_signal")
    if _contains_any(blob, COARSE_CONFIRMED_SIGNALS):
        return CoarseFilterResult(True, "confirmed_signal")
    if has_keep_in_subject or _contains_any(blob, COARSE_KEEP_SIGNALS):
        return CoarseFilterResult(True, "keep_signal")

    return CoarseFilterResult(False, "no_recruit_signal")
