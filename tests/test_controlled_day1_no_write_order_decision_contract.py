"""Controlled Day 1 — No-Write Order-Decision CONTRACT tests.

Offline/synthetic proof that the strategy / risk / order-decision flow can produce
and carry a *hypothetical* order intent while every live broker write path remains
impossible and no live adapter is ever constructed in an execution path.

These tests are paired with the static inventory in
`docs/CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md`. They use fake/synthetic
inputs only: no live KIS, no network, no real credentials, no secret values, no
activation, no daemon, no submit against a live venue.

No-invention rule: these tests only exercise existing public APIs. The
evidence/safety-invariant assertion (paper_only/activation_authorized/
real_order_adapter_constructed/orders/fills) has no run-free public emitter, so it is
left as a documented future contract gap (see `test_evidence_safety_invariant_is_a_documented_future_gap`).
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from analysis import AnalysisAction
from config.settings import ExecutionMode
from domain import Currency, DecisionId, Market, MarketPrice, Money, Percent
from domain.enums import AccountRole, AssetClass, OrderSide, OrderType
from domain.order import OrderIntent
from domain.validation import ValidationResult
from paper_loop import PaperLoopInput
from paper_loop.models import (
    PAPER_LOOP_SCHEMA,
    PAPER_LOOP_VALIDATOR_VERSION,
    PaperLoopResult,
    PaperLoopStatus,
    passed_validation_result,
)
from risk.models import OrderGenerationStatus
from risk.order_generation import OrderGenerationResult, OrderIntentGenerator
from broker.kis_live_adapter import KisLiveOrderBlockedError, KisLiveReadOnlyBrokerAdapter

NOW = datetime(2026, 6, 22, 1, 0, tzinfo=UTC)
SYMBOL = "005930"


def _hypothetical_order_intent() -> OrderIntent:
    """A synthetic OrderIntent built directly — no broker, no submit."""
    return OrderIntent(
        order_id="order-no-write-001",
        correlation_id="corr-no-write-001",
        symbol=SYMBOL,
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        target_weight_percent=Decimal("5"),
        limit_price=None,
        reason_code="synthetic no-write contract",
        source_decision_id="decision-no-write-001",
        created_at=NOW,
    )


# --- Contract 1 — hypothetical intent without any broker -------------------


def test_order_intent_constructible_as_hypothetical_without_broker() -> None:
    intent = _hypothetical_order_intent()
    assert intent.order_id == "order-no-write-001"
    assert intent.side is OrderSide.BUY
    # A hypothetical intent carries a decision; it places nothing.
    assert intent.quantity is None
    assert intent.target_weight_percent == Decimal("5")


def test_generator_reaches_intent_without_constructing_a_broker(
    sample_risk_input_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OrderIntentGenerator.generate reaches an OrderIntent and never builds a broker.

    We trip every known broker constructor: if generation touched one, the test
    would raise instead of producing GENERATED.
    """
    import broker.kis_live_adapter as kis_live_mod
    import broker.paper_broker as paper_broker_mod

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no broker may be constructed in the order-decision path")

    monkeypatch.setattr(kis_live_mod.KisLiveReadOnlyBrokerAdapter, "__init__", _explode)
    monkeypatch.setattr(paper_broker_mod.PaperBrokerAdapter, "__init__", _explode)

    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("5"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("2000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    result = OrderIntentGenerator().generate(risk_input)

    assert result.status is OrderGenerationStatus.GENERATED
    assert result.order_intent is not None
    assert result.order_intent.symbol == SYMBOL
    # No submit and no broker object exist anywhere in this path.
    assert not hasattr(result, "broker")


# --- Contract 2 — live read-only adapter cannot submit ----------------------


class _UnusableClient:
    """A stand-in KIS client that raises if anything touches it.

    submit_order must reject before using the client, so this proves the block is
    structural — not dependent on any live/network call. No real credentials, no
    secret values, no network.
    """

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"live client must not be used (touched {name!r})")


def test_live_read_only_adapter_submit_order_is_blocked() -> None:
    adapter = KisLiveReadOnlyBrokerAdapter(_UnusableClient())
    intent = _hypothetical_order_intent()
    market_price = MarketPrice(
        symbol=SYMBOL,
        market=Market.KR,
        currency=Currency.KRW,
        price=Decimal("70000"),
        as_of=NOW,
    )
    with pytest.raises(KisLiveOrderBlockedError):
        adapter.submit_order(intent, market_price)


# --- Contract 3 — no src/ execution path constructs the live adapter --------


def test_no_src_path_constructs_kis_live_read_only_adapter() -> None:
    """Static guard: no checked-in src/ file constructs KisLiveReadOnlyBrokerAdapter.

    Imports and the class definition are allowed; a construction *call*
    `KisLiveReadOnlyBrokerAdapter(` is not. Scans src/ only — never tests, never
    config/config.toml, never runtime artifacts.
    """
    construction = re.compile(r"\bKisLiveReadOnlyBrokerAdapter\s*\(")
    offenders: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("class KisLiveReadOnlyBrokerAdapter"):
                continue
            if construction.search(line):
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "live adapter constructed in src/ execution path:\n" + "\n".join(offenders)


# --- Contract 4 — paper-role enforcement; result carries intent, no broker --


def test_paper_loop_input_rejects_non_paper_account_role(
    sample_risk_input_factory,
) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("5"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("2000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    market_price = MarketPrice(
        symbol=SYMBOL,
        market=Market.KR,
        currency=Currency.KRW,
        price=Decimal("70000"),
        as_of=NOW,
    )
    with pytest.raises(ValueError, match="PAPER"):
        PaperLoopInput(
            run_id=DecisionId("paper-loop-no-write-001"),
            created_at=NOW,
            allocator_decision=risk_input.allocator_decision,
            analysis_decision=risk_input.analysis_decision,
            risk_context=risk_input.context,
            market_price=market_price,
            broker_account_role=AccountRole.KR_TAX_ADVANTAGED,
        )


def test_paper_loop_result_carries_intent_without_broker_result() -> None:
    """A result can hold a generated intent while broker_order_result stays None."""
    intent = _hypothetical_order_intent()
    passed: ValidationResult = passed_validation_result(
        schema_name=PAPER_LOOP_SCHEMA,
        validator_version=PAPER_LOOP_VALIDATOR_VERSION,
    )
    order_generation_result = OrderGenerationResult(
        status=OrderGenerationStatus.GENERATED,
        order_intent=intent,
        validation_result=passed,
        correlation_id=intent.correlation_id,
    )
    result = PaperLoopResult(
        status=PaperLoopStatus.NOOP,
        validation_result=passed,
        risk_result=passed,
        order_generation_result=order_generation_result,
        generated_order_intent=intent,
    )
    assert result.generated_order_intent is intent
    assert result.broker_order_result is None
    assert result.fill is None


# --- Contract 5 — evidence/safety invariant is a documented future gap ------


def test_evidence_safety_invariant_is_a_documented_future_gap() -> None:
    """No run-free public API emits the safety block, so the boundary doc records it
    as a future contract gap rather than inventing one (no-invention rule)."""
    doc = (REPO_ROOT / "docs" / "CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md").read_text(
        encoding="utf-8"
    )
    lowered = doc.lower()
    assert "future contract gap" in lowered
    assert "no standalone public function or model" in lowered
    assert "real_order_adapter_constructed" in lowered
    assert "is_clean_pass" in doc
