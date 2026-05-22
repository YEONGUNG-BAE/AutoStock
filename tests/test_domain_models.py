from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import domain
from config.settings import ExecutionMode as ConfigExecutionMode
from domain import (
    AccountRole,
    AssetClass,
    CashSnapshot,
    Currency,
    ExecutionMode,
    Fill,
    Market,
    MarketPrice,
    Money,
    NavSnapshot,
    OrderIntent,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioSnapshot,
    Position,
)


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def test_money_preserves_decimal_amount_and_currency() -> None:
    money = Money.from_str("1234.56", Currency.KRW)

    assert money.amount == Decimal("1234.56")
    assert money.currency == Currency.KRW


def test_market_price_rejects_non_positive_price() -> None:
    with pytest.raises(ValidationError):
        MarketPrice(
            symbol="005930",
            market=Market.KR,
            currency=Currency.KRW,
            price=Decimal("0"),
            as_of=NOW,
        )


def test_market_price_rejects_non_finite_price() -> None:
    with pytest.raises(ValidationError, match="price must be a finite decimal"):
        MarketPrice(
            symbol="005930",
            market=Market.KR,
            currency=Currency.KRW,
            price=Decimal("Infinity"),
            as_of=NOW,
        )


def test_order_intent_requires_quantity_or_target_weight_percent() -> None:
    with pytest.raises(ValidationError, match="quantity or target_weight_percent"):
        _order_intent(quantity=None, target_weight_percent=None)


def test_order_intent_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValidationError, match="quantity must be greater than 0"):
        _order_intent(quantity=Decimal("0"))


def test_order_intent_rejects_target_weight_percent_outside_range() -> None:
    with pytest.raises(ValidationError, match="target_weight_percent must be between 0 and 100"):
        _order_intent(quantity=None, target_weight_percent=Decimal("100.01"))


def test_limit_order_requires_limit_price() -> None:
    with pytest.raises(ValidationError, match="LIMIT OrderIntent requires limit_price"):
        _order_intent(order_type=OrderType.LIMIT, limit_price=None)


def test_market_order_rejects_limit_price() -> None:
    with pytest.raises(ValidationError, match="MARKET OrderIntent must not include limit_price"):
        _order_intent(order_type=OrderType.MARKET, limit_price=Decimal("100"))


def test_order_result_rejects_accepted_false_without_rejection_reason() -> None:
    with pytest.raises(ValidationError, match="accepted=False requires rejection_reason"):
        OrderResult(
            order_id="order-1",
            status=OrderStatus.REJECTED,
            accepted=False,
            rejection_reason=None,
            created_at=NOW,
        )


def test_order_result_rejects_rejected_status_with_accepted_true() -> None:
    with pytest.raises(ValidationError, match="status=REJECTED must have accepted=False"):
        OrderResult(
            order_id="order-1",
            status=OrderStatus.REJECTED,
            accepted=True,
            rejection_reason=None,
            created_at=NOW,
        )


def test_fill_rejects_non_positive_quantity_or_price() -> None:
    with pytest.raises(ValidationError):
        _fill(quantity=Decimal("0"))

    with pytest.raises(ValidationError):
        _fill(fill_price=Decimal("0"))


def test_fill_rejects_negative_commission_or_tax() -> None:
    with pytest.raises(ValidationError, match="Fill commission.amount must be greater than or equal to 0"):
        _fill(commission=Money(amount=Decimal("-1"), currency=Currency.KRW))

    with pytest.raises(ValidationError, match="Fill tax.amount must be greater than or equal to 0"):
        _fill(tax=Money(amount=Decimal("-1"), currency=Currency.KRW))


def test_position_market_value_prefers_market_price() -> None:
    position = Position(
        symbol="005930",
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.ISA,
        quantity=Decimal("10"),
        avg_cost=Decimal("100"),
        currency=Currency.KRW,
        market_price=Decimal("120"),
    )

    assert position.market_value == Decimal("1200")


def test_position_rejects_negative_quantity() -> None:
    with pytest.raises(ValidationError):
        Position(
            symbol="005930",
            market=Market.KR,
            asset_class=AssetClass.KR_EQUITY,
            account_role=AccountRole.ISA,
            quantity=Decimal("-1"),
            avg_cost=Decimal("100"),
            currency=Currency.KRW,
        )


