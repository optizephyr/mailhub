from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

JSONL_MAX_LINES = 100



def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    _trim_jsonl(path, JSONL_MAX_LINES)


def _trim_jsonl(path: Path, max_lines: int) -> None:
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) <= max_lines:
        return
    path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")


def new_trace_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def event_brief(event: Any) -> dict[str, Any]:
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


def planned_event_brief(event: Any, *, desc_limit: int = 200) -> dict[str, Any]:
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
    def __init__(
        self,
        *,
        lifecycle_path: Path,
        mail: Any,
        run: Optional[dict[str, Any]] = None,
    ) -> None:
        self.lifecycle_path = lifecycle_path
        self.trace_id = new_trace_id()
        self.ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.run = dict(run or {})
        # Support MailMessage (contracts) and legacy MailItem
        if hasattr(mail, "source"):
            message_id = mail.source.message_id
            uid = 0
            sender = mail.sender
            date = mail.sent_at
            subject = mail.subject
        else:
            message_id = mail.message_id
            uid = getattr(mail, "uid", 0)
            sender = getattr(mail, "from_", "")
            date = getattr(mail, "date", None)
            subject = mail.subject
        self.mail = {
            "message_id": message_id,
            "uid": uid,
            "subject": subject,
            "from": sender,
            "date": date,
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
