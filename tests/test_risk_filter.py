from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import AnalysisAction, AnalysisDecision, FundManagerDecision
from allocator import AllocatorDecision, AssetBucket
from domain import Currency, DecisionId, Market, MarketPrice, Money, Percent
from risk import (
    RISK_ALLOCATOR_TOLERANCE_VIOLATION,
    RISK_ASSET_CLASS_SOFT_BAND_WARNING,
    RISK_CASH_BAND_VIOLATION,
    RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED,
    RISK_GOLD_TRADE_FREQUENCY_EXCEEDED,
    RISK_INSUFFICIENT_CONTEXT,
    RISK_INVESTED_BAND_VIOLATION,
    RISK_MDD_KILLSWITCH_ACTIVE,
    RISK_NO_ACTION,
    RISK_SINGLE_POSITION_CAP_EXCEEDED,
    AssetClassWeights,
    RiskFilter,
    RiskFilterContext,
    RiskMode,
)
from domain.validation import ValidationSeverity


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
KRW = Currency.KRW
FILTER = RiskFilter()


def test_evaluate_valid_input_passed(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.HOLD,
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is True
    assert any(issue.code == RISK_NO_ACTION for issue in result.issues)


def test_buy_within_single_position_cap_allow(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("1000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is True
    assert not any(issue.code == RISK_SINGLE_POSITION_CAP_EXCEEDED for issue in result.issues)


def test_buy_exceeding_single_position_cap_block(sample_risk_input_factory) -> None:
    # NAV 100M, cap 5M cumulative; already 4.5M cumulative, target 5% = 5M -> +0.5M OK
    # target 8% = 8M, current 1M -> additional 7M -> cumulative 4.5+7=11.5M > 5M
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("8"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("1000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("4500000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(issue.code == RISK_SINGLE_POSITION_CAP_EXCEEDED for issue in result.issues)


def test_buy_missing_position_context_insufficient_context(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        context_overrides={
            "current_symbol_market_value": None,
            "current_symbol_cumulative_buy_cost": None,
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(issue.code == RISK_INSUFFICIENT_CONTEXT for issue in result.issues)


def test_sell_not_blocked_by_single_position_cap(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.SELL,
        target_weight_percent=Percent("1"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("10000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("5000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert not any(issue.code == RISK_SINGLE_POSITION_CAP_EXCEEDED for issue in result.issues)


def test_buy_causing_cash_below_min_block(sample_risk_input_factory) -> None:
    # cash 12% (12M), BUY additional 3M -> 9% < 10%
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("15"),
        context_overrides={
            "cash": Money.from_str("12000000", KRW),
            "invested_amount": Money.from_str("88000000", KRW),
            "current_symbol_market_value": Money.from_str("12000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(
        issue.code == RISK_CASH_BAND_VIOLATION and issue.severity == ValidationSeverity.ERROR
        for issue in result.issues
    )


def test_sell_causing_cash_above_max_warning(sample_risk_input_factory) -> None:
    # cash 25%, sell large portion -> cash > 30%
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.SELL,
        target_weight_percent=Percent("1"),
        context_overrides={
            "cash": Money.from_str("25000000", KRW),
            "invested_amount": Money.from_str("75000000", KRW),
            "current_symbol_market_value": Money.from_str("20000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("5000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert any(
        issue.code == RISK_CASH_BAND_VIOLATION and issue.severity == ValidationSeverity.WARNING
        for issue in result.issues
    )


def test_current_cash_inside_band_allow(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(action=AnalysisAction.HOLD)
    result = FILTER.evaluate(risk_input)
    assert not any(
        issue.code == RISK_CASH_BAND_VIOLATION and issue.severity == ValidationSeverity.ERROR
        for issue in result.issues
    )


def test_normal_invested_70_90_allow(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(action=AnalysisAction.HOLD)
    result = FILTER.evaluate(risk_input)
    assert not any(
        issue.code == RISK_INVESTED_BAND_VIOLATION and issue.severity == ValidationSeverity.ERROR
        for issue in result.issues
    )


def test_normal_invested_below_70_block(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.HOLD,
        context_overrides={
            "cash": Money.from_str("40000000", KRW),
            "invested_amount": Money.from_str("60000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(issue.code == RISK_INVESTED_BAND_VIOLATION for issue in result.issues)


def test_paper_observation_min_50_allows_55(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.HOLD,
        context_overrides={
            "cash": Money.from_str("45000000", KRW),
            "invested_amount": Money.from_str("55000000", KRW),
            "paper_observation_min_invested_percent": Percent("50"),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert not any(
        issue.code == RISK_INVESTED_BAND_VIOLATION and issue.severity == ValidationSeverity.ERROR
        for issue in result.issues
    )


def test_special_mode_lower_invested_violation_warning(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.HOLD,
        context_overrides={
            "mode": RiskMode.REBALANCING,
            "cash": Money.from_str("20000000", KRW),
            "invested_amount": Money.from_str("60000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is True
    assert any(
        issue.code == RISK_INVESTED_BAND_VIOLATION and issue.severity == ValidationSeverity.WARNING
        for issue in result.issues
    )


def test_invested_above_90_block(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.HOLD,
        context_overrides={
            "cash": Money.from_str("5000000", KRW),
            "invested_amount": Money.from_str("95000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(issue.code == RISK_INVESTED_BAND_VIOLATION for issue in result.issues)


def test_within_allocator_tolerance_allow(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("5"),
        context_overrides={
            "allocator_symbol_target_weight": Percent("5"),
            "current_symbol_market_value": Money.from_str("4000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert not any(issue.code == RISK_ALLOCATOR_TOLERANCE_VIOLATION for issue in result.issues)


def test_outside_allocator_tolerance_block(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("12"),
        context_overrides={
            "allocator_symbol_target_weight": Percent("5"),
            "current_symbol_market_value": Money.from_str("4000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(issue.code == RISK_ALLOCATOR_TOLERANCE_VIOLATION for issue in result.issues)


def test_no_allocator_target_context_no_tolerance_issue(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(action=AnalysisAction.HOLD)
    result = FILTER.evaluate(risk_input)
    assert not any(issue.code == RISK_ALLOCATOR_TOLERANCE_VIOLATION for issue in result.issues)


def test_asset_class_soft_band_warning_only(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.HOLD,
        context_overrides={
            "current_asset_weights": AssetClassWeights(
                kr=Percent("60"),
                us=Percent("10"),
                gold=Percent("30"),
            ),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is True
    assert any(issue.code == RISK_ASSET_CLASS_SOFT_BAND_WARNING for issue in result.issues)


def test_missing_current_asset_weights_no_soft_band_issue(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(action=AnalysisAction.HOLD)
    result = FILTER.evaluate(risk_input)
    assert not any(issue.code == RISK_ASSET_CLASS_SOFT_BAND_WARNING for issue in result.issues)


def test_mdd_killswitch_blocks_buy(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        context_overrides={
            "mode": RiskMode.MDD_KILLSWITCH,
            "current_symbol_market_value": Money.from_str("1000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(
        issue.code == RISK_MDD_KILLSWITCH_ACTIVE and issue.severity == ValidationSeverity.ERROR
        for issue in result.issues
    )


def test_mdd_killswitch_allows_sell(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.SELL,
        target_weight_percent=Percent("2"),
        context_overrides={
            "mode": RiskMode.MDD_KILLSWITCH,
            "current_symbol_market_value": Money.from_str("5000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("3000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert not any(
        issue.code == RISK_MDD_KILLSWITCH_ACTIVE and issue.severity == ValidationSeverity.ERROR
        for issue in result.issues
    )


def test_mdd_percent_produces_issue(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.HOLD,
        context_overrides={"mdd_percent": Percent("12")},
    )
    result = FILTER.evaluate(risk_input)
    assert any(issue.code == RISK_MDD_KILLSWITCH_ACTIVE for issue in result.issues)


def test_mdd_percent_level3_strongest_issue(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.HOLD,
        context_overrides={"mdd_percent": Percent("22")},
    )
    result = FILTER.evaluate(risk_input)
    mdd_issues = [i for i in result.issues if i.code == RISK_MDD_KILLSWITCH_ACTIVE]
    assert len(mdd_issues) == 1
    assert mdd_issues[0].severity == ValidationSeverity.ERROR
    assert "Level 3" in mdd_issues[0].message


def test_kr_buy_within_slippage_allow(sample_risk_input_factory) -> None:
    ref_price = Decimal("70000")
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        symbol="005930",
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
            "proposed_price": Money.from_str("70300", KRW),
            "reference_prices": {
                "005930": MarketPrice(
                    symbol="005930",
                    market=Market.KR,
                    currency=KRW,
                    price=ref_price,
                    as_of=NOW,
                ),
            },
        },
    )
    result = FILTER.evaluate(risk_input)
    assert not any(issue.code == RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED for issue in result.issues)


def test_kr_buy_above_slippage_block(sample_risk_input_factory) -> None:
    ref_price = Decimal("70000")
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        symbol="005930",
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
            "proposed_price": Money.from_str("71000", KRW),
            "reference_prices": {
                "005930": MarketPrice(
                    symbol="005930",
                    market=Market.KR,
                    currency=KRW,
                    price=ref_price,
                    as_of=NOW,
                ),
            },
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(issue.code == RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED for issue in result.issues)


def test_us_sell_rebound_slippage_block(sample_risk_input_factory) -> None:
    ref_price = Decimal("100")
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.SELL,
        symbol="AAPL",
        market="US",
        target_weight_percent=Percent("2"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("5000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("3000000", KRW),
            "proposed_price": Money.from_str("101", KRW),
            "reference_prices": {
                "AAPL": MarketPrice(
                    symbol="AAPL",
                    market=Market.US,
                    currency=Currency.USD,
                    price=ref_price,
                    as_of=NOW,
                ),
            },
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(issue.code == RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED for issue in result.issues)


def test_missing_price_context_no_slippage_issue(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert not any(issue.code == RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED for issue in result.issues)


def test_non_gold_unaffected_by_gold_frequency(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        context_overrides={
            "gold_trades_this_month": 2,
            "current_symbol_market_value": Money.from_str("3000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert not any(issue.code == RISK_GOLD_TRADE_FREQUENCY_EXCEEDED for issue in result.issues)


def test_gold_trade_month_count_block(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        context_overrides={
            "asset_bucket": AssetBucket.GOLD,
            "gold_trades_this_month": 2,
            "current_symbol_market_value": Money.from_str("3000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(issue.code == RISK_GOLD_TRADE_FREQUENCY_EXCEEDED for issue in result.issues)


def test_gold_trade_quarter_count_block(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.SELL,
        target_weight_percent=Percent("15"),
        context_overrides={
            "asset_bucket": AssetBucket.GOLD,
            "gold_trades_this_quarter": 4,
            "current_symbol_market_value": Money.from_str("20000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("5000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert result.passed is False
    assert any(issue.code == RISK_GOLD_TRADE_FREQUENCY_EXCEEDED for issue in result.issues)


def test_gold_trade_below_limits_allow(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        context_overrides={
            "asset_bucket": AssetBucket.GOLD,
            "gold_trades_this_month": 1,
            "gold_trades_this_quarter": 3,
            "current_symbol_market_value": Money.from_str("3000000", KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", KRW),
        },
    )
    result = FILTER.evaluate(risk_input)
    assert not any(issue.code == RISK_GOLD_TRADE_FREQUENCY_EXCEEDED for issue in result.issues)
