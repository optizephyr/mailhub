from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from mailhub.logging.lifecycle import MailTrace, event_brief, log_llm_io, new_trace_id
from mailhub.runtime.config import Settings

from .rules import coarse_filter
from .types import CandidateEvent, LlmParseResult, MailItem

TZ = ZoneInfo("Asia/Shanghai")

# 顺序：assessment / exam 优先于 interview，避免「笔试…面试」误判
TYPE_KEYWORDS = {
    "assessment": ("测评", "能力测验", "性格测试", "assessment", "hirevue", "北森", "赛码"),
    "exam": ("笔试", "机考", "在线考试", "考试邀请", "written", "online test", "编程题"),
    "interview": ("面试", "interview", "一面", "二面", "三面", "终面", "hr面"),
}

COMPANY_PATTERNS = [
    re.compile(r"(科大讯飞(?:股份有限公司)?|讯飞)"),
    re.compile(r"[【\[]([^】\]]{2,30})[】\]]"),
    # 仅当 【笔试通知】等通用括号被跳过时，再取「】公司名邀请」
    re.compile(r"[】\]]\s*([^\s【】\[\]]{2,20}?)\s*邀请"),
    re.compile(r"(?:来自|邀请你参加|邀请您参加|诚邀你参加|诚邀您参加)\s*([^\s，,。；;]{2,20})"),
    re.compile(r"^(.{2,20}?)(?:校招|校园招聘|招聘|面试|笔试|测评)"),
]

COMPANY_SKIP_NAMES = frozenset(
    {
        "面试",
        "笔试",
        "测评",
        "通知",
        "邀约",
        "笔试通知",
        "面试信息",
        "面试通知",
        "测评通知",
        "注意事项",
        "账号信息",
        "详细信息",
        "温馨提示",
        "企业官网",
    }
)

_COMPANY_SUFFIXES = (
    "校园招聘",
    "秋季校园招聘",
    "春季校园招聘",
    "校招",
    "集团",
    "科技",
)

# 业务动作邮件：有截止时刻但不建招聘日程
NON_SCHEDULE_SIGNALS = (
    "岗位流转",
    "流转邀请",
    "意向确认表",
    "岗位流转意向确认",
)

DATETIME_PATTERNS = [
    # 2026年8月20日 14:00 / 2026-08-20 14:00:00
    re.compile(
        r"(?P<y>20\d{2})\s*[年/-]\s*(?P<m>\d{1,2})\s*[月/-]\s*(?P<d>\d{1,2})\s*[日号]?"
        r"[^\d]{0,6}(?P<h>\d{1,2})\s*[:：点时]\s*(?P<min>\d{1,2})"
    ),
    # 8月20日 14:00
    re.compile(
        r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]"
        r"[^\d]{0,6}(?P<h>\d{1,2})\s*[:：点时]\s*(?P<min>\d{1,2})"
    ),
    # 08/20 14:00 or 8-20 14:00
    re.compile(
        r"(?P<m>\d{1,2})\s*[/-]\s*(?P<d>\d{1,2})"
        r"[^\d]{0,6}(?P<h>\d{1,2})\s*[:：]\s*(?P<min>\d{1,2})"
    ),
]

URL_RE = re.compile(r"https?://[^\s<>\"']+")
LOCATION_RE = re.compile(
    r"(?:地点|面试地点|地址|会议室)[:：\s]*([^\n。；;]{2,80})"
)

# 「先邀约选时间」：尚未敲定场次，不应建日历
SCHEDULE_INVITE_SIGNALS = (
    "选择面试时间",
    "选择时间",
    "请选择时间",
    "请选择合适的时间",
    "请预约",
    "预约面试",
    "预约时间",
    "可选时段",
    "可选时间",
    "以下时间中选择",
    "从下列时间",
    "自行选择时间",
    "点击链接选择",
    "点击链接预约",
    "点此预约",
    "预约面试时间",
    "面试时间选择",
    "选一个时间",
    "挑选时间",
    "book a time",
    "select a time",
    "choose a time",
    "schedule your interview",
    "pick a time",
    "time slot",
)

DEFAULT_SLOT_SIGNALS = (
    "逾期将安排",
    "逾期默认安排",
    "未选择将安排",
    "未预约将安排",
    "否则安排在",
)

# 「已成功预约 / 场次已敲定」：候选人已确定场次（即便正文未复述具体时间）。
# 优先级最高，会覆盖 schedule_invite 信号 —— 避免「预约成功通知」被误判为「请预约」。
CONFIRMED_BOOKING_SIGNALS = (
    "预约成功",
    "预约成功通知",
    "面试成功通知",
    "预约面试成功",
    "已成功预约",
    "已成功安排",
    "面试已预约",
    "面试已确认",
    "时间已敲定",
    "面试时间已敲定",
    "已敲定",
    "已选定",
    "你已成功",
    "您已成功",
    "已为你预约",
    "已为您预约",
    "scheduled successfully",
    "you have been scheduled",
    "your booking is confirmed",
    "booking confirmed",
    "interview booked",
)

# 「正式通知」：时间已确认，应当建日历
CONFIRMED_SIGNALS = (
    "面试通知",
    "笔试通知",
    "测评通知",
    "面试邀请",
    "面试邀约",
    "面试时间",
    "考试时间",
    "考试开始时间",
    "在线笔试",
    "在线考试",
    "时间已确认",
    "面试时间已确认",
    "已为您安排",
    "已帮您预约",
    "请准时参加",
    "请准时出席",
    "面试安排如下",
    "正式通知",
    "确认参加",
    "会议号",
    "入会密码",
    "interview confirmed",
    "your interview is scheduled",
    "has been scheduled",
)

# 开放窗口：窗口内任意时刻完成，走提醒事项而非日历场次
WINDOW_SIGNALS = (
    "时间范围内任选",
    "范围内任选",
    "任选两小时",
    "任选一小时",
    "任意时段",
    "任意时间完成",
    "小时内完成",
    "工作日之内",
    "个工作日内",
    "工作日内完成",
    "开放窗口",
)
RELATIVE_HOURS_RE = re.compile(r"(\d+)\s*小时内")
RELATIVE_WORKDAYS_RE = re.compile(r"(\d+)\s*个工作日")

