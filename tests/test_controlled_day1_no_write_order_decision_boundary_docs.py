"""Guard the Controlled Day 1 no-write order-decision boundary inventory doc.

`docs/CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md` is a static boundary
inventory that maps the repo's strategy / risk / order-decision / write-boundary
surfaces before any no-write contract tests are written. These guards lock its
load-bearing properties so a future edit cannot (a) blur it into Paper-Day or
tiny-live validation, (b) drop an inventory category, (c) weaken the no-write
contract candidate invariants, or (d) loosen the prohibitions against live KIS /
network / live orders / activation / live adapter wiring / submit_order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "boundary inventory doc must exist"
    return DOC.read_text(encoding="utf-8")


def test_doc_exists(doc_text: str) -> None:
    assert doc_text.strip(), "boundary inventory doc must not be empty"


def test_not_paper_day_and_not_tiny_live(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "this is not paper-day" in lowered
    assert "this is not tiny live order validation" in lowered
    # And it must state what it IS: a static boundary inventory for no-write readiness.
    assert "static boundary inventory" in lowered
    assert "no-write order-decision readiness" in lowered


def test_static_inventory_not_implementation(doc_text: str) -> None:
    # The doc must frame itself as a static inventory, not an implementation task.
    lowered = doc_text.lower()
    assert "inventory task, not an implementation task" in lowered
    assert "static inventory, not implementation" in lowered
    # And it must promise not to change runtime code.
    assert "changes no runtime code" in lowered or "no runtime / ops / src / config behavior change" in lowered


# Section headers and the exact Category-cell label each category uses.
INVENTORY_CATEGORIES = (
    "strategy / decision path",
    "risk gate / risk checks",
    "order intent model / decision artifact",
    "paper loop input / runner / execution boundary",
    "broker adapter abstraction",
    "live adapter construction point",
    "activation / paper / live flags",
    "evidence fields",
    "kill switch / abort / stop-reason surfaces",
)


def test_major_inventory_categories_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    for category in INVENTORY_CATEGORIES:
        assert category in lowered, f"inventory must cover category: {category}"


def test_each_category_mapped_or_not_found(doc_text: str) -> None:
    # Every requested category section must either cite a concrete checked-in
    # repo path (src/...) or explicitly mark the missing reference Not found.
    sections = doc_text.split("\n### ")
    for category in INVENTORY_CATEGORIES:
        section = next(
            (s for s in sections if category in s.split("\n", 1)[0].lower()),
            None,
        )
        assert section is not None, f"missing inventory section for category: {category}"
        assert ("src/" in section) or ("not found" in section.lower()), (
            f"category {category!r} must map to a src/ path or be marked Not found"
        )


def test_inventory_tables_reference_real_symbols(doc_text: str) -> None:
    # The inventory must cite actual repo files/symbols, not just prose.
    for ref in (
        "src/broker/protocols.py",
        "BrokerAdapter",
        "src/broker/paper_broker.py",
        "PaperBrokerAdapter",
        "src/broker/kis_live_adapter.py",
        "KisLiveReadOnlyBrokerAdapter",
        "src/risk/order_generation.py",
        "OrderIntentGenerator",
        "src/domain/order.py",
        "OrderIntent",
        "src/paper_loop/runner.py",
        "PaperLoopRunner",
        "src/composition/attended_paper_day.py",
    ):
        assert ref in doc_text, f"inventory must reference {ref}"


def test_table_columns_present(doc_text: str) -> None:
    # Each inventory table must carry the required columns, Category first.
    assert (
        "| Category | File | Symbol / function / class | Current behavior | "
        "Safety implication | Gap / next contract-test need |"
    ) in doc_text


def test_no_write_contract_candidates_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "no-write contract candidates" in lowered
    # Each candidate invariant from the task must appear.
    assert "hypothetical order intent" in lowered
    assert "no live adapter is constructed" in lowered
    assert "no submit is called" in lowered
    assert "no kis / broker write path" in lowered or "no kis/broker write path" in lowered
    assert "activation_authorized remains false" in lowered
    assert "paper_only remains true" in lowered
    assert "real_order_adapter_constructed remains false" in lowered
    assert "orders remains zero and fills remains zero" in lowered
    assert "evidence records the no-write" in lowered


def test_do_not_proceed_to_tiny_live_section(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "do not proceed to tiny live until" in lowered
    assert "boundary inventory is reviewed" in lowered
    assert "no-write contract tests" in lowered
    assert "operator evidence checklist" in lowered
    assert "acceptance path" in lowered and "green" in lowered


def test_prohibitions_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "no live kis" in lowered
    assert "no network" in lowered
    assert "no live orders" in lowered
    assert "no activation" in lowered
    assert "no daemon" in lowered
    assert "no automatic restart" in lowered
    assert "no live adapter wiring" in lowered
    assert "no submit_order" in lowered
    assert "no tiny-live order runbook" in lowered
    # No raw-frame/secret leakage prohibition.
    assert "traceback" in lowered


def test_live_adapter_is_blocked_and_unconstructed(doc_text: str) -> None:
    # The inventory must record that the live adapter cannot submit and that no
    # code constructs it in an execution path.
    assert "KisLiveOrderBlockedError" in doc_text
    lowered = doc_text.lower()
    assert "does not submit orders" in lowered or "cannot submit" in lowered
    # The live-adapter construction point must be explicitly marked Not found in src/.
    assert "not found" in lowered
    assert "no `src/` construction site" in doc_text or "not found in any `src/` execution path" in lowered
