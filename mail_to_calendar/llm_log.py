from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_llm_call(
    path: Path,
    *,
    message_id: str,
    subject: str,
    model: str,
    api_base: str,
    input_messages: list[dict[str, Any]],
    output_raw: Optional[str],
    output_parsed: Optional[dict[str, Any]],
    ok: bool,
    output_reasoning: Optional[str] = None,
    decision: str,
    error: Optional[str],
    latency_ms: int,
) -> None:
    """每次 LLM 调用落一条完整 I/O 记录（成功/拒绝/失败都写）。"""
    append_jsonl(
        path,
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "message_id": message_id,
            "subject": subject,
            "model": model,
            "api_base": api_base,
            "input": input_messages,
            "output_raw": output_raw,
            "output_reasoning": output_reasoning,
            "output_parsed": output_parsed,
            "ok": ok,
            "decision": decision,
            "error": error,
            "latency_ms": latency_ms,
        },
    )


def log_coarse_reject(
    path: Path,
    *,
    message_id: str,
    subject: str,
    reason: str,
) -> None:
    append_jsonl(
        path,
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "message_id": message_id,
            "subject": subject,
            "decision": "reject",
            "reason": reason,
        },
    )
