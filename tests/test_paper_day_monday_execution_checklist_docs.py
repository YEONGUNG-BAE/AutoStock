"""RTM-7c.11 — guard the Monday execution checklist doc against drift."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_MONDAY_EXECUTION_CHECKLIST.md"

_HOT_PATH_FILES = (
    "src/composition/attended_paper_day.py",
    "ops/run_attended_paper_day.py",
    "src/data/kis_ws_source.py",
    "src/data/kis_ws_auth.py",
    "src/broker/kis_transport.py",
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_mentions_expected_base_head(doc_text: str) -> None:
    assert "957303fe4666415eafff2d5ba771b856b28d7876" in doc_text
    assert "Expected HEAD" in doc_text


@pytest.mark.parametrize("path", _HOT_PATH_FILES)
def test_mentions_all_hot_path_files(doc_text: str, path: str) -> None:
    assert path in doc_text


def test_includes_shell_safe_redirect(doc_text: str) -> None:
    assert '--json > "$RUN_DIR/stdout-envelope.json"' in doc_text
    assert "PILOT_EXIT=$?" in doc_text
    assert "Do not pipe through tee." in doc_text


def test_includes_operator_live_kis_command_but_prohibits_cursor_execution(doc_text: str) -> None:
    # --live-kis appears (Operator command + prohibition text)...
    assert "--live-kis" in doc_text
    # ...but Cursor/Claude is explicitly told never to execute any step.
    assert "Cursor/Claude never executes" in doc_text
    assert "only the human Operator runs" in doc_text


def test_includes_validator_command(doc_text: str) -> None:
    assert "ops/validate_paper_day_summary.py" in doc_text


def test_includes_report_command(doc_text: str) -> None:
    assert "ops/render_paper_day_report.py" in doc_text


def test_references_failure_triage_playbook(doc_text: str) -> None:
    assert "PAPER_DAY_FAILURE_TRIAGE_PLAYBOOK.md" in doc_text


def test_includes_pass_criteria(doc_text: str) -> None:
    assert "## PASS criteria" in doc_text
    assert "summary_publication_outcome = WRITTEN" in doc_text
    assert "orders > 0 is not required for PASS." in doc_text


def test_includes_safety_proof(doc_text: str) -> None:
    assert "## Safety proof" in doc_text
    assert "live order                         0" in doc_text


def test_requires_git_status_and_ls_files_runtime(doc_text: str) -> None:
    assert "git status --short" in doc_text
    assert "git ls-files runtime" in doc_text
