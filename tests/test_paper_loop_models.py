from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import AnalysisAction
from domain import Currency, DecisionId, Market, MarketPrice, Money, Percent
from domain.enums import AccountRole
from paper_loop import PaperLoopInput


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
SYMBOL = "005930"
PRICE = Decimal("70000")


def _market_price(**overrides: object) -> MarketPrice:
    base = {
        "symbol": SYMBOL,
        "market": Market.KR,
        "currency": Currency.KRW,
        "price": PRICE,
        "as_of": NOW,
    }
    base.update(overrides)
    return MarketPrice(**base)


def _valid_loop_input(sample_risk_input_factory, **overrides: object) -> PaperLoopInput:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("5"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("2000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    base = {
        "run_id": DecisionId("paper-loop-260522-001"),
        "created_at": NOW,
        "allocator_decision": risk_input.allocator_decision,
        "analysis_decision": risk_input.analysis_decision,
        "risk_context": risk_input.context,
        "market_price": _market_price(),
    }
    base.update(overrides)
    return PaperLoopInput(**base)


def test_paper_loop_default_account_role_remains_paper(sample_risk_input_factory) -> None:
    loop_input = _valid_loop_input(sample_risk_input_factory)
    assert loop_input.broker_account_role == AccountRole.PAPER


def test_valid_input(sample_risk_input_factory) -> None:
    loop_input = _valid_loop_input(sample_risk_input_factory)
    assert loop_input.normalized_run_id.value == "paper-loop-260522-001"
    assert loop_input.broker_account_role == AccountRole.PAPER


def test_naive_created_at_reject(sample_risk_input_factory) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _valid_loop_input(
            sample_risk_input_factory,
            created_at=datetime(2026, 5, 22, 12, 0),
        )


def test_blank_run_id_reject(sample_risk_input_factory) -> None:
    with pytest.raises(ValueError, match="run_id"):
        _valid_loop_input(sample_risk_input_factory, run_id="   ")


def test_blank_correlation_id_reject(sample_risk_input_factory) -> None:
    with pytest.raises(ValueError, match="correlation_id"):
        _valid_loop_input(sample_risk_input_factory, correlation_id="   ")


def test_metadata_invalid_reject(sample_risk_input_factory) -> None:
    with pytest.raises(ValueError, match="metadata"):
        _valid_loop_input(sample_risk_input_factory, metadata="not-a-dict")


def test_market_price_symbol_mismatch_reject(sample_risk_input_factory) -> None:
    with pytest.raises(ValueError, match="symbol"):
        _valid_loop_input(
            sample_risk_input_factory,
            market_price=_market_price(symbol="000660"),
        )


def test_market_price_market_mismatch_reject(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("5"),
        market="US",
        symbol="AAPL",
        context_overrides={
            "total_nav": Money.from_str("100000", Currency.USD),
            "cash": Money.from_str("20000", Currency.USD),
            "invested_amount": Money.from_str("80000", Currency.USD),
            "current_symbol_market_value": Money.from_str("3000", Currency.USD),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000", Currency.USD),
        },
    )
    with pytest.raises(ValueError, match="market"):
        PaperLoopInput(
            run_id=DecisionId("paper-loop-us-001"),
            created_at=NOW,
            allocator_decision=risk_input.allocator_decision,
            analysis_decision=risk_input.analysis_decision,
            risk_context=risk_input.context,
            market_price=_market_price(
                symbol="AAPL",
                market=Market.KR,
                currency=Currency.KRW,
            ),
        )


def test_market_price_currency_mismatch_reject(sample_risk_input_factory) -> None:
    with pytest.raises(ValueError, match="currency"):
        _valid_loop_input(
            sample_risk_input_factory,
            market_price=_market_price(currency=Currency.USD),
        )


def test_non_paper_account_role_reject(sample_risk_input_factory) -> None:
    with pytest.raises(ValueError, match="PAPER"):
        _valid_loop_input(
            sample_risk_input_factory,
            broker_account_role=AccountRole.KR_TAX_ADVANTAGED,
        )


def test_paper_loop_input_json_roundtrip_with_iso_datetime_strings(
    sample_risk_input_factory,
) -> None:
    """JSON file load path: ISO datetime string → PaperLoopInput.model_validate."""
    import json

    loop_input = _valid_loop_input(sample_risk_input_factory)
    payload = json.loads(loop_input.model_dump_json())
    roundtripped = PaperLoopInput.model_validate(payload)

    assert roundtripped.normalized_run_id == loop_input.normalized_run_id
    assert roundtripped.created_at == loop_input.created_at
    assert roundtripped.risk_context.created_at == loop_input.risk_context.created_at
    assert roundtripped.market_price.as_of == loop_input.market_price.as_of
