from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import AnalysisAction
from domain import (
    AccountRole,
    AssetClass,
    Currency,
    Market,
    Money,
    OrderIntent,
    OrderSide,
    OrderType,
    Percent,
)
from config.settings import ExecutionMode
from allocator import AssetBucket
from risk import (
    AssetClassWeights,
    OrderGenerationStatus,
    OrderIntentGenerator,
    RISK_ORDER_GENERATION_FAILED,
    RiskMode,
)


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
GENERATOR = OrderIntentGenerator()


def test_order_generator_default_account_role_is_paper(sample_risk_input_factory) -> None:
    result = GENERATOR.generate(
        sample_risk_input_factory(
            action=AnalysisAction.BUY,
            target_weight_percent=Percent("4"),
            context_overrides={
                "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
                "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
            },
        )
    )
    assert result.status == OrderGenerationStatus.GENERATED
    assert result.order_intent is not None
    assert result.order_intent.account_role == AccountRole.PAPER


def test_buy_generates_order_intent_buy_market_target_weight(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    result = GENERATOR.generate(risk_input)

    assert result.status == OrderGenerationStatus.GENERATED
    assert result.order_intent is not None
    assert result.order_intent.account_role == AccountRole.PAPER
    assert result.order_intent.side == OrderSide.BUY
    assert result.order_intent.order_type == OrderType.MARKET
    assert result.order_intent.quantity is None
    assert result.order_intent.target_weight_percent == Decimal("4")
    assert result.order_intent.limit_price is None


def test_sell_generates_order_intent_sell_market_target_weight(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.SELL,
        target_weight_percent=Percent("2"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("5000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("3000000", Currency.KRW),
        },
    )
    result = GENERATOR.generate(risk_input)

    assert result.status == OrderGenerationStatus.GENERATED
    assert result.order_intent is not None
    assert result.order_intent.side == OrderSide.SELL
    assert result.order_intent.target_weight_percent == Decimal("2")


def test_hold_generates_noop(sample_risk_input_factory) -> None:
    result = GENERATOR.generate(sample_risk_input_factory(action=AnalysisAction.HOLD))

    assert result.status == OrderGenerationStatus.NOOP
    assert result.order_intent is None


def test_unsupported_market_string_blocked(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        market="GLOBAL",
        target_weight_percent=Percent("3"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    result = GENERATOR.generate(risk_input)

    assert result.status == OrderGenerationStatus.BLOCKED
    assert result.order_intent is None
    assert any(issue.code == RISK_ORDER_GENERATION_FAILED for issue in result.validation_result.issues)


def test_risk_error_blocks_generation(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("12"),
        context_overrides={
            "allocator_symbol_target_weight": Percent("5"),
            "current_symbol_market_value": Money.from_str("4000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    result = GENERATOR.generate(risk_input)

    assert result.status == OrderGenerationStatus.BLOCKED
    assert result.order_intent is None


def test_warning_only_still_generates(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
            "current_asset_weights": AssetClassWeights(
                kr=Percent("60"),
                us=Percent("10"),
                gold=Percent("30"),
            ),
        },
    )
    result = GENERATOR.generate(risk_input)

    assert result.status == OrderGenerationStatus.GENERATED
    assert result.order_intent is not None


def test_deterministic_same_input_same_order_id(sample_risk_input_factory) -> None:
    kwargs = dict(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        correlation_id="corr-deterministic",
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    first = GENERATOR.generate(sample_risk_input_factory(**kwargs))
    second = GENERATOR.generate(sample_risk_input_factory(**kwargs))

    assert first.order_intent is not None
    assert second.order_intent is not None
    assert first.order_intent.order_id == second.order_intent.order_id
    assert first.order_intent.model_dump() == second.order_intent.model_dump()


def test_generated_order_intent_passes_domain_validation(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    result = GENERATOR.generate(risk_input)
    assert result.order_intent is not None
    # 재파싱으로 domain validation 통과 확인
    revalidated = OrderIntent.model_validate(result.order_intent.model_dump())
    assert revalidated.order_id == result.order_intent.order_id


def test_us_market_order_intent_fields(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        symbol="AAPL",
        market="US",
        target_weight_percent=Percent("3"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    result = GENERATOR.generate(risk_input)

    assert result.order_intent is not None
    assert result.order_intent.market == Market.US
    assert result.order_intent.asset_class == AssetClass.US_EQUITY


def test_gold_asset_bucket_routing(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("18"),
        context_overrides={
            "asset_bucket": AssetBucket.GOLD,
            "gold_trades_this_month": 0,
            "gold_trades_this_quarter": 0,
            "current_symbol_market_value": Money.from_str("15000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    result = GENERATOR.generate(risk_input)

    assert result.order_intent is not None
    assert result.order_intent.asset_class == AssetClass.GOLD


def test_execution_mode_from_risk_mode(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        context_overrides={
            "mode": RiskMode.REBALANCING,
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    result = GENERATOR.generate(risk_input)

    assert result.order_intent is not None
    assert result.order_intent.execution_mode == ExecutionMode.REBALANCING


def test_order_id_format(sample_risk_input_factory) -> None:
    risk_input = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        context_overrides={
            "current_symbol_market_value": Money.from_str("3000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    result = GENERATOR.generate(risk_input)

    assert result.order_intent is not None
    assert result.order_intent.order_id == "order-analysis-260522-001"
    assert result.order_intent.source_decision_id == "analysis-260522-001"
