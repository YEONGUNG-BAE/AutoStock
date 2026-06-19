"""RTM-7c.9 — offline report fixtures rendered through the report generator.

Each fixture under ``tests/fixtures/paper_day_reports/<name>/`` is a synthetic
``summary.json`` (+ ``evidence.jsonl`` and usually ``stdout-envelope.json``) that
exercises one verdict shape the Monday operator will triage. These are fixtures,
not real run output: they contain no secret, raw HTTP response, or raw websocket
frame. The tests render them offline and assert the verdict and the triage-facing
sections without ever recomputing the verdict here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "ops"))

from render_paper_day_report import build_report  # noqa: E402
from validate_paper_day_summary import EXPECTED_SCHEMA_VERSION  # noqa: E402

FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "paper_day_reports"

# Sentinel substrings that must never appear in a synthetic fixture or a rendered
# report — they would signal a real credential / raw transport payload leaked in.
_FORBIDDEN_SENTINELS = (
    "APP_KEY",
    "APP_SECRET",
    "PSPKEY",
    "approval_key",
    "Authorization",
    "Bearer ",
    "Set-Cookie",
    "BEGIN PRIVATE KEY",
)


def _render(name: str, *, expect_source_kind: str | None = "kis_live"):
    fixture = FIXTURE_ROOT / name
    envelope = fixture / "stdout-envelope.json"
    return build_report(
        summary_path=fixture / "summary.json",
        evidence_path=fixture / "evidence.jsonl",
        envelope_path=envelope if envelope.is_file() else None,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind=expect_source_kind,
        max_timeline_rows=200,
    )


def test_pass_startup_like_fixture() -> None:
    md, result = _render("pass_startup_like")
    assert result["verdict"] == "PASS"
    assert "**verdict: PASS**" in md
    assert "summary_publication_outcome | WRITTEN | ok" in md
    assert "First failure" in md
    assert "None observed." in md


def test_no_go_health_not_ready_fixture() -> None:
    md, result = _render("no_go_health_not_ready")
    assert result["verdict"] == "NO_GO"
    assert "runtime outcome: NO_GO" in md
    assert "stop_reason: health_not_ready" in md
    # First-failure section is populated for a failed_closed run.
    assert "reason_code: health_not_ready" in md


def test_fail_source_approval_failed_fixture() -> None:
    md, result = _render("fail_source_approval_failed")
    assert result["verdict"] == "FAIL"
    assert "runtime outcome: FAIL" in md
    assert "stop_reason: source_approval_failed" in md
    assert "reason_code: source_approval_failed" in md
    assert "stage: source" in md
    # Publication did not land — PASS would be impossible regardless.
    assert "summary_publication_outcome | NOT_WRITTEN" in md


def test_needs_review_missing_envelope_fixture() -> None:
    md, result = _render("needs_review_missing_envelope")
    assert result["verdict"] == "NEEDS_REVIEW"
    assert "**verdict: NEEDS_REVIEW**" in md
    assert "missing_from_persisted_summary" in md
    assert "envelope: not provided" in md


def test_fail_sensitive_data_present_fixture() -> None:
    md, result = _render("fail_sensitive_data_present")
    assert result["verdict"] == "FAIL"
    assert "hard_fail: sensitive_data_present" in md
    assert "sensitive_data_present_any | true" in md


@pytest.mark.parametrize(
    "name",
    [
        "pass_startup_like",
        "no_go_health_not_ready",
        "fail_source_approval_failed",
        "needs_review_missing_envelope",
        "fail_sensitive_data_present",
    ],
)
def test_fixture_has_no_secret_sentinel(name: str) -> None:
    fixture = FIXTURE_ROOT / name
    md, _ = _render(name)
    blobs = [md]
    for child in fixture.iterdir():
        blobs.append(child.read_text(encoding="utf-8"))
    for blob in blobs:
        for sentinel in _FORBIDDEN_SENTINELS:
            assert sentinel not in blob, f"{name}: forbidden sentinel {sentinel!r}"
