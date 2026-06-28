"""Guard the Controlled Day 1 no-write safety-block emitter design proposal (2E).

`docs/CONTROLLED_DAY1_NO_WRITE_SAFETY_BLOCK_EMITTER_PROPOSAL.md` is a docs-only design
proposal for *how* to later close the run-free safety-block emitter gap, without
implementing any runtime code now. These guards lock its no-Paper-Day / no-tiny /
no-implementation framing, its problem statement (the full safety-block field set),
its design goals, its proposed shape, its future contract candidates, its non-goals,
its decision-required section, and the back-links from the rollup and boundary docs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_SAFETY_BLOCK_EMITTER_PROPOSAL.md"
ROLLUP_DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md"
BOUNDARY_DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md"

SAFETY_BLOCK_FIELDS = (
    "paper_only=true",
    "activation_authorized=false",
    "real_order_adapter_constructed=false",
    "orders=0",
    "fills=0",
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "safety-block emitter proposal doc must exist"
    return DOC.read_text(encoding="utf-8")


def test_doc_exists(doc_text: str) -> None:
    assert doc_text.strip(), "proposal doc must not be empty"


def test_not_paper_day_not_tiny_not_implementation(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "this is not paper-day" in lowered
    assert "this is not tiny-live order validation" in lowered
    assert "this is not an implementation" in lowered
    assert "this is a design proposal only" in lowered


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
        assert token in lowered, f"proposal must assert prohibition: {token!r}"


def test_problem_statement_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "problem statement" in lowered
    # The prior phases' boundaries are referenced.
    for phase in ("2a", "2b", "2c", "2d"):
        assert phase in lowered
    assert "no run-free public emitter" in lowered
    for field in SAFETY_BLOCK_FIELDS:
        assert field in doc_text, f"problem statement must name {field}"
    assert "attended paper-day diagnostic" in lowered
    assert "do not invent runtime code" in lowered


def test_design_goals_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "design goals" in lowered
    assert "run-free" in lowered
    assert "deterministic" in lowered
    assert "no network" in lowered
    assert "no kis/client dependency" in lowered or "no kis / client dependency" in lowered
    assert "no config/config.toml dependency" in lowered or "no config / config.toml dependency" in lowered
    assert "no runtime artifacts dependency" in lowered
    assert "no live adapter construction" in lowered
    assert "no broker submit path" in lowered
    assert "reusable by future no-write contract tests" in lowered
    assert "track separation" in lowered


def test_proposed_shape_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "proposed shape" in lowered
    assert "function name candidate" in lowered
    assert "input candidate" in lowered
    assert "output candidate" in lowered
    # Required fields enumerated in the shape.
    for field in (
        "paper_only",
        "activation_authorized",
        "real_order_adapter_constructed",
        "orders",
        "fills",
        "nonterminal_journal",
    ):
        assert field in doc_text, f"proposed shape must list field {field}"
    assert "validation_only" in doc_text or "evidence_scope" in doc_text
    # Explicit reminder that this is not added to runtime code.
    assert "do not add this to runtime code" in lowered


def test_future_contract_candidates_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "contract candidates for future implementation" in lowered
    for field in SAFETY_BLOCK_FIELDS:
        assert field in doc_text
    assert "no broker/client/config/runtime dependency" in lowered or (
        "no broker / client / config / runtime dependency" in lowered
    )
    assert "cannot represent activation_authorized=true" in lowered
    assert "cannot construct live adapter" in lowered
    assert "without running paper-day" in lowered


def test_non_goals_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "non-goals" in lowered
    assert "no paper-day refactor in this task" in lowered
    assert "no tiny-live design" in lowered
    assert "no live order support" in lowered
    assert "no submit_order implementation" in lowered
    assert "no adapter wiring" in lowered
    assert "no executable runbook" in lowered
    assert "no change to ops/src/config behavior" in lowered or (
        "no change to ops / src / config behavior" in lowered
    )


def test_decision_required_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "decision required before implementation" in lowered
    assert "domain model" in lowered and "composition helper" in lowered and "evidence helper" in lowered
    assert "share a safety-block shape" in lowered or "shared safety-block shape" in lowered
    assert "historical paper-day evidence semantics" in lowered
    assert "without running live diagnostics" in lowered


def test_rollup_and_boundary_link_to_proposal() -> None:
    rollup = ROLLUP_DOC.read_text(encoding="utf-8")
    boundary = BOUNDARY_DOC.read_text(encoding="utf-8")
    assert "docs/CONTROLLED_DAY1_NO_WRITE_SAFETY_BLOCK_EMITTER_PROPOSAL.md" in rollup
    assert "docs/CONTROLLED_DAY1_NO_WRITE_SAFETY_BLOCK_EMITTER_PROPOSAL.md" in boundary
