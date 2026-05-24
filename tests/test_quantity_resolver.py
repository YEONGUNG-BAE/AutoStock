from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import AnalysisAction
from config.settings import ExecutionMode
from domain import (
    AccountRole,
    AssetClass,
    Currency,
    Market,
    MarketPrice,
    Money,
    OrderIntent,
    OrderSide,
    OrderType,
    Percent,
)
from paper_loop import (
    PAPER_LOOP_INVALID_TARGET_WEIGHT_INTENT,
    PAPER_LOOP_NO_EXECUTABLE_QUANTITY,
    PAPER_LOOP_QUANTITY_CURRENCY_MISMATCH,
    PAPER_LOOP_QUANTITY_RESOLVED,
    PAPER_LOOP_UNSUPPORTED_ORDER_TYPE,
    QuantityResolutionStatus,
    QuantityResolver,
)
from risk import RiskFilterContext, RiskMode


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
RESOLVER = QuantityResolver()
NAV = Decimal("100000000")
PRICE = Decimal("70000")


def _context(**overrides: object) -> RiskFilterContext:
    nav = Money.from_str(str(NAV), Currency.KRW)
    base = {
        "created_at": NOW,
        "mode": RiskMode.NORMAL,
        "total_nav": nav,
        "cash": Money.from_str("20000000", Currency.KRW),
        "invested_amount": Money.from_str("80000000", Currency.KRW),
        "current_symbol_market_value": Money.from_str("2000000", Currency.KRW),
    }
    base.update(overrides)
    return RiskFilterContext(**base)


def _market_price() -> MarketPrice:
    return MarketPrice(
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        price=PRICE,
        as_of=NOW,
    )


def _target_weight_intent(*, side: OrderSide, target: str) -> OrderIntent:
    return OrderIntent(
        order_id="order-analysis-260522-001",
        correlation_id="corr-1",
        symbol="005930",
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=side,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        target_weight_percent=Decimal(target),
        created_at=NOW,
    )


def test_buy_resolves_quantity() -> None:
    result = RESOLVER.resolve(
        intent=_target_weight_intent(side=OrderSide.BUY, target="5"),
        context=_context(),
        market_price=_market_price(),
    )
    # target 5M - current 2M = 3M / 70k = 42.857 -> 42
    assert result.status == QuantityResolutionStatus.RESOLVED
    assert result.order_intent is not None
    assert result.order_intent.quantity == Decimal("42")
    assert result.order_intent.target_weight_percent is None
    assert any(i.code == PAPER_LOOP_QUANTITY_RESOLVED for i in result.validation_result.issues)


def test_sell_resolves_quantity() -> None:
    result = RESOLVER.resolve(
        intent=_target_weight_intent(side=OrderSide.SELL, target="2"),
        context=_context(current_symbol_market_value=Money.from_str("5000000", Currency.KRW)),
        market_price=_market_price(),
    )
    # current 5M - target 2M = 3M / 70k = 42
    assert result.status == QuantityResolutionStatus.RESOLVED
    assert result.order_intent is not None
    assert result.order_intent.quantity == Decimal("42")


def test_target_equals_current_no_executable_quantity() -> None:
    result = RESOLVER.resolve(
        intent=_target_weight_intent(side=OrderSide.BUY, target="2"),
        context=_context(),
        market_price=_market_price(),
    )
    assert result.status == QuantityResolutionStatus.NOOP
    assert result.order_intent is None
    assert any(i.code == PAPER_LOOP_NO_EXECUTABLE_QUANTITY for i in result.validation_result.issues)


def test_quantity_floor_toward_zero() -> None:
    result = RESOLVER.resolve(
        intent=_target_weight_intent(side=OrderSide.BUY, target="5"),
        context=_context(
            current_symbol_market_value=Money.from_str("2999999", Currency.KRW),
        ),
        market_price=_market_price(),
    )
    assert result.status == QuantityResolutionStatus.RESOLVED
    assert result.order_intent is not None
    # delta slightly above 2M/70k -> floor 28 not 29
    expected = (Decimal("5000000") - Decimal("2999999")) / PRICE
    assert result.order_intent.quantity == expected.to_integral_value(rounding=ROUND_DOWN)


def test_currency_mismatch_fails() -> None:
    result = RESOLVER.resolve(
        intent=_target_weight_intent(side=OrderSide.BUY, target="5"),
        context=_context(),
        market_price=MarketPrice(
            symbol="AAPL",
            market=Market.US,
            currency=Currency.USD,
            price=Decimal("150"),
            as_of=NOW,
        ),
    )
    assert result.status == QuantityResolutionStatus.FAILED
    assert any(i.code == PAPER_LOOP_QUANTITY_CURRENCY_MISMATCH for i in result.validation_result.issues)


def test_input_quantity_already_set_reject() -> None:
    intent = OrderIntent(
        order_id="order-qty",
        correlation_id="corr",
        symbol="005930",
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        quantity=Decimal("10"),
        created_at=NOW,
    )
    result = RESOLVER.resolve(
        intent=intent,
        context=_context(),
        market_price=_market_price(),
    )
    assert result.status == QuantityResolutionStatus.FAILED
    assert any(i.code == PAPER_LOOP_INVALID_TARGET_WEIGHT_INTENT for i in result.validation_result.issues)


def test_input_target_weight_missing_reject() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        OrderIntent(
            order_id="order-no-target",
            correlation_id="corr",
            symbol="005930",
            market=Market.KR,
            asset_class=AssetClass.KR_EQUITY,
            account_role=AccountRole.PAPER,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            execution_mode=ExecutionMode.NORMAL,
            created_at=NOW,
        )


def test_limit_intent_reject() -> None:
    intent = OrderIntent(
        order_id="order-limit",
        correlation_id="corr",
        symbol="005930",
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        execution_mode=ExecutionMode.NORMAL,
        target_weight_percent=Decimal("5"),
        limit_price=Decimal("69000"),
        created_at=NOW,
    )
    result = RESOLVER.resolve(
        intent=intent,
        context=_context(),
        market_price=_market_price(),
    )
    assert result.status == QuantityResolutionStatus.FAILED
    assert any(i.code == PAPER_LOOP_UNSUPPORTED_ORDER_TYPE for i in result.validation_result.issues)


def test_output_order_intent_passes_domain_validation() -> None:
    result = RESOLVER.resolve(
        intent=_target_weight_intent(side=OrderSide.BUY, target="5"),
        context=_context(),
        market_price=_market_price(),
    )
    assert result.order_intent is not None
    assert result.order_intent.quantity is not None
    assert result.order_intent.quantity > Decimal("0")
    assert result.order_intent.target_weight_percent is None
