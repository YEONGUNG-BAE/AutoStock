"""Guard the Operator runbook's offline-validator / stdout-envelope guidance.

`docs/PAPER_DAY_OPERATOR_RUNBOOK.md` documents how the Operator validates a run's
artifacts offline. These guards lock the envelope-handling guidance so a future
edit cannot drop the wrong-run identity check, the missing/malformed-envelope
warnings, or the statement that the validator never infers/repairs envelope-only
fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_OPERATOR_RUNBOOK.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "Operator runbook must exist"
    return DOC.read_text(encoding="utf-8")


def test_runbook_mentions_offline_validator(doc_text: str) -> None:
    assert "ops/validate_paper_day_summary.py" in doc_text


def test_runbook_keeps_missing_and_malformed_envelope_warnings(doc_text: str) -> None:
    assert "missing_from_persisted_summary" in doc_text
    lowered = doc_text.lower()
    assert "missing" in lowered
    assert "malformed" in lowered
    assert "NEEDS_REVIEW" in doc_text


def test_runbook_states_validator_does_not_infer_or_repair(doc_text: str) -> None:
    # The validator must not invent envelope-only fields. Normalize whitespace so
    # the assertion is robust to line wrapping in the prose.
    normalized = " ".join(doc_text.split())
    assert "never infers or repairs envelope-only fields" in normalized


def test_runbook_documents_wrong_run_envelope_identity_check(doc_text: str) -> None:
    assert "envelope_run_mismatch" in doc_text
    assert "_envelope_capture.run_id" in doc_text
    lowered = doc_text.lower()
    assert "wrong-run" in lowered or "wrong run" in lowered
    assert "identity" in lowered
    for field in ("run_id", "session_date", "symbol"):
        assert field in doc_text
    assert "can never be PASS" in doc_text or "cannot be PASS" in doc_text


def test_runbook_references_2026_06_30_short_validation_without_authorization(doc_text: str) -> None:
    for token in (
        "2026-06-30",
        "paper-day-source-diagnostics-validation-01h-01",
        "0c6229f939944050a87061fe9735a832",
        "a0bbe4600e44a12295316b6b5feae9c83ef08bb6",
        "short 1-hour source diagnostics validation PASS",
        "not a full-day PASS",
        "does not authorize full paper",
        "tiny-live",
        "live orders",
    ):
        assert token in doc_text


def test_runbook_references_2026_06_30_rest_of_session_without_authorization(doc_text: str) -> None:
    for token in (
        "2026-06-30",
        "paper-day-source-diagnostics-validation-rest-of-session-01",
        "479aea40b15c41cf92dc5067ab704da8",
        "rest-of-session stability validation PASS",
        "malformed_control_after_ack=626",
        "source_iterator_unknown_after_ack=1",
        "reconnect_stream_reset=1251",
        "no terminal source exhaustion",
        "not a full-day PASS from market open",
        "does not authorize full paper",
        "tiny-live",
        "live orders",
    ):
        assert token in doc_text
