"""RTM-4b.2 — MarketMonitor rolling_store plumbing (network/broker-free).

rolling_store=None이면 RTM-3와 완전히 동일하게 동작한다. rolling_store가 주입되면
APPLIED trade만 rolling history에 mirror하고(quote/heartbeat/non-APPLIED 제외), 재접속
reset 시 latest와 rolling을 함께 reset한다. latest가 APPLIED로 판정한 trade를 rolling이
거부하면 두 store 계약이 어긋난 내부 invariant 위반이므로 MonitorInternalError로
fail-closed한다. lock 중첩/실제 네트워크/clock 재독은 없다(주입된 단일 now 공유).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
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
from market_data.monitor import MarketMonitor, MonitorInternalError, ReconnectPolicy
from market_data.replay_source import ReplayMarketEventSource
from market_data.rolling_window import (
    RollingObserveResult,
    RollingObserveStatus,
    RollingRetentionPolicy,
    RollingTradeHistoryStore,
)

_BASE = datetime(2026, 6, 10, 0, 5, 0, tzinfo=UTC)
_TRADE_CHANNEL = "H0STCNT0|005930"
_QUOTE_CHANNEL = "H0STASP0|005930"


def _trade(*, sequence: int, trade_at: datetime | None = None) -> NormalizedTradeTick:
    t = trade_at or (_BASE + timedelta(seconds=sequence))
    return NormalizedTradeTick(
        provider="kis", symbol="005930", market=Market.KR, currency=Currency.KRW,
        price=Decimal("70000"), quantity=Decimal("10"), trade_at=t, received_at=t,
        provider_sequence=ProviderSequence(provider="kis", channel=_TRADE_CHANNEL, sequence=sequence, received_at=t),
    )


def _quote(*, sequence: int) -> NormalizedBestBidAsk:
    t = _BASE + timedelta(seconds=sequence)
    return NormalizedBestBidAsk(
        provider="kis", symbol="005930", market=Market.KR, currency=Currency.KRW,
        bid_price=Decimal("69900"), ask_price=Decimal("70100"),
        bid_quantity=Decimal("10"), ask_quantity=Decimal("10"),
        quote_at=t, received_at=t,
        provider_sequence=ProviderSequence(provider="kis", channel=_QUOTE_CHANNEL, sequence=sequence, received_at=t),
    )


def _heartbeat() -> MarketHeartbeat:
    return MarketHeartbeat(provider="kis", channel="PINGPONG", sent_at=_BASE, received_at=_BASE)


def _retention() -> RollingRetentionPolicy:
    return RollingRetentionPolicy(hard_max_events=1000, hard_max_age_seconds=Decimal("86400"))


def _late_clock() -> Callable[[], datetime]:
    # all events are <= this fixed now, so apply/observe never see a future event.
    return lambda: _BASE + timedelta(hours=1)


def _monitor(
    events: list[MarketEvent],
    *,
    store: LatestMarketStateStore | None = None,
    rolling_store: RollingTradeHistoryStore | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MarketMonitor:
    store = store or LatestMarketStateStore()
    return MarketMonitor(
        store=store,
        rolling_store=rolling_store,
        source_factory=lambda: ReplayMarketEventSource(list(events)),
        clock=clock or _late_clock(),
        session_id="sess-roll",
        max_events=len(events),
    )


def _run(monitor: MarketMonitor):
    return asyncio.run(monitor.run())


def _samples(rolling: RollingTradeHistoryStore) -> tuple:
    snap = rolling.peek_history(Market.KR, "005930", now=_BASE + timedelta(hours=1))
    return snap.samples


# --------------------------------------------------------------------------- #
# rolling_store=None → identical to RTM-3
# --------------------------------------------------------------------------- #
def test_none_rolling_store_runs_and_applies() -> None:
    summary = _run(_monitor([_trade(sequence=1), _trade(sequence=2)]))
    assert summary.applied == 2


# --------------------------------------------------------------------------- #
# trade APPLIED → mirrored once into rolling history
# --------------------------------------------------------------------------- #
def test_trade_applied_mirrors_into_rolling() -> None:
    rolling = RollingTradeHistoryStore(retention=_retention())
    _run(_monitor([_trade(sequence=1), _trade(sequence=2)], rolling_store=rolling))
    samples = _samples(rolling)
    assert len(samples) == 2
    assert [s.sequence for s in samples] == [1, 2]


def test_quote_and_heartbeat_do_not_mirror() -> None:
    rolling = RollingTradeHistoryStore(retention=_retention())
    _run(_monitor([_heartbeat(), _quote(sequence=1), _trade(sequence=2)], rolling_store=rolling))
    samples = _samples(rolling)
    assert len(samples) == 1
    assert samples[0].sequence == 2


def test_duplicate_trade_not_mirrored_twice() -> None:
    rolling = RollingTradeHistoryStore(retention=_retention())
    # same sequence twice → latest store rejects the 2nd as DUPLICATE → no mirror.
    _run(_monitor([_trade(sequence=1), _trade(sequence=1)], rolling_store=rolling))
    assert len(_samples(rolling)) == 1


def test_out_of_order_trade_not_mirrored() -> None:
    rolling = RollingTradeHistoryStore(retention=_retention())
    _run(_monitor([_trade(sequence=2), _trade(sequence=1)], rolling_store=rolling))
    samples = _samples(rolling)
    assert len(samples) == 1
    assert samples[0].sequence == 2


# --------------------------------------------------------------------------- #
# partial-update fail-closed: latest APPLIED but rolling not APPLIED
# --------------------------------------------------------------------------- #
class _RejectingRolling:
    """latest가 APPLIED로 판정한 trade를 rolling이 비-APPLIED로 거부하는 스텁."""

    def observe(self, tick: NormalizedTradeTick, *, now: datetime) -> RollingObserveResult:
        return RollingObserveResult(RollingObserveStatus.STREAM_MISMATCH, "forced")

    def reset_stream(self, provider: str, channel: str) -> None:  # pragma: no cover
        pass


class _RaisingRolling:
    def observe(self, tick: NormalizedTradeTick, *, now: datetime) -> RollingObserveResult:
        raise RuntimeError("rolling defect")

    def reset_stream(self, provider: str, channel: str) -> None:  # pragma: no cover
        pass


def test_latest_applied_rolling_non_applied_raises_internal() -> None:
    monitor = _monitor([_trade(sequence=1)], rolling_store=_RejectingRolling())  # type: ignore[arg-type]
    with pytest.raises(MonitorInternalError, match="not APPLIED"):
        _run(monitor)


def test_rolling_observe_exception_raises_internal() -> None:
    monitor = _monitor([_trade(sequence=1)], rolling_store=_RaisingRolling())  # type: ignore[arg-type]
    with pytest.raises(MonitorInternalError, match="rolling_store.observe failed"):
        _run(monitor)


# --------------------------------------------------------------------------- #
# reconnect: latest + rolling reset together on first event of new epoch
# --------------------------------------------------------------------------- #
class _DropAfterFirst:
    """첫 trade를 흘린 뒤 transport 단절을 모사한다."""

    def __init__(self, event: MarketEvent) -> None:
        self._event = event

    async def events(self) -> AsyncIterator[MarketEvent]:
        yield self._event
        raise RuntimeError("simulated transport drop")


def test_reconnect_resets_both_latest_and_rolling() -> None:
    rolling = RollingTradeHistoryStore(retention=_retention())
    store = LatestMarketStateStore()
    sources = iter(
        [
            _DropAfterFirst(_trade(sequence=5)),  # attempt 1: one trade then drop
            ReplayMarketEventSource([_trade(sequence=1)]),  # attempt 2: fresh epoch, low seq
        ]
    )
    recording_sleep_calls: list[float] = []

    async def _sleep(seconds: float) -> None:
        recording_sleep_calls.append(seconds)

    monitor = MarketMonitor(
        store=store,
        rolling_store=rolling,
        source_factory=lambda: next(sources),
        clock=_late_clock(),
        sleep=_sleep,
        session_id="sess-reconnect",
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=5),
        max_events=2,
    )
    _run(monitor)
    samples = _samples(rolling)
    # after reset, the new epoch's single low-sequence trade is the only sample.
    assert len(samples) == 1
    assert samples[0].sequence == 1


class _RollingResetRaises(RollingTradeHistoryStore):
    def reset_stream(self, provider: str, channel: str) -> None:
        raise RuntimeError("rolling reset defect")


def test_rolling_reset_failure_raises_internal() -> None:
    rolling = _RollingResetRaises(retention=_retention())
    store = LatestMarketStateStore()
    sources = iter(
        [
            _DropAfterFirst(_trade(sequence=5)),
            ReplayMarketEventSource([_trade(sequence=1)]),
        ]
    )

    async def _sleep(seconds: float) -> None:
        pass

    monitor = MarketMonitor(
        store=store,
        rolling_store=rolling,
        source_factory=lambda: next(sources),
        clock=_late_clock(),
        sleep=_sleep,
        session_id="sess-reset-fail",
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=5),
        max_events=2,
    )
    with pytest.raises(MonitorInternalError, match="rolling_store.reset_stream failed"):
        _run(monitor)


# --------------------------------------------------------------------------- #
# source dies without an event → neither store reset (epoch metadata preserved)
# --------------------------------------------------------------------------- #
class _DieImmediately:
    async def events(self) -> AsyncIterator[MarketEvent]:
        raise RuntimeError("dead on connect")
        yield  # pragma: no cover - unreachable


def test_dead_source_does_not_reset_rolling() -> None:
    rolling = RollingTradeHistoryStore(retention=_retention())
    store = LatestMarketStateStore()
    # attempt 1 applies one trade; attempt 2 dies before any event; attempt 3 EOF.
    sources = iter(
        [
            _DropAfterFirst(_trade(sequence=1)),
            _DieImmediately(),
            ReplayMarketEventSource([]),
        ]
    )

    async def _sleep(seconds: float) -> None:
        pass

    monitor = MarketMonitor(
        store=store,
        rolling_store=rolling,
        source_factory=lambda: next(sources),
        clock=_late_clock(),
        sleep=_sleep,
        session_id="sess-dead",
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=5),
    )
    _run(monitor)
    # the dead reconnect never delivered an event, so the seq=1 sample survives.
    samples = _samples(rolling)
    assert len(samples) == 1
    assert samples[0].sequence == 1
