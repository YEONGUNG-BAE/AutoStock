"""RTM-7b.3 — calendar-gated monitor supervisor tests (pure; fake clock/monitor; no network)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from market_data.health_policy import MarketHealthTracker
from market_data.market_session import MarketSession, MarketSessionState
from market_data.monitor import MonitorState, MonitorSummary
from market_data.supervisor import (
    MarketSupervisor,
    SupervisorError,
    SupervisorEvidence,
    SupervisorPolicy,
    SupervisorState,
)

from domain.enums import Market

_KST = ZoneInfo("Asia/Seoul")
_T0 = datetime(2026, 6, 15, 10, 0, 0, tzinfo=_KST)


def _summary() -> MonitorSummary:
    return MonitorSummary(
        monitor_session_id="fake",
        connection_attempts=1,
        consecutive_failures=0,
        applied=0,
        duplicate=0,
        out_of_order=0,
        stream_mismatch=0,
        future_event_error=0,
        final_state=MonitorState.STOPPED,
    )


class _ForeverMonitor:
    """취소될 때까지 영원히 도는 monitor. cancel 전파를 기록한다."""

    def __init__(self) -> None:
        self.started = False
        self.cancelled = False
        self.state = MonitorState.IDLE

    async def run(self) -> MonitorSummary:
        self.started = True
        self.state = MonitorState.RUNNING
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            self.state = MonitorState.STOPPED
            raise
        return _summary()  # pragma: no cover - 도달하지 않음


class _InstantMonitor:
    """즉시 자체 종료(self-exit)하는 monitor. 반복 재기동 budget 검증용."""

    def __init__(self) -> None:
        self.started = False
        self.state = MonitorState.IDLE

    async def run(self) -> MonitorSummary:
        self.started = True
        self.state = MonitorState.STOPPED
        return _summary()


class _FakeCalendar:
    """now별 세션 상태를 함수로 결정하는 fixture 캘린더."""

    def __init__(self, state_fn: Callable[[datetime], MarketSessionState]) -> None:
        self._state_fn = state_fn

    def session_at(self, market: Market, instant: datetime) -> MarketSession:
        return MarketSession(market=market, state=self._state_fn(instant), as_of=instant)

    def is_trading_day(self, market: Market, day: object) -> bool:  # pragma: no cover
        return True


def _fixed_clock(at: datetime = _T0) -> Callable[[], datetime]:
    return lambda: at


def _advancing_clock(start: datetime = _T0, step_seconds: float = 1.0) -> Callable[[], datetime]:
    state = {"now": start}

    def _clock() -> datetime:
        now = state["now"]
        state["now"] = now + timedelta(seconds=step_seconds)
        return now

    return _clock


async def _fake_sleep(_seconds: float) -> None:
    # 실제 대기 없이 yield만 한다 — create_task된 monitor가 진행/취소될 기회를 준다.
    await asyncio.sleep(0)


def _supervisor(
    *,
    state_fn: Callable[[datetime], MarketSessionState],
    monitor_factory: Callable[[], object],
    clock: Callable[[], datetime] | None = None,
    policy: SupervisorPolicy | None = None,
    max_ticks: int | None = None,
    on_evidence: Callable[[SupervisorEvidence], None] | None = None,
) -> MarketSupervisor:
    return MarketSupervisor(
        market=Market.KR,
        calendar=_FakeCalendar(state_fn),
        monitor_factory=monitor_factory,  # type: ignore[arg-type]
        tracker=MarketHealthTracker(),
        clock=clock or _fixed_clock(),
        sleep=_fake_sleep,
        policy=policy or SupervisorPolicy(),
        max_ticks=max_ticks,
        on_evidence=on_evidence,
    )


# --- session gating -----------------------------------------------------------


def test_closed_session_never_starts_monitor() -> None:
    created: list[_ForeverMonitor] = []

    def factory() -> _ForeverMonitor:
        m = _ForeverMonitor()
        created.append(m)
        return m

    sup = _supervisor(
        state_fn=lambda _now: MarketSessionState.CLOSED,
        monitor_factory=factory,
        max_ticks=3,
    )
    summary = asyncio.run(sup.run())
    assert summary.monitor_starts == 0
    assert summary.final_state is SupervisorState.STOPPED
    assert created == []


@pytest.mark.parametrize(
    "state",
    [MarketSessionState.PRE_OPEN, MarketSessionState.POST_CLOSE, MarketSessionState.CLOSED],
)
def test_non_open_states_are_inactive(state: MarketSessionState) -> None:
    sup = _supervisor(
        state_fn=lambda _now: state,
        monitor_factory=_ForeverMonitor,
        max_ticks=2,
    )
    summary = asyncio.run(sup.run())
    assert summary.monitor_starts == 0


def test_open_session_starts_single_monitor() -> None:
    created: list[_ForeverMonitor] = []

    def factory() -> _ForeverMonitor:
        m = _ForeverMonitor()
        created.append(m)
        return m

    sup = _supervisor(
        state_fn=lambda _now: MarketSessionState.OPEN,
        monitor_factory=factory,
        max_ticks=3,
    )
    summary = asyncio.run(sup.run())
    # 단일 monitor만 가동되고(매 틱 새로 만들지 않음), 종료 시 graceful cancel된다.
    assert summary.monitor_starts == 1
    assert len(created) == 1
    assert created[0].started is True
    assert created[0].cancelled is True  # graceful shutdown에서 취소됨
    assert summary.final_state is SupervisorState.STOPPED


def test_open_then_closed_gracefully_cancels_monitor() -> None:
    created: list[_ForeverMonitor] = []
    ticks = {"n": 0}

    def state_fn(_now: datetime) -> MarketSessionState:
        ticks["n"] += 1
        return MarketSessionState.OPEN if ticks["n"] <= 2 else MarketSessionState.CLOSED

    def factory() -> _ForeverMonitor:
        m = _ForeverMonitor()
        created.append(m)
        return m

    sup = _supervisor(state_fn=state_fn, monitor_factory=factory, max_ticks=4)
    summary = asyncio.run(sup.run())
    assert len(created) == 1
    assert created[0].cancelled is True  # 세션이 닫히며 graceful cancel, leak 없음
    assert summary.monitor_starts == 1


# --- restart budget -----------------------------------------------------------


def test_repeated_self_exit_exhausts_restart_budget_fails_closed() -> None:
    created: list[_InstantMonitor] = []

    def factory() -> _InstantMonitor:
        m = _InstantMonitor()
        created.append(m)
        return m

    sup = _supervisor(
        state_fn=lambda _now: MarketSessionState.OPEN,
        monitor_factory=factory,
        clock=_fixed_clock(),  # 모든 재기동이 한 window 안에 머문다
        policy=SupervisorPolicy(max_restarts_in_window=3, restart_window_seconds=300.0),
        max_ticks=50,
    )
    summary = asyncio.run(sup.run())
    assert summary.final_state is SupervisorState.FAILED_CLOSED
    # budget(3)만큼만 재기동하고 더는 만들지 않는다(무한 restart 금지).
    assert summary.monitor_starts == 3
    assert len(created) == 3


def test_self_exits_within_budget_keep_restarting() -> None:
    created: list[_InstantMonitor] = []

    def factory() -> _InstantMonitor:
        m = _InstantMonitor()
        created.append(m)
        return m

    sup = _supervisor(
        state_fn=lambda _now: MarketSessionState.OPEN,
        monitor_factory=factory,
        clock=_fixed_clock(),
        policy=SupervisorPolicy(max_restarts_in_window=5),
        max_ticks=3,  # budget 소진 전에 멈춘다
    )
    summary = asyncio.run(sup.run())
    assert summary.final_state is SupervisorState.STOPPED
    assert summary.monitor_starts >= 1
    assert summary.monitor_starts <= 5


# --- cancellation propagation -------------------------------------------------


def test_supervisor_cancel_propagates_and_cancels_monitor() -> None:
    created: list[_ForeverMonitor] = []

    def factory() -> _ForeverMonitor:
        m = _ForeverMonitor()
        created.append(m)
        return m

    sup = _supervisor(
        state_fn=lambda _now: MarketSessionState.OPEN,
        monitor_factory=factory,
        max_ticks=None,  # 외부 cancel로만 종료
    )

    async def scenario() -> None:
        task = asyncio.create_task(sup.run())
        # supervisor가 monitor를 가동할 때까지 yield.
        for _ in range(5):
            await asyncio.sleep(0)
        assert created and created[0].started is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # 구조적 취소: 가동 중 monitor도 취소되고 leak이 없다.
        assert created[0].cancelled is True
        assert sup.state is SupervisorState.STOPPED

    asyncio.run(scenario())


def test_run_is_not_reentrant() -> None:
    sup = _supervisor(
        state_fn=lambda _now: MarketSessionState.CLOSED,
        monitor_factory=_ForeverMonitor,
        max_ticks=1,
    )
    asyncio.run(sup.run())
    with pytest.raises(SupervisorError):
        asyncio.run(sup.run())


# --- health evidence ----------------------------------------------------------


def test_health_evidence_emitted_each_tick() -> None:
    events: list[SupervisorEvidence] = []
    sup = _supervisor(
        state_fn=lambda _now: MarketSessionState.OPEN,
        monitor_factory=_ForeverMonitor,
        max_ticks=2,
        on_evidence=events.append,
    )
    # 연결/구독/fresh quote를 주입하면 health_healthy가 한 번이라도 나와야 한다.
    sup.record_transport(kind="connected", at=_T0)
    sup.record_transport(kind="all_subscribed", at=_T0)
    sup.record_market(event_type="best_bid_ask", at=_T0)
    asyncio.run(sup.run())
    kinds = {e.kind for e in events}
    assert "health_healthy" in kinds
    health_events = [e for e in events if e.kind.startswith("health_")]
    assert health_events
    assert all(e.session_state == str(MarketSessionState.OPEN) for e in health_events)


def test_unhealthy_evidence_when_quote_starved() -> None:
    events: list[SupervisorEvidence] = []
    sup = _supervisor(
        state_fn=lambda _now: MarketSessionState.OPEN,
        monitor_factory=_ForeverMonitor,
        clock=_fixed_clock(_T0 + timedelta(seconds=120)),
        max_ticks=2,
        on_evidence=events.append,
    )
    sup.record_transport(kind="connected", at=_T0)
    sup.record_transport(kind="all_subscribed", at=_T0)
    sup.record_market(event_type="best_bid_ask", at=_T0)  # 120s 전 quote -> starvation
    asyncio.run(sup.run())
    kinds = {e.kind for e in events}
    assert "health_unhealthy" in kinds


# --- policy validation --------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"poll_interval_seconds": 0},
        {"max_restarts_in_window": 0},
        {"restart_window_seconds": -1},
        {"restart_backoff_seconds": -1},
    ],
)
def test_policy_rejects_invalid(kwargs: dict[str, float]) -> None:
    with pytest.raises(SupervisorError):
        SupervisorPolicy(**kwargs)


def test_max_ticks_must_be_positive() -> None:
    with pytest.raises(SupervisorError):
        _supervisor(
            state_fn=lambda _now: MarketSessionState.CLOSED,
            monitor_factory=_ForeverMonitor,
            max_ticks=0,
        )
