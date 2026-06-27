"""Guard the current paper-day status / go-no-go summary doc.

`docs/PAPER_DAY_CURRENT_STATUS.md` is the single human entry-point summary before
the next regular KR market session. These guards lock its load-bearing claims so a
future edit cannot drop the verdict framing (parser already verified; pilot-3
formal verdict NEEDS_REVIEW due to a missing stdout-envelope), the
envelope/runbook-validation-only scope of any future run, the Operator/Reviewer
entry points, or the safety prohibitions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_CURRENT_STATUS.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "current status doc must exist"
    return DOC.read_text(encoding="utf-8")


def test_current_status_doc_exists(doc_text: str) -> None:
    assert doc_text.strip(), "current status doc must not be empty"


def test_next_live_run_is_envelope_runbook_validation_only(doc_text: str) -> None:
    assert "envelope/runbook validation only" in doc_text
    assert "not" in doc_text.lower() and "activation" in doc_text.lower()
    assert "not" in doc_text.lower() and "live orders" in doc_text.lower()


def test_parser_verification_already_complete(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "parser" in lowered
    assert "already complete" in lowered
    assert "VERIFIED" in doc_text


def test_pilot3_formal_verdict_needs_review_due_missing_envelope(doc_text: str) -> None:
    assert "pilot-3" in doc_text
    assert "NEEDS_REVIEW" in doc_text
    assert "stdout-envelope.json" in doc_text
    lowered = doc_text.lower()
    assert "not captured" in lowered or "missing" in lowered


def test_operator_and_reviewer_entry_points_present(doc_text: str) -> None:
    # Operator entry points.
    assert "docs/PAPER_DAY_NEXT_OPERATOR_PACKET.md" in doc_text
    assert "ops/check_next_paper_day_readiness.py" in doc_text
    # Reviewer entry points.
    assert "docs/PAPER_DAY_REVIEWER_INTAKE_CHECKLIST.md" in doc_text
    assert "ops/validate_paper_day_summary.py" in doc_text
    assert "ops/render_paper_day_report.py" in doc_text


def test_prohibitions_present(doc_text: str) -> None:
    assert "no live orders" in doc_text
    assert "no activation" in doc_text
    assert "no daemon" in doc_text
    assert "no automatic restart" in doc_text
    lowered = doc_text.lower()
    assert "live kis is operator-only" in lowered or "operator-only" in lowered
    assert "traceback" in lowered
    assert "no secret" in lowered or "secret" in lowered


def test_known_backlog_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "backlog" in lowered
    assert "p3" in lowered
    assert "tech_debt" in lowered or "tech debt" in lowered


def test_links_to_dry_run_rehearsal(doc_text: str) -> None:
    assert "docs/PAPER_DAY_OPERATOR_DRY_RUN_REHEARSAL.md" in doc_text


def test_links_to_readiness_troubleshooting(doc_text: str) -> None:
    assert "docs/PAPER_DAY_READINESS_TROUBLESHOOTING.md" in doc_text
