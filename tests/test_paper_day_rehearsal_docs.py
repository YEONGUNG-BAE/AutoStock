"""RTM-7c.10 — guard the Monday preflight rehearsal doc against drift."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_MONDAY_PREFLIGHT_REHEARSAL.md"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "paper_day_reports"

_FIXTURES = (
    "pass_startup_like",
    "no_go_health_not_ready",
    "fail_source_approval_failed",
    "needs_review_missing_envelope",
    "fail_sensitive_data_present",
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _FIXTURES)
def test_doc_mentions_each_fixture(doc_text: str, name: str) -> None:
    assert name in doc_text


@pytest.mark.parametrize("name", _FIXTURES)
def test_each_named_fixture_exists(name: str) -> None:
    assert (FIXTURE_ROOT / name / "summary.json").is_file()


def test_doc_mentions_validator_command(doc_text: str) -> None:
    assert "ops/validate_paper_day_summary.py" in doc_text


def test_doc_mentions_render_command(doc_text: str) -> None:
    assert "ops/render_paper_day_report.py" in doc_text


def test_doc_prohibits_live_kis(doc_text: str) -> None:
    assert "--live-kis" in doc_text


def test_doc_says_missing_envelope_omits_envelope(doc_text: str) -> None:
    assert "needs_review_missing_envelope" in doc_text
    assert "omit" in doc_text and "--envelope" in doc_text


def test_doc_references_triage_playbook(doc_text: str) -> None:
    assert "PAPER_DAY_FAILURE_TRIAGE_PLAYBOOK.md" in doc_text
