from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from config.settings import AppSettings, ExecutionMode, TradingMode
from domain.enums import AccountRole, Market, OrderSide
from domain.order import OrderIntent

from broker.kis_client import resolve_account_env_var


class TinyLiveGateError(RuntimeError):
    """tiny-live manual gate 검증 실패."""


@dataclass(frozen=True)
class TinyLiveGate:
    """tiny-live manual rehearsal gate 상태 스냅샷."""

    trading_mode: TradingMode
    allow_live_trading: bool
    live_confirmation_ok: bool
    tiny_live_confirmation_ok: bool
    manual_approved: bool
    max_notional_krw: Decimal


@dataclass(frozen=True)
class TinyLiveOrderRequest:
    """dry-run tiny-live 주문 요청 객체. KIS order endpoint를 호출하지 않는다."""

    intent: OrderIntent
    estimated_notional_krw: Decimal
    account_env_var: str
    symbol: str
    market: Market
    side: OrderSide
    quantity: Decimal
    dry_run: bool = True


def build_tiny_live_gate(settings: AppSettings, *, environ: Mapping[str, str], manual_approved: bool) -> TinyLiveGate:
    live_confirmation_ok = (
        environ.get(settings.trading.live_confirmation_env_var)
        == settings.trading.live_confirmation_phrase
    )
    tiny_live_confirmation_ok = (
        environ.get(settings.trading.tiny_live_confirmation_env_var)
        == settings.trading.tiny_live_confirmation_phrase
    )
    return TinyLiveGate(
        trading_mode=settings.trading.mode,
        allow_live_trading=settings.trading.allow_live_trading,
        live_confirmation_ok=live_confirmation_ok,
        tiny_live_confirmation_ok=tiny_live_confirmation_ok,
        manual_approved=manual_approved,
        max_notional_krw=Decimal(settings.trading.max_tiny_live_notional_krw),
    )


def validate_tiny_live_manual_gate(
    *,
    settings: AppSettings,
    environ: Mapping[str, str],
    intent: OrderIntent,
    estimated_notional_krw: Decimal,
    manual_approved: bool,
) -> None:
    gate = build_tiny_live_gate(settings, environ=environ, manual_approved=manual_approved)

    if gate.trading_mode != TradingMode.LIVE:
        raise TinyLiveGateError("Tiny-live requires trading.mode=live.")

    if not gate.allow_live_trading:
        raise TinyLiveGateError("Tiny-live requires trading.allow_live_trading=true.")

    if not gate.live_confirmation_ok:
        raise TinyLiveGateError(
            f"Tiny-live requires live confirmation env var "
            f"{settings.trading.live_confirmation_env_var}={settings.trading.live_confirmation_phrase}."
        )

    if not gate.tiny_live_confirmation_ok:
        raise TinyLiveGateError(
            f"Tiny-live requires confirmation env var "
            f"{settings.trading.tiny_live_confirmation_env_var}={settings.trading.tiny_live_confirmation_phrase}."
        )

    if not gate.manual_approved:
        raise TinyLiveGateError("Tiny-live requires manual_approved=true.")

    if intent.execution_mode != ExecutionMode.MANUAL:
        raise TinyLiveGateError("Tiny-live requires OrderIntent.execution_mode=MANUAL.")

    if estimated_notional_krw > gate.max_notional_krw:
        raise TinyLiveGateError(
            f"Tiny-live notional {estimated_notional_krw} exceeds cap {gate.max_notional_krw} KRW."
        )

    if intent.account_role == AccountRole.PAPER:
        raise TinyLiveGateError("Tiny-live rejects AccountRole.PAPER.")

    if intent.account_role == AccountRole.CASH_BUFFER:
        raise TinyLiveGateError("Tiny-live rejects AccountRole.CASH_BUFFER as order execution account.")

    if intent.source_decision_id is not None:
        raise TinyLiveGateError(
            "Tiny-live rejects OrderIntent with source_decision_id (LLM/scheduler origin)."
        )

    if intent.reason_code is not None and intent.reason_code.lower() in {
        "scheduler",
        "llm",
        "allocator",
        "analysis",
        "emergency_trigger",
        "mdd_killswitch",
    }:
        raise TinyLiveGateError(
            f"Tiny-live rejects OrderIntent with scheduler/LLM reason_code={intent.reason_code}."
        )


def build_tiny_live_order_request(
    *,
    settings: AppSettings,
    environ: Mapping[str, str],
    intent: OrderIntent,
    estimated_notional_krw: Decimal,
    manual_approved: bool,
) -> TinyLiveOrderRequest:
    validate_tiny_live_manual_gate(
        settings=settings,
        environ=environ,
        intent=intent,
        estimated_notional_krw=estimated_notional_krw,
        manual_approved=manual_approved,
    )

    if intent.quantity is None:
        raise TinyLiveGateError("Tiny-live dry-run request requires explicit quantity.")

    account_env_var = resolve_account_env_var(intent.account_role, settings.broker.account_roles)

    return TinyLiveOrderRequest(
        intent=intent,
        estimated_notional_krw=estimated_notional_krw,
        account_env_var=account_env_var,
        symbol=intent.symbol,
        market=intent.market,
        side=intent.side,
        quantity=intent.quantity,
        dry_run=True,
    )


__all__ = [
    "TinyLiveGate",
    "TinyLiveGateError",
    "TinyLiveOrderRequest",
    "build_tiny_live_gate",
    "build_tiny_live_order_request",
    "validate_tiny_live_manual_gate",
]