_DURATION_NUMBER = (
    r"(?:\d+(?:\.\d+)?|[一二两三四五六七八九十]+个半|"
    r"[一二两三四五六七八九十]+半|[一二两三四五六七八九十]+)"
)
_DURATION_PATTERNS = (
    re.compile(
        rf"(?:预计(?:耗时|用时|时长)?|耗时|用时|时长|大约|约|不超过|最多)"
        rf"[^\d一二两三四五六七八九十]{{0,6}}"
        rf"(?P<first>{_DURATION_NUMBER})"
        rf"(?:\s*[-—~～至到]\s*(?P<second>{_DURATION_NUMBER}))?"
        rf"\s*个?\s*(?P<unit>小时|分钟)(?!\s*内)"
    ),
    re.compile(
        rf"任选\s*(?P<first>{_DURATION_NUMBER})"
        rf"(?:\s*[-—~～至到]\s*(?P<second>{_DURATION_NUMBER}))?"
        rf"\s*个?\s*(?P<unit>小时|分钟)(?!\s*内).{{0,12}}完成"
    ),
    re.compile(
        rf"(?P<first>{_DURATION_NUMBER})"
        rf"(?:\s*[-—~～至到]\s*(?P<second>{_DURATION_NUMBER}))?"
        rf"\s*个?\s*(?P<unit>小时|分钟)(?!\s*内).{{0,6}}"
        rf"(?:完成|做完|答完)(?:笔试|测评|考试|测试)?"
    ),
)
_ENGLISH_WINDOW_DURATION_RE = re.compile(
    r"\b(?:within|in)\s+(\d+(?:\.\d+)?)\s*(hours?|minutes?)\b",
    re.IGNORECASE,
)

# 选时间截止日里的“时间”，不是面试开始时间
# 注意：用 [\s\S] 而不是 . 是因为 BeautifulSoup.get_text('\n') 会在 block
# 之间插入换行，让「截止」关键词和日期可能跨行；Python 3 默认 . 不匹配 \n。
DEADLINE_CONTEXT_RE = re.compile(
    r"(?:前完成|前选择|前预约|前确认|截止|之前选|之前完成)[\s\S]{0,8}"
    r"(?:20\d{2}\s*[年/-]\s*)?\d{1,2}\s*[月/-]\s*\d{1,2}"
    r"|"
    r"(?:20\d{2}\s*[年/-]\s*)?\d{1,2}\s*[月/-]\s*\d{1,2}"
    r"[\s\S]{0,12}(?:前完成|前选择|前预约|前确认|截止)"
)


# 取消
CANCEL_SIGNALS = (
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
    "interview cancelled",
    "interview canceled",
)

# 改期（避免「改期机会」等说明性措辞误触发）
RESCHEDULE_SIGNALS = (
    "改期通知",
    "面试改期",
    "笔试改期",
    "原面试改期",
    "已改期",
    "时间调整",
    "时间变更",
    "面试时间变更为",
    "调整至",
    "调整为",
    "新的面试时间",
    "更新后的时间",
    "reschedule",
    "rescheduled",
    "new interview time",
    "time has been changed",
)

RESCHEDULE_FALSE_FRIENDS = (
    "改期机会",
    "次改期机会",
    "延期安排",
    "自动顺延",
)


def detect_action(text: str) -> str:
    lower = text.lower()
    if any(s.lower() in lower or s in text for s in CANCEL_SIGNALS):
        return "cancel"
    if any(s in text or s.lower() in lower for s in RESCHEDULE_FALSE_FRIENDS):
        # 说明性「改期机会 / 顺延」不是本封改期通知
        pass
    elif any(s.lower() in lower or s in text for s in RESCHEDULE_SIGNALS):
        return "reschedule"
    return "create"


def classify_stage(text: str) -> str:
    """Return confirmed | schedule_invite | unknown."""
    lower = text.lower()

    # 「已成功预约 / 场次已敲定」优先级最高 —— 避免「预约成功通知」被「点此预约」等同封提醒词误判为 schedule_invite。
    if any(s in text or s.lower() in lower for s in CONFIRMED_BOOKING_SIGNALS):
        return "confirmed"

    confirmed = any(s.lower() in lower or s in text for s in CONFIRMED_SIGNALS)
    scheduling = any(s.lower() in lower or s in text for s in SCHEDULE_INVITE_SIGNALS)

    # 强确认：即使同封提到过预约流程，也以已敲定场次为准
    strong_confirmed = any(
        s in text or s.lower() in lower
        for s in (
            "时间已确认",
            "面试时间已确认",
            "已为您安排",
            "已成功预约",
            "预约成功",
            "面试已确认",
            "请准时参加",
            "请准时出席",
            "会议号",
            "has been scheduled",
            "scheduled successfully",
            "interview confirmed",
        )
    )
    if strong_confirmed:
        return "confirmed"
    if scheduling:
        return "schedule_invite"
    if confirmed:
        return "confirmed"
    return "unknown"


def should_skip_as_schedule_invite(text: str) -> bool:
    """选时间邀约：即使正文里出现多个候选时刻，也不建日程。"""
    if classify_stage(text) == "schedule_invite":
        return True
    # 只有「请于 X 前选择」这类截止时间，没有正式确认
    if DEADLINE_CONTEXT_RE.search(text) and classify_stage(text) != "confirmed":
        # 若全文几乎只在谈预约/选择，仍跳过；纯正式通知里偶发“截止报名”不在此列
        if any(s in text or s.lower() in text.lower() for s in SCHEDULE_INVITE_SIGNALS):
            return True
    return False


def detect_event_type(text: str) -> str:
    if any(s in text for s in NON_SCHEDULE_SIGNALS):
        return "other"

    subject = text.split("\n", 1)[0]
    subject_lower = subject.lower()
    for etype, kws in TYPE_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in subject_lower or kw in subject:
                return etype

    # 全文：取最早出现的类型关键词，避免正文末尾「面试」覆盖笔试
    best_pos: Optional[int] = None
    best_type = "other"
    lower = text.lower()
    for etype, kws in TYPE_KEYWORDS.items():
        for kw in kws:
            if kw.isascii():
                pos = lower.find(kw.lower())
            else:
                pos = text.find(kw)
            if pos < 0:
                continue
            if best_pos is None or pos < best_pos:
                best_pos = pos
                best_type = etype
    return best_type


def guess_company(subject: str, body: str) -> str:
    # 主题优先：正文【面试信息】等栏目名容易抢匹配
    for source in (subject, body[:800]):
        for pattern in COMPANY_PATTERNS:
            for m in pattern.finditer(source):
                name = m.group(1).strip(" -_|【】[]")
                name = re.sub(r"\s+", "", name)
                if not name or name in COMPANY_SKIP_NAMES:
                    continue
                if any(ch in name for ch in "【】[]"):
                    continue
                if len(name) >= 2:
                    return name[:40]
    return ""


