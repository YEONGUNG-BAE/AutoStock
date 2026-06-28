"""Guard the Controlled Day 1 no-write readiness rollup / exit-criteria doc.

`docs/CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md` is the 2D rollup: it summarizes
what 2A/2B/2C prove, what remains unproven, and the exit + do-not-proceed gates that
must hold before any later tiny-live readiness work begins. These guards lock its
no-Paper-Day / no-tiny / no-runbook framing, its track separation, the 2A/2B/2C
artifact table, the proven boundaries, the open gaps (incl. the run-free safety-block
emitter gap), the 2D exit criteria, the do-not-proceed-to-tiny-live gate, the
recommended-next-track options, and the cross-doc back-links.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md"
BOUNDARY_DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md"
CHECKLIST_DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_OPERATOR_EVIDENCE_CHECKLIST.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "readiness rollup doc must exist"
    return DOC.read_text(encoding="utf-8")


def test_doc_exists(doc_text: str) -> None:
    assert doc_text.strip(), "rollup doc must not be empty"


def test_not_paper_day_not_tiny_not_runbook(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "this is not paper-day" in lowered
    assert "this is not tiny-live order validation" in lowered
    assert "this is not a live / tiny / write runbook" in lowered or (
        "this is not a live/tiny/write runbook" in lowered
    )
    assert "controlled day 1 no-write readiness rollup" in lowered
    assert "exit-criteria" in lowered or "exit criteria" in lowered


def test_track_separation_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "track separation" in lowered
    assert "paper-day kis live market-data validation is separate" in lowered
    assert "controlled day 1 no-write order-decision readiness is separate" in lowered
    assert "tiny-live order path validation is separate and later" in lowered
    # Paper-Day, even if run, authorizes nothing here.
    assert "does not authorize controlled day 1 live/write behavior" in lowered or (
        "does not authorize controlled day 1 live / write behavior" in lowered
    )
    assert "does not authorize tiny-live orders" in lowered


def test_completed_artifacts_table(doc_text: str) -> None:
    # Table with the required columns and 2A/2B/2C rows.
    assert "| Phase | Artifact | Status | What it proves |" in doc_text
    assert "docs/CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md" in doc_text
    assert "tests/test_controlled_day1_no_write_order_decision_contract.py" in doc_text
    assert "docs/CONTROLLED_DAY1_NO_WRITE_OPERATOR_EVIDENCE_CHECKLIST.md" in doc_text
    for phase in ("2A", "2B", "2C"):
        assert phase in doc_text, f"completed artifacts table must list phase {phase}"


def test_proven_no_write_boundaries_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "proven no-write boundaries" in lowered
    assert "hypothetical orderintent" in lowered or "hypothetical order intent" in lowered
    assert "reaches generated without broker construction" in lowered or (
        "without constructing broker" in lowered
    )
    assert "KisLiveOrderBlockedError" in doc_text
    assert "no src/ execution path constructs" in lowered or (
        "no `src/` execution path constructs" in DOC.read_text(encoding="utf-8").lower()
    )
    assert "non-paper" in lowered
    assert "generated_order_intent" in doc_text and "broker_order_result" in doc_text
    assert "allowed evidence" in lowered and "abort criteria" in lowered and "stop boundary" in lowered


def test_not_proven_open_gaps_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "not proven" in lowered or "open gaps" in lowered
    # Run-free safety-block emitter gap and its full flag set.
    assert "safety-block emitter" in lowered
    for flag in (
        "paper_only=true",
        "activation_authorized=false",
        "real_order_adapter_constructed=false",
        "orders=0",
        "fills=0",
    ):
        assert flag in doc_text, f"open-gaps must name {flag}"
    assert "no actual controlled day 1 no-write rehearsal run" in lowered
    assert "no live/tiny order path is validated" in lowered or (
        "no live / tiny order path is validated" in lowered
    )
    assert "no real broker write path is enabled" in lowered
    assert "no production monitoring/reconciliation is validated" in lowered or (
        "no production monitoring / reconciliation is validated" in lowered
    )


def test_exit_criteria_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "exit criteria for 2d" in lowered
    assert "2a/2b/2c artifacts exist" in lowered
    assert "contract tests are green" in lowered
    assert "links back to the no-write boundary" in lowered
    assert "full acceptance is green" in lowered
    assert "no runtime code changed" in lowered
    assert "no live/network/kis/tiny/write command was run" in lowered or (
        "no live / network / kis / tiny / write command was run" in lowered
    )
    assert "safety-block emitter gap remains explicitly documented" in lowered


def test_do_not_proceed_to_tiny_live_gate(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "do not proceed to tiny-live readiness until" in lowered or (
        "do not proceed to tiny live readiness until" in lowered
    )
    assert "2d rollup is reviewed" in lowered
    assert "contract tests remain green" in lowered
    assert "operator checklist is reviewed" in lowered
    assert "explicit human approval for tiny-live readiness" in lowered
    assert "separate tiny-live readiness plan exists" in lowered
    # Risk-cap requirements deferred to a separate later document.
    for req in ("risk caps", "max order count", "max notional", "max loss", "kill switch", "cancel path", "reconciliation"):
        assert req in lowered, f"tiny-live gate must defer {req!r} to a later doc"
    assert "no live adapter construction path exists outside an explicitly approved later track" in lowered


def test_recommended_next_track_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "recommended next track after 2d" in lowered
    assert "do not start tiny-live automatically" in lowered
    assert "safety-block emitter design proposal" in lowered
    assert "tiny-live readiness requirements inventory" in lowered
    assert "docs/tests only" in lowered or "docs / tests only" in lowered
    assert "requires explicit human approval" in lowered


def test_authorizes_nothing(doc_text: str) -> None:
    lowered = doc_text.lower()
    for token in (
        "no live kis",
        "no network",
        "no live orders",
        "no activation",
        "no daemon",
        "no automatic restart",
        "no live adapter construction",
        "no submit_order implementation",
        "no tiny-live order path",
    ):
        assert token in lowered, f"rollup must assert prohibition: {token!r}"


def test_boundary_and_checklist_link_to_rollup() -> None:
    boundary = BOUNDARY_DOC.read_text(encoding="utf-8")
    checklist = CHECKLIST_DOC.read_text(encoding="utf-8")
    assert "docs/CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md" in boundary
    assert "docs/CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md" in checklist
