"""RTM-3 — fake-transport market monitor tests (network/broker/ledger-free)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domain.enums import Currency, Market
from market_data.latest_state import LatestMarketStateStore, MissingMarketStateError
from market_data.models import (
    MarketEvent,
    MarketHeartbeat,
    NormalizedTradeTick,
    ProviderSequence,
)
from market_data.monitor import (
    MarketMonitor,
    MonitorEvidence,
    MonitorExhaustedError,
    MonitorState,
    ReconnectPolicy,
)
from market_data.protocols import MarketEventSource
from market_data.replay_source import ReplayMarketEventSource

_BASE = datetime(2026, 6, 8, 0, 5, 0, tzinfo=UTC)


def _trade(*, sequence: int, trade_at: datetime = _BASE, price: str = "70000") -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        price=Decimal(price),
        quantity=Decimal("10"),
        trade_at=trade_at,
        received_at=trade_at,
        provider_sequence=ProviderSequence(
            provider="kis", channel="H0STCNT0|005930", sequence=sequence, received_at=trade_at
        ),
    )


def _fixed_clock(now: datetime) -> Callable[[], datetime]:
    return lambda: now


class _RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _FaultySource:
    """yields_before개의 이벤트를 흘린 뒤 transport 단절을 모사해 예외를 던진다."""

    def __init__(self, events: list[MarketEvent], *, yields_before: int) -> None:
        self._events = events
        self._yields_before = yields_before

    async def events(self) -> AsyncIterator[MarketEvent]:
        for i, event in enumerate(self._events):
            if i >= self._yields_before:
                break
            yield event
        raise RuntimeError("simulated transport drop")


class _BlockingSource:
    def __init__(self, started: asyncio.Event) -> None:
        self._started = started

    async def events(self) -> AsyncIterator[MarketEvent]:
        self._started.set()
        await asyncio.Event().wait()  # never returns -> awaits cancellation
        yield  # pragma: no cover - unreachable


def _run(monitor: MarketMonitor):
    return asyncio.run(monitor.run())


# --- replay source ----------------------------------------------------------


def test_replay_source_yields_in_order_then_eof() -> None:
    events = [_trade(sequence=1), _trade(sequence=2)]
    source = ReplayMarketEventSource(events)

    async def drain() -> list[MarketEvent]:
        return [e async for e in source.events()]

    first = asyncio.run(drain())
    second = asyncio.run(drain())  # fresh iterator each call
    assert [e.provider_sequence.sequence for e in first] == [1, 2]
    assert [e.provider_sequence.sequence for e in second] == [1, 2]


def test_replay_source_satisfies_protocol() -> None:
    assert isinstance(ReplayMarketEventSource([]), MarketEventSource)


# --- happy path / counts ----------------------------------------------------


def test_monitor_applies_events_and_summarizes() -> None:
    store = LatestMarketStateStore()
    events = [_trade(sequence=1), _trade(sequence=2), _trade(sequence=3)]
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource(events),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        session_id="sess-1",
    )
    summary = _run(monitor)
    assert summary.applied == 3
    assert summary.connection_attempts == 1
    assert summary.final_state is MonitorState.STOPPED
    assert monitor.state is MonitorState.STOPPED


def test_monitor_counts_duplicate_and_out_of_order() -> None:
    store = LatestMarketStateStore()
    events = [_trade(sequence=2), _trade(sequence=2), _trade(sequence=1)]
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource(events),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        session_id="sess-2",
    )
    summary = _run(monitor)
    assert summary.applied == 1
    assert summary.duplicate == 1
    assert summary.out_of_order == 1


def test_monitor_future_event_counted_and_does_not_crash() -> None:
    store = LatestMarketStateStore()
    # clock fixed at base; event trade_at 10s in the future -> FutureMarketEventError
    events = [_trade(sequence=1, trade_at=_BASE + timedelta(seconds=10))]
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource(events),
        clock=_fixed_clock(_BASE),
        session_id="sess-3",
    )
    summary = _run(monitor)
    assert summary.future_event_error == 1
    assert summary.applied == 0
    assert summary.final_state is MonitorState.STOPPED


def test_monitor_max_events_budget_stops_early() -> None:
    store = LatestMarketStateStore()
    events = [_trade(sequence=i) for i in range(1, 6)]
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource(events),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        session_id="sess-4",
        max_events=2,
    )
    summary = _run(monitor)
    assert summary.applied == 2
    assert summary.final_state is MonitorState.STOPPED


# --- backoff (pure) ---------------------------------------------------------


def test_backoff_is_pure_and_deterministic() -> None:
    policy = ReconnectPolicy(initial_delay_seconds=1.0, multiplier=2.0, max_delay_seconds=10.0)
    assert policy.delay_for_attempt(1) == 1.0
    assert policy.delay_for_attempt(2) == 2.0
    assert policy.delay_for_attempt(3) == 4.0
    assert policy.delay_for_attempt(4) == 8.0
    assert policy.delay_for_attempt(5) == 10.0  # capped
    with pytest.raises(ValueError):
        policy.delay_for_attempt(0)


# --- reconnect + explicit stream reset --------------------------------------


def test_monitor_reconnects_then_resets_stream() -> None:
    store = LatestMarketStateStore()
    clock = _fixed_clock(_BASE + timedelta(seconds=5))
    sleep = _RecordingSleep()
    first = _FaultySource([_trade(sequence=5, price="70000")], yields_before=1)
    second = ReplayMarketEventSource([_trade(sequence=1, price="80000")])
    sources: list[MarketEventSource] = [first, second]
    evidence: list[MonitorEvidence] = []

    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: sources.pop(0),
        clock=clock,
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=5),
        sleep=sleep,
        session_id="sess-5",
        on_evidence=evidence.append,
    )
    summary = _run(monitor)

    # backed off exactly once with the first-attempt delay
    assert sleep.calls == [1.0]
    assert summary.connection_attempts == 2
    # explicit stream reset happened on confirmed reconnect
    assert any(e.kind == "reset" for e in evidence)
    # after reset, a NEW epoch sequence=1 was accepted despite being < old sequence=5
    snap = store.peek(Market.KR, "005930", now=_BASE + timedelta(seconds=5))
    assert snap.trade is not None
    assert str(snap.trade.price) == "80000"
    assert snap.trade.provider_sequence.sequence == 1


def test_monitor_exhausts_after_max_attempts() -> None:
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _FaultySource([_trade(sequence=1)], yields_before=0),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=2),
        sleep=sleep,
        session_id="sess-6",
    )
    with pytest.raises(MonitorExhaustedError) as excinfo:
        _run(monitor)
    summary = excinfo.value.summary
    assert summary.final_state is MonitorState.EXHAUSTED
    assert summary.connection_attempts == 2
    assert sleep.calls == [1.0]  # backoff only after the first failure, not after exhaustion


# --- cancellation -----------------------------------------------------------


def test_monitor_cancellation_reraises_and_stops() -> None:
    store = LatestMarketStateStore()
    started = asyncio.Event()
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _BlockingSource(started),
        clock=_fixed_clock(_BASE),
        session_id="sess-7",
    )

    async def scenario() -> None:
        task = asyncio.create_task(monitor.run())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert monitor.state is MonitorState.STOPPED


# --- evidence isolation -----------------------------------------------------


def test_evidence_never_leaks_price_or_raw_values() -> None:
    store = LatestMarketStateStore()
    sentinel = "999999999"
    events = [_trade(sequence=1, price=sentinel)]
    evidence: list[MonitorEvidence] = []
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _FaultySource(events, yields_before=1),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=1),
        sleep=_RecordingSleep(),
        session_id="sess-8",
        on_evidence=evidence.append,
    )
    with pytest.raises(MonitorExhaustedError):
        _run(monitor)
    # drop evidence carries only a generic reason_code, never the exception text or price
    for e in evidence:
        assert sentinel not in str(e)
        assert "simulated transport drop" not in str(e)
    drops = [e for e in evidence if e.kind == "drop"]
    assert drops and all(e.reason_code == "source_error" for e in drops)


def test_require_fresh_missing_after_reset() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=3), now=_BASE + timedelta(seconds=1))
    store.reset_stream("kis", "H0STCNT0|005930")
    with pytest.raises(MissingMarketStateError):
        store.require_fresh(Market.KR, "005930", now=_BASE + timedelta(seconds=2))