def _datetime_from_match(m: re.Match[str], now: datetime) -> Optional[datetime]:
    parts = m.groupdict()
    year = int(parts["y"]) if parts.get("y") else now.year
    month = int(parts["m"])
    day = int(parts["d"])
    hour = int(parts["h"])
    minute = int(parts["min"])
    try:
        dt = datetime(year, month, day, hour, minute, tzinfo=TZ)
    except ValueError:
        return None
    if "y" not in parts and dt < now - timedelta(days=60):
        dt = dt.replace(year=now.year + 1)
    return dt


def parse_datetime(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or datetime.now(TZ)
    for pattern in DATETIME_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        dt = _datetime_from_match(m, now)
        if dt is not None:
            return dt
    return None


def parse_all_datetimes(
    text: str,
    now: Optional[datetime] = None,
    *,
    skip_deadline_context: bool = False,
) -> list[datetime]:
    """从 text 里提取所有匹配 DATETIME_PATTERNS 的 datetime。

    skip_deadline_context=True 时：先扫出 DEADLINE_CONTEXT_RE 命中过的所有日期
    （如 9月4），然后丢掉正文里同一日期的所有 datetime。该策略适用于「正文里有
    具体开场时刻 + 还有一个『预约/选择截止」时刻」的场景——后者不应被误当作
    end_at（如【Shopee】预约成功通知 正文里「2026年9月5日 15:30」与
    「2026年9月4日下午2点」）。

    之所以不是仅仅按 span 重叠过滤，是因为同一天日期在正文里可能出现两次
    （如 Shopee 邮件倒数第二行「可预约期限时间（2026年9月4日下午2点00分）」），
    第一处 DEADLINE_CONTEXT_RE 会命中并跳过，但第二处不一定能命中 span 重叠。
    """
    now = now or datetime.now(TZ)

    deadline_date_keys: set[str] = set()
    if skip_deadline_context:
        for m in DEADLINE_CONTEXT_RE.finditer(text):
            for dm in re.finditer(r"\d{1,2}\s*[月/-]\s*\d{1,2}", m.group()):
                # 归一到 "9月4" / "9/4" / "9-4" 三个形式之一都能被 `ds in match.group()` 命中
                deadline_date_keys.add(re.sub(r"\s+", "", dm.group()))

    found: list[tuple[int, datetime]] = []
    seen: set[str] = set()
    for pattern in DATETIME_PATTERNS:
        for match in pattern.finditer(text):
            if skip_deadline_context:
                normalized_match = re.sub(r"\s+", "", match.group())
                if any(
                    ds in normalized_match for ds in deadline_date_keys
                ):
                    continue
            dt = _datetime_from_match(match, now)
            if dt is None:
                continue
            key = f"{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}"
            if key in seen:
                continue
            seen.add(key)
            found.append((match.start(), dt))
    found.sort(key=lambda item: item[0])
    return [dt for _, dt in found]


def _default_slot_datetime(text: str, now: datetime) -> Optional[datetime]:
    positions = [text.find(signal) for signal in DEFAULT_SLOT_SIGNALS]
    positions = [pos for pos in positions if pos >= 0]
    if not positions:
        return None
    return parse_datetime(text[min(positions) :], now=now)


def is_open_window(text: str, event_type: str) -> bool:
    if event_type == "assessment":
        return True
    return any(signal in text for signal in WINDOW_SIGNALS)


def relative_deadline(text: str, now: datetime) -> Optional[datetime]:
    match = RELATIVE_HOURS_RE.search(text)
    if match:
        return now + timedelta(hours=int(match.group(1)))
    match = RELATIVE_WORKDAYS_RE.search(text)
    if match:
        return now + timedelta(days=int(match.group(1)))
    return None


def _duration_number(value: str) -> Optional[float]:
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass

    half = 0.0
    if value.endswith("个半"):
        value = value[:-2]
        half = 0.5
    elif value.endswith("半"):
        value = value[:-1]
        half = 0.5
    digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value == "十":
        number = 10
    elif "十" in value:
        tens, ones = value.split("十", 1)
        number = (digits.get(tens, 1) * 10) + digits.get(ones, 0)
    else:
        number = digits.get(value)
    if number is None:
        return None
    return float(number) + half


def extract_task_duration_minutes(text: str) -> Optional[int]:
    """Conservatively extract task effort from Chinese duration phrases.

    Deadline windows such as “48 小时内完成” and English phrases are
    intentionally excluded; the LLM may supply those cases.
    """
    for pattern in _DURATION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group("second") or match.group("first")
        amount = _duration_number(raw)
        if amount is None or amount <= 0:
            continue
        minutes = amount * (60 if match.group("unit") == "小时" else 1)
        return max(1, int(round(minutes)))
    return None


def _duration_matches_window_claim(text: str, minutes: Optional[int]) -> bool:
    if minutes is None:
        return False
    for match in RELATIVE_HOURS_RE.finditer(text):
        if int(match.group(1)) * 60 == minutes:
            return True
    for match in _ENGLISH_WINDOW_DURATION_RE.finditer(text):
        amount = float(match.group(1))
        claimed = amount * (60 if match.group(2).lower().startswith("hour") else 1)
        if int(round(claimed)) == minutes:
            return True
    return False


def _anchor_now(mail: MailItem) -> datetime:
    if mail.date:
        try:
            parsed = parsedate_to_datetime(mail.date)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=TZ)
            return parsed.astimezone(TZ)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(mail.date)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=TZ)
                return parsed.astimezone(TZ)
            except ValueError:
                pass
    return datetime.now(TZ)


def _iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


def default_duration_hours(event_type: str) -> float:
    if event_type == "exam":
        return 2.0
    if event_type == "assessment":
        return 1.5
    return 1.0


