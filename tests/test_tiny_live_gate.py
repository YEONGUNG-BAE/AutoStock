from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from broker.tiny_live_gate import (
    TinyLiveGateError,
    build_tiny_live_order_request,
    validate_tiny_live_manual_gate,
)
from config.settings import (
    AppSettings,
    BrokerAccountRoleSettings,
    BrokerAdapterName,
    BrokerSettings,
    ExecutionMode,
    KisLiveSettings,
    TradingMode,
    TradingSettings,
    load_settings,
)
from domain.enums import AccountRole, AssetClass, Market, OrderSide, OrderType
from domain.order import OrderIntent


def _live_settings(**trading_overrides) -> AppSettings:
    trading_defaults = {
        "mode": TradingMode.LIVE,
        "allow_live_trading": True,
        "live_confirmation_env_var": "LIVE_TRADING_CONFIRM",
        "live_confirmation_phrase": "ENABLE_LIVE_TRADING",
        "tiny_live_confirmation_env_var": "TINY_LIVE_CONFIRM",
        "tiny_live_confirmation_phrase": "ENABLE_TINY_LIVE",
        "max_tiny_live_notional_krw": 100_000,
    }
    trading_defaults.update(trading_overrides)
    return AppSettings(
        trading=TradingSettings(**trading_defaults),
        broker=BrokerSettings(
            adapter=BrokerAdapterName.KIS_LIVE,
            live=KisLiveSettings(),
            account_roles=BrokerAccountRoleSettings(),
        ),
    )


def _full_environ() -> dict[str, str]:
    return {
        "LIVE_TRADING_CONFIRM": "ENABLE_LIVE_TRADING",
        "TINY_LIVE_CONFIRM": "ENABLE_TINY_LIVE",
    }


def _manual_intent(**overrides) -> OrderIntent:
    defaults = {
        "order_id": "ord-tiny-1",
        "correlation_id": "corr-tiny-1",
        "symbol": "005930",
        "market": Market.KR,
        "asset_class": AssetClass.KR_EQUITY,
        "account_role": AccountRole.KR_TAX_ADVANTAGED,
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "execution_mode": ExecutionMode.MANUAL,
        "quantity": Decimal("1"),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def test_rejects_by_default_paper_mode() -> None:
    settings = AppSettings()
    intent = _manual_intent()

    with pytest.raises(TinyLiveGateError, match="trading.mode=live"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ={},
            intent=intent,
            estimated_notional_krw=Decimal("10000"),
            manual_approved=True,
        )


def test_rejects_live_without_allow_live_trading() -> None:
    settings = _live_settings(allow_live_trading=False)
    intent = _manual_intent()

    with pytest.raises(TinyLiveGateError, match="allow_live_trading=true"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ=_full_environ(),
            intent=intent,
            estimated_notional_krw=Decimal("10000"),
            manual_approved=True,
        )


def test_rejects_missing_live_confirmation() -> None:
    settings = _live_settings()
    intent = _manual_intent()
    environ = {"TINY_LIVE_CONFIRM": "ENABLE_TINY_LIVE"}

    with pytest.raises(TinyLiveGateError, match="live confirmation"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ=environ,
            intent=intent,
            estimated_notional_krw=Decimal("10000"),
            manual_approved=True,
        )


def test_rejects_missing_tiny_live_confirmation() -> None:
    settings = _live_settings()
    intent = _manual_intent()
    environ = {"LIVE_TRADING_CONFIRM": "ENABLE_LIVE_TRADING"}

    with pytest.raises(TinyLiveGateError, match="TINY_LIVE_CONFIRM"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ=environ,
            intent=intent,
            estimated_notional_krw=Decimal("10000"),
            manual_approved=True,
        )


def test_rejects_manual_approved_false() -> None:
    settings = _live_settings()
    intent = _manual_intent()

    with pytest.raises(TinyLiveGateError, match="manual_approved=true"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ=_full_environ(),
            intent=intent,
            estimated_notional_krw=Decimal("10000"),
            manual_approved=False,
        )


def test_rejects_non_manual_execution_mode() -> None:
    settings = _live_settings()
    intent = _manual_intent(execution_mode=ExecutionMode.NORMAL)

    with pytest.raises(TinyLiveGateError, match="execution_mode=MANUAL"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ=_full_environ(),
            intent=intent,
            estimated_notional_krw=Decimal("10000"),
            manual_approved=True,
        )


def test_rejects_over_cap() -> None:
    settings = _live_settings(max_tiny_live_notional_krw=50_000)
    intent = _manual_intent()

    with pytest.raises(TinyLiveGateError, match="exceeds cap"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ=_full_environ(),
            intent=intent,
            estimated_notional_krw=Decimal("100000"),
            manual_approved=True,
        )


def test_rejects_llm_origin_source_decision_id() -> None:
    settings = _live_settings()
    intent = _manual_intent(source_decision_id="decision-llm-123")

    with pytest.raises(TinyLiveGateError, match="source_decision_id"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ=_full_environ(),
            intent=intent,
            estimated_notional_krw=Decimal("10000"),
            manual_approved=True,
        )


def test_rejects_scheduler_reason_code() -> None:
    settings = _live_settings()
    intent = _manual_intent(reason_code="scheduler")

    with pytest.raises(TinyLiveGateError, match="reason_code=scheduler"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ=_full_environ(),
            intent=intent,
            estimated_notional_krw=Decimal("10000"),
            manual_approved=True,
        )


def test_rejects_paper_account_role() -> None:
    settings = _live_settings()
    intent = _manual_intent(account_role=AccountRole.PAPER)

    with pytest.raises(TinyLiveGateError, match="AccountRole.PAPER"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ=_full_environ(),
            intent=intent,
            estimated_notional_krw=Decimal("10000"),
            manual_approved=True,
        )


def test_rejects_cash_buffer_account_role() -> None:
    settings = _live_settings()
    intent = _manual_intent(account_role=AccountRole.CASH_BUFFER)

    with pytest.raises(TinyLiveGateError, match="CASH_BUFFER"):
        validate_tiny_live_manual_gate(
            settings=settings,
            environ=_full_environ(),
            intent=intent,
            estimated_notional_krw=Decimal("10000"),
            manual_approved=True,
        )


def test_accepts_fully_gated_manual_dry_run_request() -> None:
    settings = _live_settings()
    intent = _manual_intent()

    request = build_tiny_live_order_request(
        settings=settings,
        environ=_full_environ(),
        intent=intent,
        estimated_notional_krw=Decimal("50000"),
        manual_approved=True,
    )

    assert request.dry_run is True
    assert request.account_env_var == "KIS_ISA_ACCOUNT"
    assert request.quantity == Decimal("1")


def test_no_submit_tiny_live_order_function_in_module() -> None:
    import broker.tiny_live_gate as module

    assert not hasattr(module, "submit_tiny_live_order")
    assert not hasattr(module, "place_tiny_live_order")
