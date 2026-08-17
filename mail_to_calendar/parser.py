from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from .config import Settings
from .lifecycle_log import MailTrace, event_brief, log_llm_io, new_trace_id
from .mail_qq import MailItem
from .models import CandidateEvent, LlmParseResult
from .rules import coarse_filter

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

# 选时间截止日里的“时间”，不是面试开始时间
DEADLINE_CONTEXT_RE = re.compile(
    r"(?:前完成|前选择|前预约|前确认|截止|之前选|之前完成).{0,8}"
    r"(?:20\d{2}\s*[年/-]\s*)?\d{1,2}\s*[月/-]\s*\d{1,2}"
    r"|"
    r"(?:20\d{2}\s*[年/-]\s*)?\d{1,2}\s*[月/-]\s*\d{1,2}"
    r".{0,12}(?:前完成|前选择|前预约|前确认|截止)"
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
    confirmed = any(s.lower() in lower or s in text for s in CONFIRMED_SIGNALS)
    scheduling = any(s.lower() in lower or s in text for s in SCHEDULE_INVITE_SIGNALS)

    # 强确认：即使同封提到过预约流程，也以已敲定场次为准
    strong_confirmed = any(
        s in text or s.lower() in lower
        for s in (
            "时间已确认",
            "面试时间已确认",
            "已为您安排",
            "请准时参加",
            "请准时出席",
            "会议号",
            "has been scheduled",
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


def parse_datetime(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or datetime.now(TZ)
    for pattern in DATETIME_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        parts = m.groupdict()
        year = int(parts["y"]) if parts.get("y") else now.year
        month = int(parts["m"])
        day = int(parts["d"])
        hour = int(parts["h"])
        minute = int(parts["min"])
        try:
            dt = datetime(year, month, day, hour, minute, tzinfo=TZ)
        except ValueError:
            continue
        # if year omitted and date already passed by >60 days, assume next year
        if "y" not in parts and dt < now - timedelta(days=60):
            dt = dt.replace(year=now.year + 1)
        return dt
    return None


def default_duration_hours(event_type: str) -> float:
    if event_type == "exam":
        return 2.0
    if event_type == "assessment":
        return 1.5
    return 1.0


def build_title(
    event_type: str,
    company: str,
    action: str,
    subject: str = "",
) -> str:
    """确定性重建日历标题为「[type|cancel] 公司」，供 calendar_match 认领旧日程。

    - cancel → [cancel] 公司
    - create / reschedule → [interview|exam|assessment|other] 公司（直接用类型，不转中文）
    - 公司为空时回退到主题前 40 字，保证至少带 [标签] 前缀。
    """
    name = (company or "").strip() or (subject or "").strip()[:40]
    label = "cancel" if action == "cancel" else (event_type or "other")
    return f"[{label}] {name}".strip()[:200]


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
    return m.group(1).strip() if m else ""


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

    if action == "cancel":
        return CandidateEvent(
            message_id=mail.message_id,
            subject=mail.subject,
            title=build_title(event_type, company, "cancel", mail.subject),
            event_type=event_type,
            action="cancel",
            company=company,
            description=f"来源邮件: {mail.subject}\n发件人: {mail.from_}\n\n{body[:1500]}",
            confidence=0.8 if company else 0.55,
            source_snippet=body[:300],
            references=list(mail.references),
        )

    # 岗位流转等：有截止确认时刻，但不建面试/笔试日程
    if any(s in blob for s in NON_SCHEDULE_SIGNALS):
        return None

    # 邀约选时间 ≠ 已确认场次：避免把候选时段写成日程
    if should_skip_as_schedule_invite(blob):
        return None

    start = parse_datetime(blob)
    if not start:
        return None

    hours = default_duration_hours(event_type)
    end = start + timedelta(hours=hours)
    title = build_title(event_type, company, action, mail.subject)
    meeting_url = extract_meeting_url(body)
    location = extract_location(body) or meeting_url
    stage = classify_stage(blob)
    confidence = 0.75 if stage == "confirmed" or action == "reschedule" else 0.55
    if company:
        confidence += 0.1

    desc_parts = [
        f"来源邮件: {mail.subject}",
        f"发件人: {mail.from_}",
        f"判定阶段: {stage}",
        f"动作: {action}",
    ]
    if meeting_url:
        desc_parts.append(f"链接: {meeting_url}")
    desc_parts.append("")
    desc_parts.append(body[:1500])

    return CandidateEvent(
        message_id=mail.message_id,
        subject=mail.subject,
        title=title[:200],
        event_type=event_type,
        action=action,
        start_at=start.replace(tzinfo=None).isoformat(timespec="seconds"),
        end_at=end.replace(tzinfo=None).isoformat(timespec="seconds"),
        location=location[:200],
        company=company,
        description="\n".join(desc_parts)[:3500],
        meeting_url=meeting_url,
        confidence=min(confidence, 0.95),
        source_snippet=body[:300],
        references=list(mail.references),
    )


def normalize_event(event: CandidateEvent) -> CandidateEvent:
    """Stage E: 统一整形字段。"""
    action = event.action if event.action in ("create", "reschedule", "cancel") else "create"
    event_type = event.event_type or "other"
    company = (event.company or "").strip()[:40]
    title = build_title(event_type, company, action, event.subject)

    return CandidateEvent(
        message_id=event.message_id,
        subject=event.subject,
        title=title[:200],
        event_type=event_type,
        action=action,
        start_at=str(event.start_at or ""),
        end_at=str(event.end_at or ""),
        location=str(event.location or "")[:200],
        company=company,
        description=str(event.description or "")[:3500],
        meeting_url=str(event.meeting_url or ""),
        confidence=float(event.confidence or 0.5),
        source_snippet=str(event.source_snippet or "")[:300],
        references=list(event.references or []),
    )


def _event_from_llm_data(mail: MailItem, data: dict[str, Any]) -> LlmParseResult:
    """把已解析的 JSON 转成 LlmParseResult（明确拒绝 vs 残缺）。"""
    if data.get("stage") == "schedule_invite":
        return LlmParseResult(
            decision="reject_by_model",
            reject_reason="schedule_invite",
        )
    if not data.get("relevant"):
        return LlmParseResult(
            decision="reject_by_model",
            reject_reason="irrelevant",
        )

    action = data.get("action") or "create"
    if action not in ("create", "reschedule", "cancel"):
        action = "create"
    if action != "cancel" and (not data.get("start_at") or not data.get("end_at")):
        return LlmParseResult(
            decision="incomplete",
            error="missing start_at/end_at for non-cancel action",
        )

    event_type = data.get("event_type") or "other"
    title = data.get("title") or mail.subject
    company = str(data.get("company") or "").strip() or guess_company(
        mail.subject, mail.body
    )

    event = CandidateEvent(
        message_id=mail.message_id,
        subject=mail.subject,
        title=str(title)[:200],
        event_type=event_type,
        action=action,
        start_at=str(data.get("start_at") or ""),
        end_at=str(data.get("end_at") or ""),
        location=str(data.get("location") or "")[:200],
        company=str(company)[:40],
        description=f"来源邮件: {mail.subject}\n发件人: {mail.from_}\n\n{mail.body[:1500]}",
        meeting_url=str(data.get("meeting_url") or ""),
        confidence=float(data.get("confidence") or 0.8),
        source_snippet=mail.body[:300],
        references=list(mail.references),
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
                "title, company, start_at(YYYY-MM-DDTHH:MM:SS, Asia/Shanghai), "
                "end_at, location, meeting_url, confidence(0-1).\n"
                "规则:\n"
                "- company 必填：填招聘方公司/机构简称（如 美团、字节跳动、快手）；"
                "实在无法判断时填空串。\n"
                "- 取消面试/无需参加：action=cancel，relevant=true，可不填时间。\n"
                "- 改期/时间变更为：action=reschedule，必须填新的 start_at/end_at。\n"
                "- 若是让候选人「选择/预约/挑选」面试时间：stage=schedule_invite，relevant=false。\n"
                "- 只有时间已确认的正式通知才 action=create 且 relevant=true。\n"
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
    模型明确拒绝（irrelevant / schedule_invite）不走启发式。

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
            reason = llm_result.reject_reason or "irrelevant"
            if reason == "schedule_invite":
                summary = "模型判定选时间邀约，不建日程"
            else:
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
        if event:
            normalized = normalize_event(event)
            assert normalized is not None
            _add_parse_stage(
                trace,
                engine="llm_then_heuristic",
                result=fallback_result,
                llm=llm_meta,
                event=normalized,
            )
            if own_trace:
                trace.finish_dry_run(
                    f"LLM 后启发式解析为{normalized.action}「{normalized.title}」，未执行 apply"
                )
            return normalized

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