# 主题拼接顺序：业务线长度上限与「业务线」词黑名单，避免把主题描述误当业务线。
_BUSINESS_LINE_MAX_LEN = 10
_SUBJECT_LINE_SUFFIXES = (
    "面试通知", "面试邀约", "面试邀请", "面试邀请函",
    "笔试通知", "笔试邀约", "笔试邀请",
    "测评通知", "测评邀请", "在线测评通知",
    "面试", "笔试", "测评",
    "通知", "邀约", "邀请", "邀请函",
)
_BUSINESS_LINE_BAD_TOKENS = (
    # 动词/敬语/状态类，应被识别为「描述」而非「业务线」
    "邀请", "请", "您", "同学", "成功", "已收到", "启动",
    # 邮件动作/后缀词，出现即说明该片段还是后缀延伸，不是业务线
    "招聘", "校招", "笔试", "测评", "通知", "邀约",
    "面试", "改期", "取消", "调整",
)
_SUBJECT_BRACKET_RE = re.compile(r"[【\[]([^】\]]{2,40})[】\]]")
_DEPARTMENT_RE = re.compile(r"面试部门[：:]\s*([^\n\r]+)")
_DEPARTMENT_SPLIT_RE = re.compile(r"[\-－—–]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _strip_subject_suffix(subject: str) -> str:
    s = subject.strip()
    for suf in _SUBJECT_LINE_SUFFIXES:
        if s.endswith(suf):
            return s[: -len(suf)].rstrip(" -_、，。,.")
    return s


def _looks_like_business_line(text: str) -> bool:
    text = text.strip()
    if not text or len(text) > _BUSINESS_LINE_MAX_LEN:
        return False
    for bad in _BUSINESS_LINE_BAD_TOKENS:
        if bad in text:
            return False
    if not _CJK_RE.search(text):
        return False
    return True


def extract_business_line(
    subject: str, body: str, *, company: str = ""
) -> str:
    """从主题提取业务线（如「千问事业部」），正文「面试部门：xxx-yyy」作为后备。

    返回空串表示未识别出业务线；title 拼接时仅在非空时追加「·业务线」。
    当提取出的业务线与已识别出的 company 重叠时跳过，避免「[面试] 美团·美团」冗余。
    """
    company_norm = (company or "").strip()
    # 主题路径：「【公司】业务线[面试/笔试/通知]」
    stripped = _strip_subject_suffix(subject)
    m = _SUBJECT_BRACKET_RE.search(stripped)
    if m:
        after = stripped[m.end():].strip(" -_、，。,.")
        if _looks_like_business_line(after):
            line = re.sub(r"\s+", "", after)[:_BUSINESS_LINE_MAX_LEN]
            if not _overlaps_company(line, company_norm):
                return line

    # 正文后备：「面试部门：xxx-yyy」取连字符前的主部门
    body_match = _DEPARTMENT_RE.search(body)
    if body_match:
        raw = body_match.group(1).strip()
        first = _DEPARTMENT_SPLIT_RE.split(raw, maxsplit=1)[0].strip()
        first = re.sub(r"\s+", "", first)
        if _looks_like_business_line(first):
            if not _overlaps_company(first, company_norm):
                return first[:_BUSINESS_LINE_MAX_LEN]

    return ""


def _overlaps_company(text: str, company: str) -> bool:
    """业务线与 company 重复（相等、子串、超集）时为 True。"""
    t = (text or "").strip()
    c = (company or "").strip()
    if not t or not c:
        return False
    return t == c or t in c or c in t


def build_title(
    event_type: str,
    company: str,
    action: str,
    subject: str = "",
    *,
    business_line: str = "",
) -> str:
    """确定性重建日历标题为「[中文标签] 公司[·业务线]」，供 calendar_match 认领旧日程。

    - cancel → [取消] 公司[·业务线]
    - create / reschedule → [面试|笔试|测评|其他] 公司[·业务线]
    - 公司为空时回退到主题前 40 字，保证至少带 [标签] 前缀。
    - 业务线为空时仅输出「公司」；业务线存在时用 · 拼接，避免「同公司不同业务线」合并。
    """
    name = (company or "").strip() or (subject or "").strip()[:40]
    bl = (business_line or "").strip()
    if bl:
        name = f"{name}·{bl}" if name else bl
    labels = {
        "interview": "面试",
        "exam": "笔试",
        "assessment": "测评",
        "other": "其他",
    }
    label = "取消" if action == "cancel" else labels.get(event_type, "其他")
    return f"[{label}] {name}".strip()[:200]


def _title_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(TZ)
    return parsed.replace(tzinfo=TZ)


def _is_day_boundary(value: datetime) -> bool:
    return (value.hour, value.minute) in ((0, 0), (23, 59))


def _window_title_suffix(start_at: str, end_at: str) -> str:
    start = _title_datetime(start_at)
    end = _title_datetime(end_at)
    if not end:
        return ""

    end_date = f"{end.month}月{end.day}日"
    if not start:
        end_time = "" if _is_day_boundary(end) else f" {end:%H:%M}"
        return f"截止{end_date}{end_time}"

    start_date = f"{start.month}月{start.day}日"
    if start.date() != end.date():
        return f"{start_date}-{end_date}"

    start_visible = not _is_day_boundary(start)
    end_visible = not _is_day_boundary(end)
    if start_visible and end_visible:
        return f"{start_date} {start:%H:%M}-{end:%H:%M}"
    if start_visible:
        return f"{start_date} {start:%H:%M}开始"
    if end_visible:
        return f"{start_date} 截止{end:%H:%M}"
    return start_date


def build_reminder_title(
    event_type: str,
    company: str,
    action: str,
    start_at: str,
    end_at: str,
    subject: str = "",
    task_duration_minutes: Optional[int] = None,
    *,
    business_line: str = "",
) -> str:
    title = build_title(event_type, company, action, subject, business_line=business_line)
    if action == "cancel":
        return title
    if task_duration_minutes and task_duration_minutes > 0:
        if task_duration_minutes % 60 == 0:
            duration = f"{task_duration_minutes // 60}小时"
        else:
            duration = f"{task_duration_minutes}分钟"
        title = title.replace("]", f"·{duration}]", 1)
    suffix = _window_title_suffix(start_at, end_at)
    return f"{title} {suffix}".strip()[:200]


def _schedule_invite_title(company: str, subject: str) -> str:
    name = company.strip() or subject.strip()[:40] or "秋招"
    return f"{name} 请预约"[:200]


def extract_meeting_url(text: str) -> str:
    for url in URL_RE.findall(text):
        host = urlparse(url).netloc.lower()
        if any(
            x in host
            for x in (
                "tencent",
                "zoom",
                "feishu",
                "lark",
                "meeting",
                "teams",
                "nowcoder",
                "baijiahao",
                "hirevue",
                "wjx",
            )
        ):
            return url.rstrip(").,，。]")
    urls = URL_RE.findall(text)
    return urls[0].rstrip(").,，。]") if urls else ""


def extract_location(text: str) -> str:
    m = LOCATION_RE.search(text)
    if not m:
        return ""
    loc = m.group(1).strip()
    if _is_bare_location_label(loc):
        return ""
    return loc


def _is_bare_location_label(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return True
    return stripped.endswith((":", "："))


def prefer_place(location: str, meeting_url: str) -> str:
    loc = (location or "").strip()
    url = (meeting_url or "").strip()
    if loc and not _is_bare_location_label(loc):
        return loc[:200]
    return url[:200] if url else ""


def normalize_company_name(company: str) -> str:
    name = (company or "").strip()
    changed = True
    while changed and name:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if name.endswith(suffix) and len(name) - len(suffix) >= 2:
                name = name[: -len(suffix)].rstrip("-_ ")
                changed = True
                break
    return name


def _extract_schedule_url(mail: MailItem) -> str:
    if mail.html.strip():
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(mail.html, "lxml")
        for link in soup.find_all("a", href=True):
            label = link.get_text(" ", strip=True).lower()
            href = str(link.get("href") or "").strip()
            if href.startswith(("http://", "https://")) and any(
                signal in label for signal in ("预约", "选择", "时间", "book", "schedule")
            ):
                return href
    return extract_meeting_url(mail.body)


def _schedule_invite_event(
    mail: MailItem, *, company: str = "", confidence: float = 0.85
) -> CandidateEvent:
    blob = f"{mail.subject}\n{mail.body}"
    company = company or guess_company(mail.subject, mail.body)
    times = parse_all_datetimes(blob, now=_anchor_now(mail))
    deadline = _iso(times[-1]) if times else ""
    return CandidateEvent(
        message_id=mail.message_id,
        subject=mail.subject,
        title=_schedule_invite_title(company, mail.subject),
        event_type="schedule_invite",
        action="create",
        deadline=deadline,
        company=company,
        meeting_url=_extract_schedule_url(mail),
        time_precision="unknown",
        confidence=confidence if company else min(confidence, 0.65),
        source_snippet=mail.body[:300],
        references=list(mail.references),
    )


THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>([\s\S]*?)</think>", re.IGNORECASE)


def _iter_json_object_spans(text: str):
    """Yield (start, end) spans of top-level `{...}` objects, string-aware."""
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        escape = False
        for j in range(i, n):
            c = text[j]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield i, j + 1
                    i = j + 1
                    break
        else:
            break


def parse_llm_json(content: str) -> dict:
    """Parse JSON from compatible APIs (Markdown fence / MiniMax <think> / trailing junk)."""
    text = content.strip()
    # MiniMax-M2 等推理模型会把思考过程包在 <think>…</think> 里，再跟最终 JSON
    text = THINK_BLOCK_RE.sub("", text)
    text = re.sub(r"</?think\b[^>]*>", "", text, flags=re.IGNORECASE).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text, count=1)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # 推理正文里常夹带示例 JSON；取最后一个能解析的对象（最终答案）
    last_error: Optional[Exception] = None
    last_obj: Optional[dict] = None
    for start, end in _iter_json_object_spans(text):
        try:
            data = json.loads(text[start:end])
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(data, dict):
            last_obj = data
    if last_obj is not None:
        return last_obj
    if last_error:
        raise last_error
    raise json.JSONDecodeError("No JSON object found", text, 0)




def heuristic_parse(mail: MailItem) -> Optional[CandidateEvent]:
    body = mail.body
    blob = f"{mail.subject}\n{body}"
    action = detect_action(blob)
    company = guess_company(mail.subject, body)
    event_type = detect_event_type(blob)
    # title 与 company 字段都使用归一后的 company，避免「[面试] 阿里巴巴校园招聘·千问事业部」这种未归一拼接
    normalized_company = normalize_company_name(company)
    business_line = extract_business_line(mail.subject, body, company=normalized_company)

    if action == "cancel":
        return CandidateEvent(
            message_id=mail.message_id,
            subject=mail.subject,
            title=build_title(event_type, normalized_company, "cancel", mail.subject, business_line=business_line),
            event_type=event_type,
            action="cancel",
            company=normalized_company,
            description="",
            confidence=0.8 if company else 0.55,
            source_snippet=body[:300],
            references=list(mail.references),
            business_line=business_line,
        )

    # 岗位流转等：有截止确认时刻，但不建面试/笔试日程
    if any(s in blob for s in NON_SCHEDULE_SIGNALS):
        return None

    now = _anchor_now(mail)
    default_slot = _default_slot_datetime(blob, now)

    # 无出场时刻的预约/选场进入 Bark，不写日历或提醒事项。
    if should_skip_as_schedule_invite(blob):
        if any(
            signal in blob
            for signal in ("可选时段", "可选时间", "以下时间中选择", "从下列时间")
        ) and len(parse_all_datetimes(blob, now=_anchor_now(mail))) >= 2:
            # 候选场次的多日程模型不在本轮 Bark 需求内，保持原有忽略行为。
            return None
        if default_slot is None:
            return _schedule_invite_event(mail, company=company)

    times = (
        [default_slot]
        if default_slot
        else parse_all_datetimes(blob, now=now, skip_deadline_context=True)
    )
    meeting_url = extract_meeting_url(body)
    location = extract_location(body) or meeting_url
    title = build_title(event_type, normalized_company, action, mail.subject, business_line=business_line)
    stage = classify_stage(blob)
    confidence = 0.75 if stage == "confirmed" or action == "reschedule" else 0.55
    if company:
        confidence += 0.1

    if is_open_window(blob, event_type):
        start = times[0] if times else None
        end = times[-1] if len(times) >= 2 else None
        if end is None:
            end = relative_deadline(blob, now)
        if start is not None and end is None and len(times) == 1:
            end = start
            start = None
        return CandidateEvent(
            message_id=mail.message_id,
            subject=mail.subject,
            title=title[:200],
            event_type=event_type,
            action=action,
            start_at=_iso(start),
            end_at=_iso(end),
            deadline=_iso(end),
            location=(location or "")[:200],
            company=normalized_company,
            description="",
            meeting_url=meeting_url,
            time_precision="window",
            task_duration_minutes=extract_task_duration_minutes(blob),
            confidence=min(confidence, 0.95),
            source_snippet=body[:300],
            references=list(mail.references),
            business_line=business_line,
        )

    start = times[0] if times else None
    if not start:
        return None

    end = times[1] if len(times) >= 2 else start + timedelta(
        hours=default_duration_hours(event_type)
    )
    if not location:
        return None

    return CandidateEvent(
        message_id=mail.message_id,
        subject=mail.subject,
        title=title[:200],
        event_type=event_type,
        action=action,
        start_at=_iso(start),
        end_at=_iso(end),
        location=location[:200],
        company=normalized_company,
        description="",
        meeting_url=meeting_url,
        time_precision="fixed",
        confidence=min(confidence, 0.95),
        source_snippet=body[:300],
        references=list(mail.references),
        business_line=business_line,
    )


def normalize_event(event: CandidateEvent) -> CandidateEvent:
    """Stage E: 统一整形字段。"""
    action = event.action if event.action in ("create", "reschedule", "cancel") else "create"
    event_type = event.event_type or "other"
    time_precision = (
        event.time_precision
        if event.time_precision in ("fixed", "window", "unknown")
        else "fixed"
    )
    end_at = str(event.end_at or "")
    deadline = str(event.deadline or "")
    if time_precision == "window":
        deadline = deadline or end_at
        end_at = end_at or deadline
    raw_duration = event.task_duration_minutes
    task_duration_minutes = (
        int(raw_duration)
        if isinstance(raw_duration, (int, float)) and raw_duration > 0
        else None
    )
    # Defensive: fixed-precision 场景下如果 LLM/启发式都未填 end_at，按 start_at + duration 算
    # 避免被【Shopee】这种「正文里有预约截止日」错当作 end_at 填入。
    start_at_str = str(event.start_at or "")
    if time_precision == "fixed" and not end_at and start_at_str:
        minutes = task_duration_minutes or int(
            default_duration_hours(event_type) * 60
        )
        try:
            start_dt = datetime.fromisoformat(start_at_str)
            end_at = (start_dt + timedelta(minutes=minutes)).isoformat(
                timespec="seconds"
            )
        except ValueError:
            pass
    company = normalize_company_name(event.company)[:40]
    location = prefer_place(str(event.location or ""), str(event.meeting_url or ""))
    business_line = (event.business_line or "").strip()
    if event_type == "schedule_invite":
        title = _schedule_invite_title(company, event.subject)
    elif time_precision == "window":
        title = build_reminder_title(
            event_type,
            company,
            action,
            str(event.start_at or ""),
            end_at,
            event.subject,
            task_duration_minutes,
            business_line=business_line,
        )
    else:
        title = build_title(event_type, company, action, event.subject, business_line=business_line)

    return CandidateEvent(
        message_id=event.message_id,
        subject=event.subject,
        title=title[:200],
        event_type=event_type,
        action=action,
        start_at=str(event.start_at or ""),
        end_at=end_at,
        deadline=deadline,
        location=location,
        company=company,
        description="",
        meeting_url=str(event.meeting_url or ""),
        time_precision=time_precision,
        task_duration_minutes=task_duration_minutes,
        confidence=float(event.confidence or 0.5),
        source_snippet=str(event.source_snippet or "")[:300],
        references=list(event.references or []),
        sent_at=str(event.sent_at or ""),
        business_line=business_line,
    )


def merge_llm_with_heuristic(
    llm_event: CandidateEvent,
    heuristic: Optional[CandidateEvent],
) -> CandidateEvent:
    def fill(primary: str, fallback: str) -> str:
        return (primary or "").strip() or (fallback or "").strip()

    heur_start = heuristic.start_at if heuristic else ""
    heur_end = heuristic.end_at if heuristic else ""
    heur_deadline = heuristic.deadline if heuristic else ""
    heur_company = heuristic.company if heuristic else ""
    heur_url = heuristic.meeting_url if heuristic else ""
    heur_location = heuristic.location if heuristic else ""
    heur_type = heuristic.event_type if heuristic else ""
    heur_precision = heuristic.time_precision if heuristic else ""
    heur_duration = heuristic.task_duration_minutes if heuristic else None
    heur_business_line = heuristic.business_line if heuristic else ""

    start_at = fill(llm_event.start_at, heur_start)
    end_at = fill(llm_event.end_at, heur_end)
    deadline = fill(llm_event.deadline, heur_deadline)
    meeting_url = fill(llm_event.meeting_url, heur_url)
    location = prefer_place(llm_event.location, meeting_url)
    if not location:
        location = prefer_place(heur_location, meeting_url or heur_url)
        meeting_url = fill(meeting_url, heur_url)
        location = prefer_place(location, meeting_url)
    company = fill(llm_event.company, heur_company)
    event_type = llm_event.event_type
    if (not event_type or event_type == "other") and heur_type:
        event_type = heur_type
    precision = llm_event.time_precision
    if precision not in ("fixed", "window", "unknown") and heur_precision:
        precision = heur_precision
    if not fill(llm_event.start_at, llm_event.end_at) and heur_precision:
        precision = heur_precision

    used_heuristic_time = bool(
        (not (llm_event.start_at or "").strip() and heur_start)
        or (not (llm_event.end_at or "").strip() and heur_end)
        or (not (llm_event.deadline or "").strip() and heur_deadline)
    )
    confidence = float(llm_event.confidence or 0.8)
    if used_heuristic_time:
        confidence = min(confidence, 0.7)

    return CandidateEvent(
        message_id=llm_event.message_id,
        subject=llm_event.subject,
        title=llm_event.title,
        event_type=event_type,
        action=llm_event.action,
        start_at=start_at,
        end_at=end_at,
        deadline=deadline,
        location=location,
        company=company,
        description="",
        meeting_url=meeting_url,
        time_precision=precision,
        task_duration_minutes=heur_duration or llm_event.task_duration_minutes,
        confidence=confidence,
        source_snippet=llm_event.source_snippet,
        references=list(llm_event.references or []),
        sent_at=llm_event.sent_at or (heuristic.sent_at if heuristic else ""),
        business_line=fill(llm_event.business_line, heur_business_line),
    )


def _event_from_llm_data(mail: MailItem, data: dict[str, Any]) -> LlmParseResult:
    """把已解析的 JSON 转成 LlmParseResult（明确拒绝 vs 残缺）。"""
    if data.get("stage") == "schedule_invite":
        company = str(data.get("company") or "").strip()
        event = _schedule_invite_event(
            mail,
            company=company,
            confidence=float(data.get("confidence") or 0.8),
        )
        model_deadline = str(data.get("deadline") or "").strip()
        if model_deadline:
            event.deadline = model_deadline
        model_url = str(data.get("meeting_url") or "").strip()
        if model_url:
            event.meeting_url = model_url
        return LlmParseResult(decision="accept", event=normalize_event(event))
    if not data.get("relevant"):
        return LlmParseResult(
            decision="reject_by_model",
            reject_reason="irrelevant",
        )

    action = data.get("action") or "create"
    if action not in ("create", "reschedule", "cancel"):
        action = "create"
    event_type = data.get("event_type") or "other"
    precision = str(data.get("time_precision") or "").strip()
    if precision not in ("fixed", "window"):
        if event_type == "assessment":
            precision = "window"
        elif action != "cancel" and not data.get("location") and (
            data.get("start_at") or data.get("end_at")
        ):
            precision = "window"
        else:
            precision = "fixed"
    incomplete_error: Optional[str] = None
    if action != "cancel":
        if precision == "window":
            if not data.get("start_at") and not data.get("end_at"):
                incomplete_error = "missing start_at/end_at for window task"
        elif (
            not data.get("start_at")
            or not data.get("location")
        ):
            # end_at 不再是必填：fixed-precision 的 end_at 由代码按 start_at + duration 算
            incomplete_error = "missing start_at/location for non-cancel action"

    title = data.get("title") or mail.subject
    company = str(data.get("company") or "").strip() or guess_company(
        mail.subject, mail.body
    )
    business_line = (
        str(data.get("business_line") or "").strip()
        or extract_business_line(mail.subject, mail.body, company=company)
    )
    model_duration = (
        int(data["task_duration_minutes"])
        if isinstance(data.get("task_duration_minutes"), (int, float))
        and not isinstance(data.get("task_duration_minutes"), bool)
        and data["task_duration_minutes"] > 0
        else None
    )
    if _duration_matches_window_claim(
        f"{mail.subject}\n{mail.body}", model_duration
    ):
        model_duration = None

    event = CandidateEvent(
        message_id=mail.message_id,
        subject=mail.subject,
        title=str(title)[:200],
        event_type=event_type,
        action=action,
        start_at=str(data.get("start_at") or ""),
        end_at=str(data.get("end_at") or ""),
        deadline=str(data.get("deadline") or ""),
        location=str(data.get("location") or "")[:200],
        company=str(company)[:40],
        description="",
        meeting_url=str(data.get("meeting_url") or ""),
        time_precision=precision,
        task_duration_minutes=model_duration,
        confidence=float(data.get("confidence") or 0.8),
        source_snippet=mail.body[:300],
        references=list(mail.references),
        sent_at=str(mail.date or ""),
        business_line=business_line,
    )
    if incomplete_error:
        return LlmParseResult(
            decision="incomplete", event=event, error=incomplete_error
        )
    return LlmParseResult(decision="accept", event=normalize_event(event))


def llm_parse(
    mail: MailItem,
    settings: Settings,
    *,
    trace: Optional[MailTrace] = None,
) -> LlmParseResult:
    """Stage C: LLM 精解析；完整 I/O 写入 llm_io 旁路（经 trace_id 关联）。"""
    if not settings.llm_enabled:
        return LlmParseResult(decision="error", error="llm not enabled")

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "你是校招日程提取助手。区分选时间邀约、正式通知、改期、取消。"
                "只输出合法 JSON 对象，不要 markdown，不要在 JSON 前后加任何文字。"
            ),
        },
        {
            "role": "user",
            "content": (
                "从秋招/校招相关邮件中提取日程。只返回一个 JSON 对象，不要 markdown，不要解释。\n"
                "字段: relevant(bool), action(create|reschedule|cancel), "
                "stage(confirmed|schedule_invite|other), "
                "event_type(interview|exam|assessment|other), "
                "time_precision(fixed|window|unknown), "
                "title, company, business_line, "
                "start_at(YYYY-MM-DDTHH:MM:SS, Asia/Shanghai), "
                "end_at, deadline, location, meeting_url, "
                "task_duration_minutes(正整数或null), confidence(0-1).\n"
                "规则:\n"
                "- company 必填：填招聘方公司/机构简称（如 美团、字节跳动、快手）；"
                "实在无法判断时填空串。\n"
                "- business_line：所属业务部/部门/事业群简称（如 千问事业部、淘天集团、阿里云、抖音电商）。"
                "同一公司不同业务线的面试必须分别抽取，绝不可合并。无法判断时填空串。\n"
                "- business_line 不要拼进 company，company 仍填招聘方。\n"
                "- 取消面试/无需参加：action=cancel，relevant=true，可不填时间。\n"
                "- 已确认时刻的面试、固定开考笔试：time_precision=fixed，"
                "start_at 必填，location 必填。\n"
                "  · 正文**显式给了结束时刻**（如「14:00-15:30」「至 15:30 结束」「面试将持续 1.5 小时」），应填 end_at。\n"
                "  · 正文**只给了开始时刻**（如「面试时间：14:00」「14:00 开始」），end_at 留空，"
                "由代码按 start_at + 默认时长自动计算（面试 1h、笔试 2h，或按 task_duration_minutes）。\n"
                "  · **正文里的『预约/选择截止」日期不能填进 end_at**——"
                "那是预约修改 deadline，不是面试/笔试结束时间。\n"
                "  · 线上日程的 location 填会议链接。\n"
                "- 开放窗口（测评、任选时段完成的笔试、N 小时/工作日内完成）："
                "time_precision=window，end_at 填截止，start_at 填窗口开始（可空），"
                "location 可空，链接放 meeting_url。\n"
                "- task_duration_minutes 只填完成任务本身预计需要的分钟数；"
                "区间或上限取上限。仅有「N小时内完成/within N hours」时必须填 null，"
                "因为那是完成窗口而非任务耗时。\n"
                "- 不按时间跨度判断 fixed/window：跨度很长但要求全程按时参加仍是 fixed；"
                "窗口很短但可任选时间完成仍是 window。\n"
                "- 改期/时间变更为：action=reschedule，fixed 必须填新的时间和地点；"
                "window 必须填新的截止或窗口。\n"
                "- 若是让候选人「选择/预约/挑选」场次且没有可出场时刻："
                "stage=schedule_invite，relevant=true，time_precision=unknown；"
                "预约截止填 deadline，不填 start_at/end_at/location。\n"
                "- 区分「请选择/请预约/点此预约」（stage=schedule_invite）"
                "与「预约成功/已成功预约/已为您预约/预约成功通知/scheduled successfully」"
                "（stage=confirmed）：\n"
                "  · 前者是邀请候选人选时间，后者是候选人已确定场次。\n"
                "  · 成功预约的邮件若正文给出具体面试时间（如 2026-09-05 14:00），"
                "按 fixed 提取，填 start_at/end_at/location。\n"
                "  · 成功预约的邮件若未复述时间但给了 mokahr/会议链接，"
                "stage=confirmed，time_precision=unknown，不填 start_at/end_at；"
                "此时 relevant 仍为 true，但本封不产生日历事件（让用户在 mokahr 查看）。\n"
                "- 若信里给出逾期默认/保底出场时刻，则不是 schedule_invite；"
                "按该时刻提取 fixed 日程。\n"
                "- 时间已确认的正式通知、可完成的开放窗口、schedule_invite "
                "都属于 relevant=true。\n"
                "- 选时间的截止日不是面试开始时间。\n\n"
                f"主题: {mail.subject}\n"
                f"正文:\n{mail.body[:4000]}"
            ),
        },
    ]

    output_raw: Optional[str] = None
    output_parsed: Optional[dict[str, Any]] = None
    decision = "error"
    error: Optional[str] = None
    result = LlmParseResult(decision="error", error="not started")
    started = time.monotonic()

    try:
        resp = requests.post(
            f"{settings.llm_api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.llm_model,
                "temperature": 0,
                "messages": messages,
            },
            timeout=60,
        )
        resp.raise_for_status()
        message = resp.json()["choices"][0]["message"]
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("LLM 返回的 message.content 不是文本")
        output_raw = content
        output_parsed = parse_llm_json(content)
        result = _event_from_llm_data(mail, output_parsed)
        decision = result.decision
        error = result.error
    except Exception as exc:
        decision = "error"
        error = str(exc)
        result = LlmParseResult(decision="error", error=error)

    latency_ms = int((time.monotonic() - started) * 1000)
    log_llm_io(
        settings.llm_io_log_path,
        trace_id=trace.trace_id if trace else new_trace_id(),
        message_id=mail.message_id,
        subject=mail.subject,
        model=settings.llm_model,
        api_base=settings.llm_api_base,
        input_messages=messages,
        output_raw=output_raw,
        output_parsed=output_parsed,
        ok=output_parsed is not None,
        decision=decision,
        error=error,
        latency_ms=latency_ms,
    )
    result.latency_ms = latency_ms
    return result


