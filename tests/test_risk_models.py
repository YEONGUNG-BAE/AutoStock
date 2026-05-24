from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from risk import (
    AssetClassWeights,
    OrderGenerationStatus,
    RiskDecision,
    RiskFilterContext,
    RiskFilterInput,
    RiskMode,
)
from domain import Currency, Money, Percent


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
KRW = Currency.KRW


def _nav(amount: str = "100000000") -> Money:
    return Money.from_str(amount, KRW)


def _sample_context(**overrides: object) -> RiskFilterContext:
    nav = _nav()
    base = {
        "created_at": NOW,
        "mode": RiskMode.NORMAL,
        "total_nav": nav,
        "cash": Money.from_str("20000000", KRW),
        "invested_amount": Money.from_str("80000000", KRW),
    }
    base.update(overrides)
    return RiskFilterContext(**base)


def test_risk_mode_enum_parsing() -> None:
    assert RiskMode.NORMAL == "normal"
    assert RiskMode("rebalancing") == RiskMode.REBALANCING


def test_risk_mode_invalid_reject() -> None:
    with pytest.raises(ValueError):
        RiskMode("invalid")


def test_risk_decision_enum_parsing() -> None:
    assert RiskDecision.ALLOW == "allow"
    assert RiskDecision("block") == RiskDecision.BLOCK


def test_order_generation_status_enum_parsing() -> None:
    assert OrderGenerationStatus.GENERATED == "generated"
    assert OrderGenerationStatus("noop") == OrderGenerationStatus.NOOP


def test_valid_risk_filter_context() -> None:
    context = _sample_context()
    assert context.total_nav.amount == Decimal("100000000")


def test_naive_created_at_reject() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _sample_context(created_at=datetime(2026, 5, 22, 12, 0))


def test_total_nav_zero_reject() -> None:
    with pytest.raises(ValueError, match="total_nav"):
        _sample_context(total_nav=Money.from_str("0", KRW))


def test_negative_cash_reject() -> None:
    with pytest.raises(ValueError, match="cash"):
        _sample_context(cash=Money.from_str("-1", KRW))


def test_negative_invested_reject() -> None:
    with pytest.raises(ValueError, match="invested_amount"):
        _sample_context(invested_amount=Money.from_str("-1", KRW))


def test_negative_gold_trade_count_reject() -> None:
    with pytest.raises(ValueError, match="gold_trades_this_month"):
        _sample_context(gold_trades_this_month=-1)


def test_invalid_paper_observation_min_reject() -> None:
    with pytest.raises(ValueError, match="paper_observation_min"):
        _sample_context(paper_observation_min_invested_percent=Percent("45"))


def test_valid_paper_observation_min() -> None:
    context = _sample_context(paper_observation_min_invested_percent=Percent("55"))
    assert context.paper_observation_min_invested_percent == Percent("55")


def test_metadata_invalid_reject() -> None:
    with pytest.raises(ValueError, match="metadata"):
        _sample_context(metadata={"key": {1, 2}})


def test_currency_mismatch_reject() -> None:
    with pytest.raises(ValueError, match="currency"):
        _sample_context(cash=Money.from_str("20000000", Currency.USD))


def test_proposed_price_quote_currency_may_differ_from_nav() -> None:
    context = _sample_context(
        proposed_price=Money.from_str("101", Currency.USD),
    )
    assert context.proposed_price is not None
    assert context.proposed_price.currency == Currency.USD
    assert context.total_nav.currency == KRW


def test_asset_class_weights_extra_field_reject() -> None:
    with pytest.raises(ValidationError):
        AssetClassWeights(
            kr=Percent("50"),
            us=Percent("30"),
            gold=Percent("20"),
            extra="x",
        )


def test_risk_filter_input_valid(
    sample_risk_input_factory,
) -> None:
    risk_input = sample_risk_input_factory()
    assert risk_input.correlation_id is None


def test_risk_filter_input_blank_correlation_id_reject(
    sample_risk_input_factory,
) -> None:
    with pytest.raises(ValueError, match="correlation_id"):
        sample_risk_input_factory(correlation_id="   ")


def test_risk_filter_input_extra_field_reject(
    sample_risk_input_factory,
) -> None:
    base = sample_risk_input_factory()
    with pytest.raises(ValidationError):
        RiskFilterInput(
            allocator_decision=base.allocator_decision,
            analysis_decision=base.analysis_decision,
            context=base.context,
            extra_field="x",
        )
