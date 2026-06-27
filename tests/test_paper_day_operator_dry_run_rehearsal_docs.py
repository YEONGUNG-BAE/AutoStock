"""Guard the offline Operator dry-run rehearsal doc.

`docs/PAPER_DAY_OPERATOR_DRY_RUN_REHEARSAL.md` lets a human Operator finger-trace
the next paper-day command flow (variables, ordering, artifact paths) WITHOUT
executing any live KIS command. These guards lock its offline/docs-only framing,
its prohibitions, the readiness-before-live ordering, the stdout-envelope capture
paths, the validator/report envelope path, and the cross-doc references so a future
edit cannot turn it into something that encourages a live run from Cursor/Claude.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_OPERATOR_DRY_RUN_REHEARSAL.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "dry-run rehearsal doc must exist"
    return DOC.read_text(encoding="utf-8")


def test_dry_run_rehearsal_doc_exists(doc_text: str) -> None:
    assert doc_text.strip(), "dry-run rehearsal doc must not be empty"


def test_offline_docs_only_and_prohibitions(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "offline" in lowered
    assert "docs-only" in lowered or "docs only" in lowered
    assert "do not run live kis" in lowered
    assert "do not use network" in lowered
    assert "do not run startup smoke" in lowered
    assert "do not run the attended paper-day pilot" in lowered or "do not run attended paper-day pilot" in lowered
    assert "do not use secrets" in lowered
    assert "do not print config contents" in lowered
    assert "do not run live orders" in lowered
    assert "do not activate runtime" in lowered


def test_synthetic_example_variables_present(doc_text: str) -> None:
    assert 'SESSION_DATE="<YYYY-MM-DD>"' in doc_text
    assert 'RUN_LABEL="<operator-selected-label>"' in doc_text
    assert 'DURATION_SECONDS="<bounded-duration>"' in doc_text
    assert 'RUN_DIR="runtime/paper-day/$SESSION_DATE/$RUN_LABEL"' in doc_text


def test_references_checker_and_live_command_but_warns_not_to_execute(doc_text: str) -> None:
    assert "ops/check_next_paper_day_readiness.py" in doc_text
    assert "ops/run_attended_paper_day.py" in doc_text
    # The live command must carry an explicit do-not-execute warning.
    lowered = doc_text.lower()
    assert "do not execute this block in cursor/claude" in lowered
    assert "operator-only" in lowered


def test_readiness_checker_before_live_run_command(doc_text: str) -> None:
    checker_ref = "ops/check_next_paper_day_readiness.py"
    run_ref = "ops/run_attended_paper_day.py"
    assert doc_text.index(checker_ref) < doc_text.index(run_ref)
    # Readiness checker consumes the Operator-selected variables.
    assert '--session-date "$SESSION_DATE"' in doc_text
    assert '--run-label "$RUN_LABEL"' in doc_text
    assert '--duration-seconds "$DURATION_SECONDS"' in doc_text
    assert '--run-dir "$RUN_DIR"' in doc_text


def test_stdout_envelope_capture_paths_present(doc_text: str) -> None:
    assert '--stdout-envelope-out "$RUN_DIR/stdout-envelope.json"' in doc_text
    assert '--json > "$RUN_DIR/stdout-envelope.shell.json"' in doc_text


def test_validator_report_use_envelope_path(doc_text: str) -> None:
    assert "ops/validate_paper_day_summary.py" in doc_text
    assert "ops/render_paper_day_report.py" in doc_text
    assert '--envelope "$RUN_DIR/stdout-envelope.json"' in doc_text


def test_references_current_status_and_reviewer_intake(doc_text: str) -> None:
    assert "docs/PAPER_DAY_CURRENT_STATUS.md" in doc_text
    assert "docs/PAPER_DAY_REVIEWER_INTAKE_CHECKLIST.md" in doc_text


def test_monday_morning_final_manual_checks_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "monday morning final manual checks" in lowered
    assert "session_state=OPEN" in doc_text
    assert "regular KR market session" in doc_text