def _llm_stage_meta(settings: Settings, llm_result: LlmParseResult) -> dict[str, Any]:
    """主日志 parse.llm 只留摘要字段（完整 I/O 在 llm_io 旁路）。"""
    return {
        "decision": llm_result.decision,
        "latency_ms": llm_result.latency_ms,
        "model": settings.llm_model,
    }


def _add_parse_stage(
    trace: MailTrace,
    *,
    engine: str,
    result: str,
    event: Optional[CandidateEvent] = None,
    llm: Optional[dict[str, Any]] = None,
) -> None:
    stage: dict[str, Any] = {"name": "parse", "engine": engine, "result": result}
    if llm is not None:
        stage["llm"] = llm
    if event is not None:
        stage["event"] = event_brief(event)
    trace.add_stage(stage)


def parse_mail(
    mail: MailItem,
    settings: Settings,
    *,
    trace: Optional[MailTrace] = None,
) -> Optional[CandidateEvent]:
    """
    解析流水线：
      Stage A 规则粗过滤 → Stage C LLM（可选）→ Stage D 启发式兜底 → Stage E 规范化
    模型明确拒绝 irrelevant 时不走启发式；schedule_invite 是已解析邮件。

    若传入 trace，写入 coarse/parse 阶段；拒绝路径会 finish。
    成功产出事件时不 finish，留给 apply 侧补全。
    未传入 trace 时自建一条并在本函数内 finish（便于单测）。
    """
    own_trace = trace is None
    if own_trace:
        trace = MailTrace(
            lifecycle_path=settings.lifecycle_log_path,
            mail=mail,
            run={"mode": "parse_only", "dry_run": True},
        )
    assert trace is not None

    # Stage A
    coarse = coarse_filter(mail)
    if not coarse.passed:
        trace.add_stage(
            {
                "name": "coarse_filter",
                "result": "reject",
                "reason": coarse.reason,
            }
        )
        trace.finish("rejected_coarse", f"粗过滤拒绝：{coarse.reason}")
        return None

    trace.add_stage(
        {
            "name": "coarse_filter",
            "result": "pass",
            "reason": coarse.reason,
        }
    )

    # Stage B/C
    if settings.llm_enabled:
        llm_result = llm_parse(mail, settings, trace=trace)
        llm_meta = _llm_stage_meta(settings, llm_result)

        if llm_result.decision == "accept" and llm_result.event:
            source_text = f"{mail.subject}\n{mail.body}"
            heuristic_duration = extract_task_duration_minutes(
                source_text
            )
            if heuristic_duration is not None:
                llm_result.event.task_duration_minutes = heuristic_duration
            elif _duration_matches_window_claim(
                source_text, llm_result.event.task_duration_minutes
            ):
                llm_result.event.task_duration_minutes = None
            llm_result.event = normalize_event(llm_result.event)
            _add_parse_stage(
                trace,
                engine="llm",
                result="accept",
                llm=llm_meta,
                event=llm_result.event,
            )
            if own_trace:
                trace.finish_dry_run(
                    f"解析为{llm_result.event.action}「{llm_result.event.title}」，未执行 apply"
                )
            return llm_result.event

        if llm_result.decision == "reject_by_model":
            summary = "模型判定无关邮件，不建日程"
            _add_parse_stage(
                trace,
                engine="llm",
                result="reject_by_model",
                llm=llm_meta,
            )
            trace.finish("rejected_parse", summary)
            return None

        # incomplete / error → Stage D 启发式兜底
        fallback_result = (
            "incomplete_fallback"
            if llm_result.decision == "incomplete"
            else "error_fallback"
        )
        event = heuristic_parse(mail)
        if llm_result.event:
            merged = merge_llm_with_heuristic(llm_result.event, event)
            normalized = normalize_event(merged)
            if event and llm_result.decision != "incomplete":
                normalized = normalize_event(event)
            event = normalized
        elif event:
            event = normalize_event(event)
        if event:
            _add_parse_stage(
                trace,
                engine="llm_then_heuristic",
                result=fallback_result,
                llm=llm_meta,
                event=event,
            )
            if own_trace:
                trace.finish_dry_run(
                    f"LLM 后启发式解析为{event.action}「{event.title}」，未执行 apply"
                )
            return event

        _add_parse_stage(
            trace,
            engine="llm_then_heuristic",
            result="reject_heuristic",
            llm=llm_meta,
        )
        trace.finish("rejected_parse", "LLM 失败且启发式未能抽出日程")
        return None

    # Stage D only（未启用 LLM）
    event = heuristic_parse(mail)
    if event:
        normalized = normalize_event(event)
        assert normalized is not None
        _add_parse_stage(
            trace,
            engine="heuristic",
            result="accept",
            event=normalized,
        )
        if own_trace:
            trace.finish_dry_run(
                f"启发式解析为{normalized.action}「{normalized.title}」，未执行 apply"
            )
        return normalized

    _add_parse_stage(trace, engine="heuristic", result="reject_heuristic")
    trace.finish("rejected_parse", "启发式未能抽出日程")
    return None
