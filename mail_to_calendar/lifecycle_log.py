from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .mail_qq import MailItem
from .models import CandidateEvent


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def new_trace_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def event_brief(event: CandidateEvent) -> dict[str, Any]:
    """主日志里的精简日程字段（不含 description 全文）。"""
    return {
        "action": event.action,
        "event_type": event.event_type,
        "company": event.company,
        "title": event.title,
        "start_at": event.start_at,
        "end_at": event.end_at,
        "location": event.location,
        "meeting_url": event.meeting_url,
        "confidence": event.confidence,
    }


def planned_event_brief(event: CandidateEvent, *, desc_limit: int = 200) -> dict[str, Any]:
    """dry-run 计划日程：event_brief + 截断 description。"""
    brief = event_brief(event)
    desc = event.description or ""
    if len(desc) > desc_limit:
        desc = desc[:desc_limit] + "…"
    brief["description"] = desc
    return brief


def log_llm_io(
    path: Path,
    *,
    trace_id: str,
    message_id: str,
    subject: str,
    model: str,
    api_base: str,
    input_messages: list[dict[str, Any]],
    output_raw: Optional[str],
    output_parsed: Optional[dict[str, Any]],
    ok: bool,
    decision: str,
    error: Optional[str],
    latency_ms: int,
) -> None:
    """旁路：完整 LLM I/O（不含 thinking），用 trace_id 关联主生命周期记录。"""
    append_jsonl(
        path,
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "trace_id": trace_id,
            "message_id": message_id,
            "subject": subject,
            "model": model,
            "api_base": api_base,
            "input": input_messages,
            "output_raw": output_raw,
            "output_parsed": output_parsed,
            "ok": ok,
            "decision": decision,
            "error": error,
            "latency_ms": latency_ms,
        },
    )


class MailTrace:
    """一封邮件一条生命周期记录：分阶段追加，finish 时落盘。"""

    def __init__(
        self,
        *,
        lifecycle_path: Path,
        mail: MailItem,
        run: Optional[dict[str, Any]] = None,
    ) -> None:
        self.lifecycle_path = lifecycle_path
        self.trace_id = new_trace_id()
        self.ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.run = dict(run or {})
        self.mail = {
            "message_id": mail.message_id,
            "uid": mail.uid,
            "subject": mail.subject,
            "from": mail.from_,
            "date": mail.date,
        }
        self.stages: list[dict[str, Any]] = []
        self._finished = False

    def add_stage(self, stage: dict[str, Any]) -> None:
        self.stages.append(stage)

    def finish(self, status: str, summary: str) -> None:
        if self._finished:
            return
        self._finished = True
        append_jsonl(
            self.lifecycle_path,
            {
                "v": 1,
                "trace_id": self.trace_id,
                "ts": self.ts,
                "run": self.run,
                "mail": self.mail,
                "outcome": {"status": status, "summary": summary},
                "stages": self.stages,
            },
        )

    def finish_dry_run(
        self,
        summary: str,
        *,
        result: str = "dry_run",
        match_via: str = "none",
        event_row_id: Optional[int] = None,
        planned_event: Optional[dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """parse_only / dry-run：补 apply 阶段再落盘。sync dry-run 可带 would_* 与 planned_event。"""
        stage: dict[str, Any] = {"name": "apply", "result": result}
        if result != "dry_run":
            stage["match"] = {"via": match_via}
        if event_row_id is not None:
            stage["event_row_id"] = event_row_id
        if planned_event is not None:
            stage["planned_event"] = planned_event
        if error:
            stage["error"] = error
        self.add_stage(stage)
        self.finish("dry_run", summary)

    @property
    def finished(self) -> bool:
        return self._finished
