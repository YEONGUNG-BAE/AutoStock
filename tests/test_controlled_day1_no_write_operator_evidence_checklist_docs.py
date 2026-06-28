"""Guard the Controlled Day 1 no-write Operator evidence checklist doc.

`docs/CONTROLLED_DAY1_NO_WRITE_OPERATOR_EVIDENCE_CHECKLIST.md` is an operator-facing
checklist for the *future* Controlled Day 1 no-write order-decision rehearsal: it tells
the Operator which offline/synthetic evidence to collect and when to stop, WITHOUT
authorizing any live/tiny/write command. These guards lock its no-Paper-Day / no-tiny
framing, its prerequisites, its allowed and forbidden evidence lists, its abort
criteria, and its stop boundary (do-not-proceed-to-tiny-live gate), plus the back-link
from the boundary inventory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_OPERATOR_EVIDENCE_CHECKLIST.md"
BOUNDARY_DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "operator evidence checklist doc must exist"
    return DOC.read_text(encoding="utf-8")


def test_doc_exists(doc_text: str) -> None:
    assert doc_text.strip(), "checklist doc must not be empty"


def test_not_paper_day_and_not_tiny_live(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "this is not paper-day" in lowered
    assert "this is not tiny-live order validation" in lowered
    assert "controlled day 1 no-write order-decision readiness" in lowered
    # Operator-facing but no live/tiny/write command authorized.
    assert "operator-facing" in lowered
    assert "no live / tiny / write command is authorized" in lowered or (
        "no live/tiny/write command is authorized" in lowered
    )
    # Does not replace the boundary inventory or contract tests.
    assert "does not replace the boundary inventory or contract tests" in lowered


def test_links_inventory_and_contract_tests(doc_text: str) -> None:
    assert "docs/CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md" in doc_text
    assert "tests/test_controlled_day1_no_write_order_decision_contract.py" in doc_text


def test_current_prerequisites_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "current prerequisites" in lowered
    assert "docs/CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md" in doc_text
    assert "tests/test_controlled_day1_no_write_order_decision_contract.py" in doc_text
    assert "full acceptance should be green" in lowered


def test_allowed_evidence_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "allowed evidence" in lowered
    assert "offline" in lowered
    assert "synthetic" in lowered
    assert "hypothetical order intent" in lowered
    assert "broker_order_result is absent" in lowered or "broker_order_result absent" in lowered
    assert "no KisLiveReadOnlyBrokerAdapter construction" in doc_text or (
        "no `KisLiveReadOnlyBrokerAdapter` construction" in doc_text
    )
    assert "KisLiveOrderBlockedError" in doc_text
    assert "non-PAPER" in doc_text
    assert "future" in lowered and "gap" in lowered
    assert "run-free" in lowered


def test_forbidden_evidence_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "forbidden evidence" in lowered
    assert "no secrets" in lowered
    assert "config/config.toml" in doc_text
    assert "no runtime artifacts" in lowered
    assert "no raw frames" in lowered
    assert "no live kis" in lowered
    assert "no broker" in lowered and "endpoint" in lowered
    assert "no tiny-live order output" in lowered
    # Token/secret families enumerated as prohibitions only.
    for token in ("url", "token", "app key", "approval key", "account", "traceback"):
        assert token in lowered, f"forbidden list must name {token!r}"


def test_abort_criteria_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "abort" in lowered
    assert "live" in lowered and "network" in lowered
    assert "tiny-live" in lowered
    assert "submit_order" in lowered
    assert "live adapter wiring" in lowered
    assert "activation_authorized" in lowered
    assert "kisLiveReadOnlyBrokerAdapter constructed".lower() in lowered or (
        "construct" in lowered and "kisLiveReadOnlyBrokerAdapter".lower() in lowered
    )
    assert "non-paper" in lowered
    assert "broker_order_result" in lowered or "fill" in lowered
    assert "acceptance fails" in lowered


def test_stop_boundary_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "stop boundary" in lowered
    assert "documentation/evidence checklist only" in lowered or (
        "documentation / evidence checklist only" in lowered
    )
    assert "do not proceed to tiny-live until" in lowered or (
        "do not proceed to tiny live until" in lowered
    )
    assert "boundary inventory" in lowered and "reviewed" in lowered
    assert "contract tests" in lowered and "green" in lowered
    assert "operator checklist" in lowered and "reviewed" in lowered
    assert "safety-block emitter" in lowered
    assert "explicit human approval" in lowered


def test_boundary_inventory_links_to_checklist() -> None:
    boundary = BOUNDARY_DOC.read_text(encoding="utf-8")
    assert "docs/CONTROLLED_DAY1_NO_WRITE_OPERATOR_EVIDENCE_CHECKLIST.md" in boundary
