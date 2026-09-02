"""Tests for mailhub.runtime.migrate_bark_misclassified."""

from __future__ import annotations

import json
from pathlib import Path

from mailhub.runtime.migrate_bark_misclassified import (
    reclassify_bark_misclassified,
)
from mailhub.store.sqlite import EventStore


SHOPEE_BOOKING_CONFIRMED_MID = "<1788329954863_54283_104203_71424.sc-10-9-180-166-inbound0@mail.mokahr.com>"
SHOPEE_PLEASE_BOOK_MID = "<1788329954863_54283_104203_71425.sc-10-9-180-166-inbound0@mail.mokahr.com>"
CTRIP_BOOKING_MID = "<1788319344506_54283_104203_59683.sc-10-9-185-168-inbound0@mail.mokahr.com>"


def _write_lifecycle(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def _lifecycle_entry(
    *,
    message_id: str,
    subject: str,
    outcome_status: str,
    stages: list[dict],
    from_: str = "shopee-no-reply@mail.mokahr.com",
    ts: str = "2026-09-02T06:26:16+00:00",
) -> dict:
    return {
        "v": 1,
        "trace_id": f"test-{message_id[:8]}",
        "ts": ts,
        "run": {"dry_run": False, "full": True, "source_id": "qq.default"},
        "mail": {
            "message_id": message_id,
            "uid": 0,
            "subject": subject,
            "from": from_,
            "date": "2026-09-02T06:26:16+08:00",
        },
        "outcome": {
            "status": outcome_status,
            "summary": "applied"
            if outcome_status == "applied"
            else outcome_status,
        },
        "stages": stages,
    }


def _seed_action_and_processed(
    store: EventStore,
    *,
    source_id: str,
    message_id: str,
    action_type: str = "bark.push",
    result: str = "push",
    error: str | None = None,
    idempotency_prefix: str | None = None,
) -> str:
    """在 action_executions 与 processed_messages 中预置一行；返回 idempotency_key。"""
    prefix = idempotency_prefix or f"bark:{source_id}:{message_id}"
    # bark.push 的 idempotency_key 在生产中为 `bark:<source_id>:<msg>`；
    # 但 _find_bark_action_rows 按 `<source_id>:<msg>:%` 前缀匹配，所以非 bark 也兼容。
    idempotency_key = (
        prefix
        if action_type == "bark.push"
        else f"{source_id}:{message_id}:{action_type}:{result}"
    )
    store._conn.execute(
        """
        INSERT INTO action_executions
        (idempotency_key, action_type, status, external_id, error, executed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            idempotency_key,
            action_type,
            "succeeded",
            None,
            error,
            "2026-09-02T06:26:16",
        ),
    )
    store.mark_processed(message_id, "applied", source_id=source_id)
    store._conn.commit()
    return idempotency_key


def test_dry_run_finds_shopee_booking_confirmed(tmp_path: Path) -> None:
    """dry-run：扫到 1 条 Shopee「预约成功通知」Bark 误推；其他记录不动。"""
    log = tmp_path / "logs" / "mail_lifecycle.jsonl"
    _write_lifecycle(
        log,
        [
            _lifecycle_entry(
                message_id=SHOPEE_BOOKING_CONFIRMED_MID,
                subject="【Shopee】预约面试成功通知",
                outcome_status="applied",
                stages=[
                    {"name": "apply", "result": "pushed"},
                ],
            ),
            _lifecycle_entry(
                message_id=SHOPEE_PLEASE_BOOK_MID,
                subject="【Shopee】校园招聘预约面试时间选择",
                outcome_status="applied",
                stages=[
                    {"name": "apply", "result": "pushed"},
                ],
            ),
            _lifecycle_entry(
                message_id=CTRIP_BOOKING_MID,
                subject="【携程集团】请尽快完成AI面试",
                outcome_status="applied",
                stages=[
                    {"name": "apply", "result": "pushed"},
                ],
                from_="trip-no-reply@mail.mokahr.com",
            ),
        ],
    )
    store = EventStore(tmp_path / "synced.sqlite")
    try:
        _seed_action_and_processed(
            store,
            source_id="qq.default",
            message_id=SHOPEE_BOOKING_CONFIRMED_MID,
        )
        _seed_action_and_processed(
            store,
            source_id="qq.default",
            message_id=SHOPEE_PLEASE_BOOK_MID,
        )
        _seed_action_and_processed(
            store,
            source_id="qq.default",
            message_id=CTRIP_BOOKING_MID,
        )

        result = reclassify_bark_misclassified(
            store, log, source_id="qq.default", dry_run=True
        )

        assert result.scanned_lifecycle_entries == 3
        # 只有 Shopee「预约成功通知」命中 CONFIRMED_BOOKING_SIGNALS
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.message_id == SHOPEE_BOOKING_CONFIRMED_MID
        # 「【Shopee】预约面试成功通知」里的「面试」隔开了「预约」与「成功」，
        # 所以匹配上的关键字是 "面试成功通知" / "预约面试成功" / "面试已预约" 等，
        # 不再是单纯的 "预约成功"。
        assert rec.keyword in {"面试成功通知", "预约面试成功", "面试已预约", "面试已确认"}
        # dry_run 不应写任何东西
        assert rec.error_annotated is False
        assert rec.purged_processed is False
        assert result.changed == []

        # DB 状态保持原样
        assert store.already_processed(SHOPEE_BOOKING_CONFIRMED_MID, "qq.default")
        assert store.already_processed(SHOPEE_PLEASE_BOOK_MID, "qq.default")
        assert store.already_processed(CTRIP_BOOKING_MID, "qq.default")
        receipt = store.get_action_receipt(
            f"bark:qq.default:{SHOPEE_BOOKING_CONFIRMED_MID}"
        )
        assert receipt is not None
        assert (receipt.get("error") or "") == ""
    finally:
        store.close()


def test_apply_annotates_and_purges(tmp_path: Path) -> None:
    """非 dry-run：action_executions.error 标注、processed_messages 清掉、action_executions 行保留。"""
    log = tmp_path / "logs" / "mail_lifecycle.jsonl"
    _write_lifecycle(
        log,
        [
            _lifecycle_entry(
                message_id=SHOPEE_BOOKING_CONFIRMED_MID,
                subject="【Shopee】预约面试成功通知",
                outcome_status="applied",
                stages=[{"name": "apply", "result": "pushed"}],
            ),
        ],
    )
    store = EventStore(tmp_path / "synced.sqlite")
    try:
        _seed_action_and_processed(
            store,
            source_id="qq.default",
            message_id=SHOPEE_BOOKING_CONFIRMED_MID,
        )
        assert store.already_processed(SHOPEE_BOOKING_CONFIRMED_MID, "qq.default")

        result = reclassify_bark_misclassified(
            store, log, source_id="qq.default", dry_run=False
        )

        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.error_annotated is True
        assert rec.purged_processed is True

        # processed_messages 已清
        assert not store.already_processed(
            SHOPEE_BOOKING_CONFIRMED_MID, "qq.default"
        )

        # action_executions 行保留，但 error 已填
        receipt = store.get_action_receipt(
            f"bark:qq.default:{SHOPEE_BOOKING_CONFIRMED_MID}"
        )
        assert receipt is not None
        assert receipt["status"] == "succeeded"
        assert receipt["error"].startswith("misclassified:")
        # 「【Shopee】预约面试成功通知」里的「面试」隔开了「预约」与「成功」，
        # 所以命中的关键字是 "面试成功通知"，不是 "预约成功"。
        assert "面试成功通知" in receipt["error"]
        # executed_at 应保留原值（不是 NOW）
        assert receipt["executed_at"] == "2026-09-02T06:26:16"
    finally:
        store.close()


def test_idempotent_second_run(tmp_path: Path) -> None:
    """重跑应跳过已标注记录（不动 processed_messages、不重写 error）。"""
    log = tmp_path / "logs" / "mail_lifecycle.jsonl"
    _write_lifecycle(
        log,
        [
            _lifecycle_entry(
                message_id=SHOPEE_BOOKING_CONFIRMED_MID,
                subject="【Shopee】预约面试成功通知",
                outcome_status="applied",
                stages=[{"name": "apply", "result": "pushed"}],
            ),
        ],
    )
    store = EventStore(tmp_path / "synced.sqlite")
    try:
        _seed_action_and_processed(
            store,
            source_id="qq.default",
            message_id=SHOPEE_BOOKING_CONFIRMED_MID,
        )

        # 第一次跑：标注 + 清理
        first = reclassify_bark_misclassified(
            store, log, source_id="qq.default", dry_run=False
        )
        assert len(first.records) == 1
        assert first.skipped_already_annotated == 0

        # 此时 processed_messages 已清；为模拟「重跑」，手动重新 mark（sync 仍可能重新写入），
        # 然后验证第二次跑不会重复处理已标注的 bark 行
        store.mark_processed(SHOPEE_BOOKING_CONFIRMED_MID, "applied", source_id="qq.default")

        second = reclassify_bark_misclassified(
            store, log, source_id="qq.default", dry_run=False
        )
        # 第二次扫到 1 条，但因 error 已标记，被 skip
        assert second.skipped_already_annotated == 1
        # 不应二次清理（因为 idempotency_key 是同一条，跳过后不会再 unmark_processed）
        assert store.already_processed(SHOPEE_BOOKING_CONFIRMED_MID, "qq.default")
    finally:
        store.close()


def test_ignores_non_pushed_applied(tmp_path: Path) -> None:
    """apply/created（reminders/calendar）不视为 Bark 误判 —— 不被本迁移改。"""
    log = tmp_path / "logs" / "mail_lifecycle.jsonl"
    _write_lifecycle(
        log,
        [
            _lifecycle_entry(
                message_id=SHOPEE_BOOKING_CONFIRMED_MID,
                subject="【Shopee】预约面试成功通知",
                outcome_status="applied",
                stages=[{"name": "apply", "result": "created"}],  # 不是 pushed
            ),
        ],
    )
    store = EventStore(tmp_path / "synced.sqlite")
    try:
        # 创建的是 reminders.create 行，不是 bark.push
        _seed_action_and_processed(
            store,
            source_id="qq.default",
            message_id=SHOPEE_BOOKING_CONFIRMED_MID,
            action_type="reminders.create",
            result="create",
            idempotency_prefix=None,  # 用 <source_id>:<msg>:<action>:<result> 格式
        )
        result = reclassify_bark_misclassified(
            store, log, source_id="qq.default", dry_run=True
        )
        assert result.records == []
    finally:
        store.close()


def test_missing_lifecycle_file_is_noop(tmp_path: Path) -> None:
    """lifecycle jsonl 不存在时返回空 result，不抛错。"""
    log = tmp_path / "logs" / "mail_lifecycle.jsonl"  # 不创建
    store = EventStore(tmp_path / "synced.sqlite")
    try:
        result = reclassify_bark_misclassified(
            store, log, source_id="qq.default", dry_run=True
        )
        assert result.records == []
        assert result.scanned_lifecycle_entries == 0
    finally:
        store.close()
