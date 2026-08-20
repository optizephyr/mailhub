"""Corpus evaluation for the rule engine against tests/fixtures/email_corpus/labels.json."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailhub.plugins.policies.qiuzhao.parser import (
    classify_stage,
    detect_action,
    detect_event_type,
    guess_company,
    heuristic_parse,
)
from mailhub.plugins.policies.qiuzhao.rules import coarse_filter
from tests.eml_loader import EMAIL_EXAMPLE_DIR, iter_labeled_cases, load_labels


def _cases():
    return iter_labeled_cases()


@pytest.mark.parametrize(
    "case,mail",
    _cases(),
    ids=[c["id"] for c, _ in _cases()],
)
def test_rules_corpus_case(case, mail):
    expect = case["expect"]
    blob = f"{mail.subject}\n{mail.body}"

    coarse = coarse_filter(mail)
    assert coarse.passed is expect["coarse_pass"], (
        f"{case['id']}: coarse_pass want={expect['coarse_pass']} "
        f"got={coarse.passed} reason={coarse.reason}"
    )

    if not expect["coarse_pass"]:
        return

    if expect["stage"] is not None:
        assert classify_stage(blob) == expect["stage"], case["id"]
    if expect["action"] is not None:
        assert detect_action(blob) == expect["action"], case["id"]
    detected_event_type = expect.get("detected_event_type", expect["event_type"])
    if detected_event_type is not None:
        assert detect_event_type(blob) == detected_event_type, case["id"]

    if expect["company_contains"]:
        company = guess_company(mail.subject, mail.body)
        assert any(tok in company for tok in expect["company_contains"]), (
            f"{case['id']}: company={company!r} missing any of {expect['company_contains']}"
        )

    event = heuristic_parse(mail)
    want_event = expect["should_create_event"]
    want_reminder = expect.get("should_create_reminder", False)
    want_bark = expect.get("should_push_bark", False)
    if want_event or want_reminder or want_bark:
        assert event is not None, f"{case['id']}: expected event"
        if want_bark:
            assert event.event_type == "schedule_invite", case["id"]
            assert event.start_at == "", case["id"]
            assert event.end_at == "", case["id"]
        elif want_reminder:
            assert event.time_precision == "window", case["id"]
        else:
            assert event.time_precision != "window", case["id"]
        if expect["action"] is not None:
            assert event.action == expect["action"], case["id"]
        if expect["event_type"] is not None:
            assert event.event_type == expect["event_type"], case["id"]
        if expect["start_at_prefix"]:
            assert event.start_at.startswith(expect["start_at_prefix"]), (
                f"{case['id']}: start_at={event.start_at!r}"
            )
        if expect.get("end_at_prefix"):
            assert event.end_at.startswith(expect["end_at_prefix"]), (
                f"{case['id']}: end_at={event.end_at!r}"
            )
        if expect["must_not_start_at_prefix"]:
            assert not event.start_at.startswith(expect["must_not_start_at_prefix"]), case["id"]
        if expect["company_contains"]:
            blob_co = f"{event.company} {event.title}"
            assert any(tok in blob_co for tok in expect["company_contains"]), (
                f"{case['id']}: event company/title missing {expect['company_contains']}: "
                f"company={event.company!r} title={event.title!r}"
            )
    else:
        assert event is None, (
            f"{case['id']}: expected no event, got action={getattr(event, 'action', None)} "
            f"start={getattr(event, 'start_at', None)} title={getattr(event, 'title', None)}"
        )


def test_labels_index_covers_existing_emls():
    """Every non-synthetic production .eml should appear in labels.json (warn via assert)."""
    data = load_labels()
    indexed = {c["eml"] for c in data["cases"]}
    on_disk = {p.name for p in EMAIL_EXAMPLE_DIR.glob("*.eml")}
    missing = sorted(on_disk - indexed)
    assert not missing, f"eml files not in labels.json: {missing}"


def test_labels_json_paths_exist():
    data = load_labels()
    missing = [
        c["eml"]
        for c in data["cases"]
        if not (EMAIL_EXAMPLE_DIR / c["eml"]).is_file()
    ]
    assert not missing, f"labels.json points to missing eml: {missing}"