def test_cash_snapshot_rejects_negative_amount() -> None:
    with pytest.raises(ValidationError):
        CashSnapshot(
            currency=Currency.KRW,
            amount=Decimal("-1"),
            account_role=AccountRole.PAPER,
            as_of=NOW,
        )


def test_portfolio_snapshot_rejects_invested_percent_outside_range() -> None:
    with pytest.raises(ValidationError, match="invested_percent must be between 0 and 100"):
        PortfolioSnapshot(
            snapshot_id="snap-1",
            as_of=NOW,
            positions=(),
            cash=(),
            total_nav_krw=Decimal("1000000"),
            cash_krw=Decimal("200000"),
            invested_percent=Decimal("101"),
        )


def test_portfolio_snapshot_rejects_positive_mdd_percent() -> None:
    with pytest.raises(ValidationError, match="mdd_percent must be less than or equal to 0"):
        PortfolioSnapshot(
            snapshot_id="snap-1",
            as_of=NOW,
            positions=(),
            cash=(),
            total_nav_krw=Decimal("1000000"),
            cash_krw=Decimal("200000"),
            invested_percent=Decimal("80"),
            mdd_percent=Decimal("1"),
        )


def test_nav_snapshot_rejects_total_nav_mismatch_beyond_tolerance() -> None:
    with pytest.raises(ValidationError, match="total_nav_krw must match cash_krw \\+ invested_krw"):
        NavSnapshot(
            snapshot_id="nav-1",
            as_of=NOW,
            total_nav_krw=Decimal("1000"),
            cash_krw=Decimal("300"),
            invested_krw=Decimal("600"),
        )


def test_asset_class_enums_are_distinct() -> None:
    assert {AssetClass.KR_EQUITY, AssetClass.US_EQUITY, AssetClass.GOLD, AssetClass.CASH} == {
        AssetClass(item) for item in ("KR_EQUITY", "US_EQUITY", "GOLD", "CASH")
    }


def test_account_role_enums_are_distinct() -> None:
    assert {AccountRole.ISA, AccountRole.GENERAL, AccountRole.CMA, AccountRole.PAPER} == {
        AccountRole(item) for item in ("ISA", "GENERAL", "CMA", "PAPER")
    }


def test_execution_mode_matches_config_execution_mode() -> None:
    assert ExecutionMode is ConfigExecutionMode
    assert {mode.value for mode in ExecutionMode} == {
        "normal",
        "rebalancing",
        "emergency_trigger",
        "mdd_killswitch",
        "manual",
    }


def test_domain_package_exports_only_stable_public_types() -> None:
    assert set(domain.__all__) == {
        "AccountRole",
        "AssetClass",
        "CashSnapshot",
        "Currency",
        "ExecutionMode",
        "Fill",
        "Market",
        "MarketPrice",
        "Money",
        "NavSnapshot",
        "OrderIntent",
        "OrderResult",
        "OrderSide",
        "OrderStatus",
        "OrderType",
        "PortfolioSnapshot",
        "Position",
        "TimeInForce",
    }


def _order_intent(
    *,
    quantity: Decimal | None = Decimal("10"),
    target_weight_percent: Decimal | None = None,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
) -> OrderIntent:
    return OrderIntent(
        order_id="order-1",
        correlation_id="corr-1",
        symbol="005930",
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.ISA,
        side=OrderSide.BUY,
        order_type=order_type,
        execution_mode=ExecutionMode.NORMAL,
        quantity=quantity,
        target_weight_percent=target_weight_percent,
        limit_price=limit_price,
        created_at=NOW,
    )


def _fill(
    *,
    quantity: Decimal = Decimal("10"),
    fill_price: Decimal = Decimal("100"),
    commission: Money = Money(amount=Decimal("1"), currency=Currency.KRW),
    tax: Money = Money(amount=Decimal("0"), currency=Currency.KRW),
) -> Fill:
    return Fill(
        fill_id="fill-1",
        order_id="order-1",
        symbol="005930",
        market=Market.KR,
        side=OrderSide.BUY,
        quantity=quantity,
        fill_price=fill_price,
        commission=commission,
        tax=tax,
        filled_at=NOW,
    )
