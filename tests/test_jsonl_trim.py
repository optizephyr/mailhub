"""JSONL 日志行数上限：超出后只保留最新记录。"""

from __future__ import annotations

import json
from pathlib import Path

from mail_to_calendar.lifecycle_log import JSONL_MAX_LINES, append_jsonl


def test_append_jsonl_keeps_newest_max_lines(tmp_path: Path):
    path = tmp_path / "logs" / "mail_lifecycle.jsonl"
    for i in range(JSONL_MAX_LINES + 25):
        append_jsonl(path, {"i": i})

    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == JSONL_MAX_LINES
    records = [json.loads(ln) for ln in lines]
    assert records[0]["i"] == 25
    assert records[-1]["i"] == JSONL_MAX_LINES + 24


def test_append_jsonl_under_limit_untouched(tmp_path: Path):
    path = tmp_path / "x.jsonl"
    for i in range(3):
        append_jsonl(path, {"i": i})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["i"] == 0
