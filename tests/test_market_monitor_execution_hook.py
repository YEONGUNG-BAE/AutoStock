"""RTM-7c.2 — MarketMonitor neutral post-apply hook tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domain.enums import Currency, Market
from market_data.latest_state import LatestMarketStateStore
from market_data.models import (
    MarketEvent,
    MarketHeartbeat,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)
from market_data.monitor import (
    AppliedMarketUpdate,
    MarketMonitor,
    MonitorInternalError,
    ReconnectPolicy,
)
from market_data.replay_source import ReplayMarketEventSource
from market_data.rolling_window import RollingObserveStatus, RollingRetentionPolicy, RollingTradeHistoryStore
from market_data.supervisor import MarketSupervisor, SupervisorAction, SupervisorState, provisional_supervisor_policy
from market_data.health_policy import MarketHealthTracker, provisional_thresholds
from market_data.market_session import MarketSession, MarketSessionState

_BASE = datetime(2026, 6, 10, 0, 5, 0, tzinfo=UTC)
_TRADE_CH = "H0STCNT0|005930"
_QUOTE_CH = "H0STASP0|005930"


def _trade(*, sequence: int, at: datetime | None = None) -> NormalizedTradeTick:
    t = at or (_BASE + timedelta(seconds=sequence))
    return NormalizedTradeTick(
        provider="kis", symbol="005930", market=Market.KR, currency=Currency.KRW,
        price=Decimal("70000"), quantity=Decimal("10"), trade_at=t, received_at=t,
        provider_sequence=ProviderSequence(provider="kis", channel=_TRADE_CH, sequence=sequence, received_at=t),
    )


def _quote(*, sequence: int, at: datetime | None = None) -> NormalizedBestBidAsk:
    t = at or (_BASE + timedelta(seconds=sequence))
    return NormalizedBestBidAsk(
        provider="kis", symbol="005930", market=Market.KR, currency=Currency.KRW,
        bid_price=Decimal("69900"), ask_price=Decimal("70100"),
        bid_quantity=Decimal("10"), ask_quantity=Decimal("10"),
        quote_at=t, received_at=t,
        provider_sequence=ProviderSequence(provider="kis", channel=_QUOTE_CH, sequence=sequence, received_at=t),
    )


def _heartbeat() -> MarketHeartbeat:
    return MarketHeartbeat(provider="kis", channel="PINGPONG", sent_at=_BASE, received_at=_BASE)


class _SteppingClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _run_monitor(
    events: list[MarketEvent],
    *,
    clock: Callable[[], datetime] | None = None,
    rolling_store: RollingTradeHistoryStore | None = None,
    on_applied_update: Callable[[AppliedMarketUpdate], None] | None = None,
) -> None:
    store = LatestMarketStateStore()
    clk = clock or (lambda: _BASE + timedelta(hours=1))
    monitor = MarketMonitor(
        store=store,
        rolling_store=rolling_store,
        source_factory=lambda: ReplayMarketEventSource(events),
        clock=clk,
        session_id="hook-test",
        max_events=len(events) + 5,
        on_applied_update=on_applied_update,
    )
    asyncio.run(monitor.run())


def test_applied_trade_invokes_callback_once() -> None:
    calls: list[AppliedMarketUpdate] = []
    _run_monitor([_trade(sequence=1)], on_applied_update=calls.append)
    assert len(calls) == 1
    assert calls[0].event_type.value == "trade"
    assert calls[0].sequence == 1


def test_applied_quote_invokes_callback_once() -> None:
    calls: list[AppliedMarketUpdate] = []
    _run_monitor([_quote(sequence=1)], on_applied_update=calls.append)
    assert len(calls) == 1
    assert calls[0].event_type.value == "best_bid_ask"


def test_heartbeat_no_callback() -> None:
    calls: list[AppliedMarketUpdate] = []
    _run_monitor([_heartbeat()], on_applied_update=calls.append)
    assert calls == []


def test_duplicate_no_callback() -> None:
    calls: list[AppliedMarketUpdate] = []
    _run_monitor([_trade(sequence=1), _trade(sequence=1)], on_applied_update=calls.append)
    assert len(calls) == 1


def test_out_of_order_no_callback() -> None:
    calls: list[AppliedMarketUpdate] = []
    _run_monitor([_trade(sequence=2), _trade(sequence=1)], on_applied_update=calls.append)
    assert len(calls) == 1


def test_future_event_no_callback() -> None:
    calls: list[AppliedMarketUpdate] = []
    future = _BASE + timedelta(hours=2)
    late = _BASE + timedelta(hours=1)
    _run_monitor(
        [_trade(sequence=1, at=future)],
        clock=lambda: late,
        on_applied_update=calls.append,
    )
    assert calls == []


def test_stream_mismatch_no_callback() -> None:
    calls: list[AppliedMarketUpdate] = []
    t1 = _trade(sequence=1)
    t2 = NormalizedTradeTick(
        provider="other",
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        price=Decimal("70000"),
        quantity=Decimal("10"),
        trade_at=_BASE + timedelta(seconds=2),
        received_at=_BASE + timedelta(seconds=2),
        provider_sequence=ProviderSequence(
            provider="other", channel="OTHER|005930", sequence=1,
            received_at=_BASE + timedelta(seconds=2),
        ),
    )
    _run_monitor([t1, t2], on_applied_update=calls.append)
    assert len(calls) == 1


def test_callback_applied_at_matches_shared_now() -> None:
    clock = _SteppingClock(_BASE + timedelta(hours=1))
    calls: list[AppliedMarketUpdate] = []
    _run_monitor([_trade(sequence=1)], clock=clock, on_applied_update=calls.append)
    assert calls[0].applied_at == clock()


def test_callback_none_preserves_monitor_behavior() -> None:
    store = LatestMarketStateStore()
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource([_trade(sequence=1)]),
        clock=lambda: _BASE + timedelta(hours=1),
        session_id="no-hook",
        max_events=1,
    )
    summary = asyncio.run(monitor.run())
    assert summary.applied == 1


def test_callback_exception_is_monitor_internal_error() -> None:
    def _boom(_update: AppliedMarketUpdate) -> None:
        raise RuntimeError("secret")

    with pytest.raises(MonitorInternalError, match="post_apply_hook failed"):
        _run_monitor([_trade(sequence=1)], on_applied_update=_boom)


class _RejectingRollingStore:
    def observe(self, event: NormalizedTradeTick, *, now: datetime) -> object:
        from market_data.rolling_window import RollingObserveResult

        return RollingObserveResult(status=RollingObserveStatus.OUT_OF_ORDER)

    def reset_stream(self, provider: str, channel: str) -> None:
        pass


def test_rolling_observe_failure_no_callback() -> None:
    calls: list[AppliedMarketUpdate] = []
    rolling = RollingTradeHistoryStore(
        retention=RollingRetentionPolicy(hard_max_events=100, hard_max_age_seconds=Decimal("3600"))
    )
    store = LatestMarketStateStore()
    bad = _RejectingRollingStore()
    monitor = MarketMonitor(
        store=store,
        rolling_store=bad,  # type: ignore[arg-type]
        source_factory=lambda: ReplayMarketEventSource([_trade(sequence=1)]),
        clock=lambda: _BASE + timedelta(hours=1),
        session_id="roll-fail",
        max_events=1,
        on_applied_update=calls.append,
    )
    with pytest.raises(MonitorInternalError):
        asyncio.run(monitor.run())
    assert calls == []


class _HookFailMonitor:
    async def run(self):
        raise MonitorInternalError("post_apply_hook failed")


class _OpenCalendar:
    def session_at(self, market: Market, instant: datetime) -> MarketSession:
        return MarketSession(market=market, state=MarketSessionState.OPEN, as_of=instant)

    def is_trading_day(self, market: Market, day: object) -> bool:
        return True


async def _fake_sleep(_: float) -> None:
    await asyncio.sleep(0)


def test_supervisor_internal_failure_failed_closed_no_restart() -> None:
    sup = MarketSupervisor(
        market=Market.KR,
        calendar=_OpenCalendar(),
        monitor_factory=_HookFailMonitor,
        tracker=MarketHealthTracker(provisional_thresholds()),
        clock=lambda: _BASE,
        sleep=_fake_sleep,
        policy=provisional_supervisor_policy(),
        max_ticks=2,
    )
    summary = asyncio.run(sup.run())
    assert summary.final_state is SupervisorState.FAILED_CLOSED
    assert summary.monitor_restarts == 0
