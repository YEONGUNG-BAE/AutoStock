"""Guard the Tiny-live Readiness Requirements Inventory (2F).

`docs/TINY_LIVE_READINESS_REQUIREMENTS_INVENTORY.md` is a docs-only requirements
inventory for a *future* tiny-live readiness track. It defines what must exist before
any future tiny-live readiness work begins, while explicitly authorizing no tiny-live
run, no live order, no live adapter wiring, no submit_order implementation, no
activation path, and no executable runbook. These guards lock its framing, track
separation, prerequisites, the requirements table (columns + every required area),
the allowed status vocabulary, the no-false-Complete rule, the hard blockers, the
forbidden-in-this-task list, the recommended next step, and the back-links.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "TINY_LIVE_READINESS_REQUIREMENTS_INVENTORY.md"
ROLLUP_DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md"
PROPOSAL_DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_SAFETY_BLOCK_EMITTER_PROPOSAL.md"

REQUIREMENT_AREAS = (
    "human approval",
    "activation gate",
    "paper/live mode separation",
    "live adapter construction authorization",
    "submit_order implementation boundary",
    "max notional cap",
    "max order count",
    "max daily loss",
    "one-symbol constraint",
    "one-action / one-order constraint",
    "kill switch",
    "cancel path",
    "order reject handling",
    "fill handling",
    "position reconciliation",
    "cash reconciliation",
    "duplicate order prevention",
    "broker disconnect handling",
    "evidence schema",
    "secret/log redaction",
    "runtime artifact policy",
    "operator abort criteria",
    "reviewer acceptance criteria",
    "rollback / disable switch",
    "no daemon / no automatic restart",
)

ALLOWED_STATUS_VALUES = (
    "Not started",
    "Existing partial evidence",
    "Future design required",
    "Future implementation required",
    "Future approval required",
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "tiny-live readiness requirements inventory must exist"
    return DOC.read_text(encoding="utf-8")


def test_doc_exists(doc_text: str) -> None:
    assert doc_text.strip(), "inventory doc must not be empty"


def test_framing_what_this_is_not(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "this is not paper-day" in lowered
    assert "this is not controlled day 1 no-write readiness" in lowered
    assert "this is not tiny-live order validation" in lowered
    assert "this is not a live / tiny / write runbook" in lowered or (
        "this is not a live/tiny/write runbook" in lowered
    )
    assert "this is a requirements inventory only" in lowered


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
        assert token in lowered, f"inventory must assert prohibition: {token!r}"


def test_track_separation_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "track separation" in lowered
    assert "paper-day kis live market-data validation remains separate" in lowered
    assert "controlled day 1 no-write order-decision readiness remains separate" in lowered
    assert "tiny-live readiness is a later requirements/planning track only" in lowered or (
        "tiny-live readiness is a later requirements / planning track only" in lowered
    )
    assert "requires explicit human approval" in lowered
    assert "does not authorize tiny-live" in lowered


def test_prerequisites_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "prerequisites before tiny-live readiness work can begin" in lowered
    assert "paper-day market-data track remains green or explicitly reviewed" in lowered
    assert "2a/2b/2c/2d/2e artifacts are reviewed" in lowered
    assert "no-write contract tests remain green" in lowered
    assert "safety-block emitter gap is accepted or resolved" in lowered
    assert "explicit human approval exists for tiny-live readiness planning" in lowered
    assert "no live adapter construction path exists outside an approved later track" in lowered
    assert "full acceptance is green" in lowered


def test_requirements_table_columns(doc_text: str) -> None:
    assert (
        "| Requirement area | Requirement | Why it matters | "
        "Evidence needed before future tiny-live validation | Status |"
    ) in doc_text


def test_all_requirement_areas_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    for area in REQUIREMENT_AREAS:
        assert area in lowered, f"requirements inventory must cover area: {area}"


def test_allowed_status_values_documented(doc_text: str) -> None:
    # The doc must enumerate the allowed status vocabulary.
    for status in ALLOWED_STATUS_VALUES:
        assert status in doc_text, f"inventory must document status value: {status!r}"


def test_no_false_complete_status(doc_text: str) -> None:
    # No requirement row may be marked Complete (no checked-in tiny-live evidence
    # exists). The only acceptable mention of "Complete" is the rule forbidding it.
    rule_present = (
        "Do not mark" in doc_text and "Complete" in doc_text
    )
    assert rule_present, "inventory must state the no-false-Complete rule"
    # Ensure no table row uses a Complete status cell.
    for line in doc_text.splitlines():
        if line.lstrip().startswith("|") and "| Complete" in line:
            raise AssertionError(f"requirement row must not be Complete: {line!r}")


def test_hard_blockers_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "hard blockers for future tiny-live validation" in lowered
    assert "no explicit human approval" in lowered
    assert "safety-block emitter gap unresolved or not accepted" in lowered
    assert "activation gate unclear" in lowered
    assert "live adapter construction path unclear" in lowered
    assert "submit_order boundary unclear" in lowered
    assert "risk caps unspecified" in lowered
    assert "cancel/reconciliation path unspecified" in lowered or (
        "cancel / reconciliation path unspecified" in lowered
    )
    assert "evidence schema missing" in lowered
    assert "secrets/log redaction unproven" in lowered or "secrets / log redaction unproven" in lowered
    assert "acceptance not green" in lowered
    assert "runtime artifact/config/secret leakage" in lowered or (
        "runtime artifact / config / secret leakage" in lowered
    )


def test_forbidden_in_this_task_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "forbidden in this 2f task" in lowered
    assert "no live kis" in lowered
    assert "no network" in lowered
    assert "no live orders" in lowered
    assert "no activation" in lowered
    assert "no submit_order implementation" in lowered
    assert "no live adapter wiring" in lowered
    assert "no tiny-live runbook" in lowered
    assert "no executable commands for tiny-live" in lowered
    assert "no runtime code changes" in lowered
    # Secret/token families enumerated as prohibitions only.
    for token in ("account", "secret", "token", "url", "app key", "approval key", "raw frame", "traceback"):
        assert token in lowered, f"forbidden list must name {token!r}"


def test_recommended_next_step_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "recommended next step after 2f" in lowered
    assert "do not start tiny-live automatically" in lowered
    assert "offline handoff index" in lowered
    assert "operator-run paper-day market-data validation" in lowered
    assert "gated by explicit human approval" in lowered


def test_rollup_and_proposal_link_to_inventory() -> None:
    rollup = ROLLUP_DOC.read_text(encoding="utf-8")
    proposal = PROPOSAL_DOC.read_text(encoding="utf-8")
    assert "docs/TINY_LIVE_READINESS_REQUIREMENTS_INVENTORY.md" in rollup
    assert "docs/TINY_LIVE_READINESS_REQUIREMENTS_INVENTORY.md" in proposal
