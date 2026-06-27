"""Guard the Reviewer intake checklist for paper-day handoff.

`docs/PAPER_DAY_REVIEWER_INTAKE_CHECKLIST.md` is the Reviewer-side acceptance
checklist consumed after an Operator completes a future attended paper-day run and
hands off `RUN_DIR` artifacts. These guards lock its load-bearing properties so a
future edit cannot drop the required-artifact list, the same-run envelope identity
check, the hard-FAIL safety signals, or the secret/raw-frame prohibitions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_REVIEWER_INTAKE_CHECKLIST.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "Reviewer intake checklist must exist"
    return DOC.read_text(encoding="utf-8")


def test_checklist_exists(doc_text: str) -> None:
    assert doc_text.strip(), "checklist must not be empty"
    assert "Reviewer" in doc_text
    assert "intake" in doc_text.lower() or "Intake" in doc_text


def test_mentions_required_artifacts(doc_text: str) -> None:
    for artifact in (
        "summary.json",
        "evidence.jsonl",
        "stdout-envelope.json",
        "db",
        "review-report.md",
    ):
        assert artifact in doc_text, f"checklist must list required artifact {artifact}"


def test_required_terminal_values_present(doc_text: str) -> None:
    assert "PILOT_EXIT" in doc_text
    lowered = doc_text.lower()
    assert "stdout-envelope" in lowered
    assert "printed" in lowered or "file path" in lowered


def test_git_runtime_hygiene_present(doc_text: str) -> None:
    assert "git status --short" in doc_text
    assert "git ls-files runtime" in doc_text


def test_validator_and_report_use_envelope(doc_text: str) -> None:
    assert "ops/validate_paper_day_summary.py" in doc_text
    assert "ops/render_paper_day_report.py" in doc_text
    assert '--envelope "$RUN_DIR/stdout-envelope.json"' in doc_text


def test_same_run_envelope_and_mismatch_present(doc_text: str) -> None:
    assert "envelope_run_mismatch" in doc_text
    assert "_envelope_capture.run_id" in doc_text
    for field in ("run_id", "session_date", "symbol"):
        assert field in doc_text
    assert "can never be PASS" in doc_text or "cannot be PASS" in doc_text


def test_pass_blockers_present(doc_text: str) -> None:
    for blocker in (
        "missing_from_persisted_summary",
        "envelope_malformed",
        "envelope_run_mismatch",
        "summary_publication_outcome",
        "cleanup_outcome",
    ):
        assert blocker in doc_text, f"checklist must list PASS blocker {blocker}"
    assert "WRITTEN" in doc_text
    assert "CLEAN" in doc_text
    assert "runtime lock" in doc_text.lower()


def test_hard_fail_safety_signals_present(doc_text: str) -> None:
    for signal in (
        "sensitive_data_present",
        "paper_only",
        "activation_authorized",
        "real_order_adapter_constructed",
        "automatic_restart",
    ):
        assert signal in doc_text, f"checklist must list hard-FAIL signal {signal}"
    lowered = doc_text.lower()
    assert "orders" in lowered and "fills" in lowered
    assert "nonterminal_journal" in doc_text


def test_no_go_examples_present(doc_text: str) -> None:
    assert "invalid_session_window" in doc_text
    lowered = doc_text.lower()
    assert "market closed" in lowered or "non-`open` session" in lowered or "session_state != OPEN" in doc_text
    assert "health not ready" in lowered


def test_secret_and_raw_frame_prohibitions_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "secret" in lowered
    assert "raw websocket frame" in lowered or "raw frame" in lowered
    assert "payload" in lowered
    for token in ("token", "app key", "approval key", "account", "traceback"):
        assert token in lowered, f"checklist must prohibit requesting {token}"
    assert "hand-edit" in lowered
    assert "live kis" in lowered
