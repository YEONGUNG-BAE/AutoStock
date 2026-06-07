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
    MonitorInternalError,
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


class _SteppingClock:
    """첫 호출은 start, 이후 호출은 start+step. wait_for의 deadline 계산을 음수로
    만들어 실제 대기 없이 timeout 분기를 결정론적으로 태운다."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self._start = start
        self._step = step
        self._calls = 0

    def __call__(self) -> datetime:
        self._calls += 1
        return self._start if self._calls == 1 else self._start + self._step


class _ApplyRaisingStore:
    """store.apply가 generic 예외를 던지는 스텁. 내부 결함이 transport drop으로
    오인되지 않고 MonitorInternalError로 전파되는지 검증하는 데 쓴다."""

    def apply(self, event: MarketEvent, *, now: datetime) -> object:
        raise RuntimeError("internal store defect")

    def reset_stream(self, provider: str, channel: str) -> None:  # pragma: no cover
        pass


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


# --- hardening item 1: reset is deferred to the new epoch's first event -----


def test_reconnect_without_new_event_preserves_old_state() -> None:
    """새 source가 첫 이벤트도 못 내고 EOF면 reset이 일어나지 않아 기존 state가 보존된다.
    reset이 '재접속 시작'이 아니라 '새 stream 첫 이벤트 수신 직후'에만 일어남을 못박는다."""
    store = LatestMarketStateStore()
    clock = _fixed_clock(_BASE + timedelta(seconds=5))
    first = _FaultySource([_trade(sequence=5, price="70000")], yields_before=1)
    second = ReplayMarketEventSource([])  # immediate EOF, no events
    sources: list[MarketEventSource] = [first, second]
    evidence: list[MonitorEvidence] = []

    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: sources.pop(0),
        clock=clock,
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=5),
        sleep=_RecordingSleep(),
        session_id="hard-1",
        on_evidence=evidence.append,
    )
    summary = _run(monitor)

    assert summary.final_state is MonitorState.STOPPED
    assert summary.connection_attempts == 2
    # no reset evidence: the second epoch never delivered an event to trigger it
    assert not any(e.kind == "reset" for e in evidence)
    # old state from the first epoch survives untouched
    snap = store.peek(Market.KR, "005930", now=_BASE + timedelta(seconds=5))
    assert snap.trade is not None
    assert snap.trade.provider_sequence.sequence == 5
    assert str(snap.trade.price) == "70000"


# --- hardening item 2: heartbeat stale watchdog reconnects -------------------


def test_heartbeat_watchdog_drops_on_silence() -> None:
    """heartbeat_watch가 설정되면 다음 이벤트를 timeout 안에 못 받을 때 half-dead
    연결로 보고 drop·reconnect한다."""
    store = LatestMarketStateStore()
    started = asyncio.Event()
    evidence: list[MonitorEvidence] = []
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _BlockingSource(started),
        clock=_fixed_clock(_BASE),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=1),
        sleep=_RecordingSleep(),
        session_id="hard-2",
        heartbeat_watch=("kis", "PINGPONG"),
        heartbeat_timeout_seconds=0.05,
        on_evidence=evidence.append,
    )
    with pytest.raises(MonitorExhaustedError) as excinfo:
        _run(monitor)
    assert excinfo.value.summary.final_state is MonitorState.EXHAUSTED
    drops = [e for e in evidence if e.kind == "drop"]
    assert drops and all(e.reason_code == "heartbeat_stale" for e in drops)


# --- hardening item 3: runtime timeout fires even on a silent source ---------


def test_runtime_timeout_stops_silent_source() -> None:
    """silent source(이벤트 무한 대기)에도 max_runtime_seconds가 작동해 budget 종료한다.
    stepping clock으로 deadline을 음수화해 실제 대기 없이 timeout 분기를 태운다."""
    store = LatestMarketStateStore()
    started = asyncio.Event()
    evidence: list[MonitorEvidence] = []
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _BlockingSource(started),
        clock=_SteppingClock(_BASE, timedelta(seconds=100)),
        session_id="hard-3",
        max_runtime_seconds=0.05,
        on_evidence=evidence.append,
    )
    summary = _run(monitor)
    assert summary.final_state is MonitorState.STOPPED
    assert summary.connection_attempts == 1
    stops = [e for e in evidence if e.kind == "stop"]
    assert stops and all(e.reason_code == "runtime_timeout" for e in stops)


# --- hardening item 4: internal vs transport error boundary ------------------


def test_internal_store_error_propagates_not_reconnect() -> None:
    """store.apply 내부 결함은 MonitorInternalError로 즉시 전파되고, transport drop으로
    오인해 backoff·reconnect하지 않는다."""
    sleep = _RecordingSleep()
    monitor = MarketMonitor(
        store=_ApplyRaisingStore(),  # type: ignore[arg-type]
        source_factory=lambda: ReplayMarketEventSource([_trade(sequence=1)]),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=5),
        sleep=sleep,
        session_id="hard-4a",
    )
    with pytest.raises(MonitorInternalError):
        _run(monitor)
    assert sleep.calls == []  # never backed off
    assert monitor.state is MonitorState.STOPPED


def test_factory_error_triggers_backoff_not_death() -> None:
    """source_factory 자체 오류는 transport drop으로 분류돼 backoff·reconnect한다."""
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()
    calls = {"n": 0}

    def factory() -> MarketEventSource:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connect failed")
        return ReplayMarketEventSource([_trade(sequence=1, price="80000")])

    monitor = MarketMonitor(
        store=store,
        source_factory=factory,
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=5),
        sleep=sleep,
        session_id="hard-4b",
    )
    summary = _run(monitor)
    assert summary.applied == 1
    assert summary.connection_attempts == 2
    assert sleep.calls == [1.0]
    assert summary.final_state is MonitorState.STOPPED


# --- hardening item 5: policy + budget argument validation -------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"multiplier": 0.0},
        {"initial_delay_seconds": -1.0},
        {"initial_delay_seconds": 1.0, "max_delay_seconds": 0.5},
    ],
)
def test_reconnect_policy_rejects_invalid_args(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ReconnectPolicy(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_events": 0},
        {"max_runtime_seconds": 0.0},
        {"heartbeat_watch": ("kis", "PINGPONG")},  # timeout missing
        {"heartbeat_timeout_seconds": 1.0},  # watch missing
        {"heartbeat_watch": ("kis", "PINGPONG"), "heartbeat_timeout_seconds": 0.0},
    ],
)
def test_monitor_rejects_invalid_budget_args(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        MarketMonitor(
            store=LatestMarketStateStore(),
            source_factory=lambda: ReplayMarketEventSource([]),
            clock=_fixed_clock(_BASE),
            session_id="hard-5",
            **kwargs,  # type: ignore[arg-type]
        )
