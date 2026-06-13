"""RTM-7c.2 — session/health execution gate (offline, pure library).

MarketMonitor post-apply hook 이후 fast-loop orchestrator가 coordinator를 호출하기 전
세션 OPEN + transport/market-data HEALTHY 여부를 검증한다. network/broker/ledger/LLM
접근이 없으며 calendar/health tracker는 주입식 protocol/구현체로만 질의한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from domain._datetime import require_timezone_aware_datetime
from domain.enums import Market
from market_data.health_policy import HealthVerdict, MarketHealthTracker
from market_data.market_session import (
    MarketCalendarProvider,
    MarketSession,
    MarketSessionError,
    MarketSessionState,
)

__all__ = [
    "ExecutionGateSnapshot",
    "ExecutionGateProvider",
    "SessionHealthExecutionGate",
    "REASON_GATE_AS_OF_MISMATCH",
    "REASON_GATE_MARKET_MISMATCH",
    "REASON_GATE_PROVIDER_ERROR",
    "REASON_HELD_HEALTH",
    "REASON_HELD_SESSION",
]


REASON_HELD_SESSION = "held_session"
REASON_HELD_HEALTH = "held_health"
REASON_GATE_AS_OF_MISMATCH = "gate_as_of_mismatch"
REASON_GATE_MARKET_MISMATCH = "gate_market_mismatch"
REASON_GATE_PROVIDER_ERROR = "gate_provider_error"


@dataclass(frozen=True)
class ExecutionGateSnapshot:
    """한 평가 시점의 session/health gate 결과. frozen이며 raw credential을 담지 않는다."""

    market: Market
    evaluated_at: datetime
    session: MarketSession
    health: HealthVerdict


class ExecutionGateProvider(Protocol):
    """주입식 execution gate. orchestrator는 구현체에 강결합하지 않는다."""

    def evaluate(self, *, market: Market, now: datetime) -> ExecutionGateSnapshot: ...


@dataclass(frozen=True)
class SessionHealthExecutionGate:
    """calendar + MarketHealthTracker 기반 기본 gate 구현."""

    calendar: MarketCalendarProvider
    tracker: MarketHealthTracker

    def evaluate(self, *, market: Market, now: datetime) -> ExecutionGateSnapshot:
        require_timezone_aware_datetime(now, field_name="now")
        session = self.calendar.session_at(market, now)
        health = self.tracker.evaluate(session=session, now=now)
        return ExecutionGateSnapshot(
            market=market,
            evaluated_at=now,
            session=session,
            health=health,
        )


def gate_execution_reason(
    gate: ExecutionGateSnapshot,
    *,
    update_market: Market,
    update_applied_at: datetime,
) -> str | None:
    """gate가 coordinator 호출을 허용하면 None, 아니면 typed reason_code."""
    if gate.market != update_market:
        return REASON_GATE_MARKET_MISMATCH
    if gate.evaluated_at != update_applied_at:
        return REASON_GATE_AS_OF_MISMATCH
    if gate.session.state is not MarketSessionState.OPEN:
        return REASON_HELD_SESSION
    if not gate.health.is_execution_ready:
        return REASON_HELD_HEALTH
    return None


def evaluate_gate_safe(
    provider: ExecutionGateProvider,
    *,
    market: Market,
    now: datetime,
) -> tuple[ExecutionGateSnapshot | None, str | None]:
    """provider 예외를 sanitized GATE_PROVIDER_ERROR로 변환한다(raw repr 금지)."""
    try:
        return provider.evaluate(market=market, now=now), None
    except MarketSessionError:
        return None, REASON_GATE_PROVIDER_ERROR
    except Exception:
        return None, REASON_GATE_PROVIDER_ERROR
