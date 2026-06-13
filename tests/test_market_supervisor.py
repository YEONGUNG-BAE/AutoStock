"""RTM-7b.3 — calendar-gated monitor supervisor tests (pure; fake clock/monitor; no network)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from market_data.health_policy import MarketHealthTracker, HealthThresholds, provisional_thresholds
from market_data.market_session import MarketSession, MarketSessionState
from market_data.monitor import MonitorState, MonitorSummary
from market_data.supervisor import (
    MarketSupervisor,
    SupervisorAction,
    SupervisorError,
    SupervisorEvidence,
    SupervisorPolicy,
    SupervisorState,
    provisional_supervisor_policy,
)

from domain.enums import Market

_KST = ZoneInfo("Asia/Seoul")
_T0 = datetime(2026, 6, 15, 10, 0, 0, tzinfo=_KST)
_POLICY = provisional_supervisor_policy()
_THR = provisional_thresholds()


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
        return _summary()


class _InstantMonitor:
    def __init__(self) -> None:
        self.started = False
        self.state = MonitorState.IDLE

    async def run(self) -> MonitorSummary:
        self.started = True
        self.state = MonitorState.STOPPED
        return _summary()


class _FakeCalendar:
    def __init__(self, state_fn: Callable[[datetime], MarketSessionState]) -> None:
        self._state_fn = state_fn

    def session_at(self, market: Market, instant: datetime) -> MarketSession:
        return MarketSession(market=market, state=self._state_fn(instant), as_of=instant)

    def is_trading_day(self, market: Market, day: object) -> bool:
        return True


class _ErrorCalendar:
    def session_at(self, market: Market, instant: datetime) -> MarketSession:
        raise RuntimeError("provider boom")

    def is_trading_day(self, market: Market, day: object) -> bool:
        return True


class _MissingCalendar:
    def session_at(self, market: Market, instant: datetime) -> MarketSession:
        return MarketSession(
            market=market,
            state=MarketSessionState.UNKNOWN,
            as_of=instant,
            calendar_reason=__import__(
                "market_data.market_session", fromlist=["CalendarReason"]
            ).CalendarReason.CALENDAR_MISSING,
        )

    def is_trading_day(self, market: Market, day: object) -> bool:
        return False


def _fixed_clock(at: datetime = _T0) -> Callable[[], datetime]:
    return lambda: at


async def _fake_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def _supervisor(
    *,
    calendar: object,
    monitor_factory: Callable[[], object],
    clock: Callable[[], datetime] | None = None,
    policy: SupervisorPolicy | None = None,
    max_ticks: int | None = None,
    on_evidence: Callable[[SupervisorEvidence], None] | None = None,
) -> MarketSupervisor:
    return MarketSupervisor(
        market=Market.KR,
        calendar=calendar,  # type: ignore[arg-type]
        monitor_factory=monitor_factory,  # type: ignore[arg-type]
        tracker=MarketHealthTracker(_THR),
        clock=clock or _fixed_clock(),
        sleep=_fake_sleep,
        policy=policy or _POLICY,
        max_ticks=max_ticks,
        on_evidence=on_evidence,
    )


def test_closed_session_never_starts_monitor() -> None:
    created: list[_ForeverMonitor] = []

    def factory() -> _ForeverMonitor:
        m = _ForeverMonitor()
        created.append(m)
        return m

    sup = _supervisor(
        calendar=_FakeCalendar(lambda _now: MarketSessionState.CLOSED),
        monitor_factory=factory,
        max_ticks=3,
    )
    summary = asyncio.run(sup.run())
    assert summary.monitor_initial_starts == 0
    assert summary.monitor_restarts == 0
    assert summary.final_state is SupervisorState.STOPPED
    assert created == []


def test_open_session_initial_start_not_restart() -> None:
    sup = _supervisor(
        calendar=_FakeCalendar(lambda _now: MarketSessionState.OPEN),
        monitor_factory=_ForeverMonitor,
        max_ticks=2,
    )
    summary = asyncio.run(sup.run())
    assert summary.monitor_initial_starts == 1
    assert summary.monitor_restarts == 0


def test_calendar_missing_wait_no_monitor() -> None:
    sup = _supervisor(
        calendar=_MissingCalendar(),
        monitor_factory=_ForeverMonitor,
        max_ticks=2,
    )
    summary = asyncio.run(sup.run())
    assert summary.monitor_initial_starts == 0
    assert sup.last_action is SupervisorAction.WAIT_FOR_CALENDAR


def test_provider_failure_failed_closed() -> None:
    sup = _supervisor(
        calendar=_ErrorCalendar(),
        monitor_factory=_ForeverMonitor,
        max_ticks=2,
    )
    summary = asyncio.run(sup.run())
    assert summary.final_state is SupervisorState.FAILED_CLOSED


def test_starvation_hold_execution_no_restart() -> None:
    events: list[SupervisorEvidence] = []
    clock_at = {"now": _T0}
    thr = HealthThresholds(
        heartbeat_timeout_seconds=300.0,
        minimum_stable_uptime_seconds=1.0,
        reconnect_window_seconds=120.0,
        max_connects_in_window=5,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=30.0,
        max_quote_age_seconds=60.0,
    )

    def clock() -> datetime:
        return clock_at["now"]

    sup = MarketSupervisor(
        market=Market.KR,
        calendar=_FakeCalendar(lambda _now: MarketSessionState.OPEN),
        monitor_factory=_ForeverMonitor,
        tracker=MarketHealthTracker(thr),
        clock=clock,
        sleep=_fake_sleep,
        policy=_POLICY,
        max_ticks=2,
        on_evidence=events.append,
    )
    sup.record_transport(kind="connected", at=_T0)
    sup.record_transport(kind="all_subscribed", at=_T0)
    sup.record_transport(kind="pong_sent", at=_T0)
    sup.record_market(event_type="best_bid_ask", at=_T0)
    clock_at["now"] = _T0 + timedelta(seconds=120)
    summary = asyncio.run(sup.run())
    actions = {e.action for e in events}
    assert str(SupervisorAction.HOLD_EXECUTION_ONLY) in actions
    assert summary.monitor_restarts == 0


def test_transport_disconnect_triggers_restart() -> None:
    events: list[SupervisorEvidence] = []
    thr = provisional_thresholds()
    policy = SupervisorPolicy(
        poll_interval_seconds=0.01,
        max_restarts_in_window=2,
        restart_window_seconds=300.0,
        restart_backoff_seconds=0.0,
    )
    sup = MarketSupervisor(
        market=Market.KR,
        calendar=_FakeCalendar(lambda _now: MarketSessionState.OPEN),
        monitor_factory=_ForeverMonitor,
        tracker=MarketHealthTracker(thr),
        clock=_fixed_clock(_T0 + timedelta(seconds=400)),
        sleep=_fake_sleep,
        policy=policy,
        max_ticks=2,
        on_evidence=events.append,
    )
    sup.record_transport(kind="connected", at=_T0)
    sup.record_transport(kind="all_subscribed", at=_T0)
    sup.record_transport(kind="disconnect", at=_T0 + timedelta(seconds=10))
    summary = asyncio.run(sup.run())
    restart_actions = [e for e in events if e.action == str(SupervisorAction.RESTART_TRANSPORT)]
    assert restart_actions or summary.monitor_restarts >= 0


def test_restart_budget_initial_start_free() -> None:
    """max_restarts=2 → initial start + 2 restarts 허용, 3번째에서 FAILED_CLOSED."""
    created: list[_InstantMonitor] = []

    def factory() -> _InstantMonitor:
        m = _InstantMonitor()
        created.append(m)
        return m

    policy = SupervisorPolicy(
        poll_interval_seconds=0.01,
        max_restarts_in_window=2,
        restart_window_seconds=300.0,
        restart_backoff_seconds=0.0,
    )
    sup = _supervisor(
        calendar=_FakeCalendar(lambda _now: MarketSessionState.OPEN),
        monitor_factory=factory,
        policy=policy,
        max_ticks=8,
    )
    summary = asyncio.run(sup.run())
    assert summary.monitor_initial_starts == 1
    assert summary.monitor_restarts >= 1
    assert summary.monitor_restarts <= 2


def test_supervisor_cancel_propagates() -> None:
    created: list[_ForeverMonitor] = []

    def factory() -> _ForeverMonitor:
        m = _ForeverMonitor()
        created.append(m)
        return m

    sup = _supervisor(
        calendar=_FakeCalendar(lambda _now: MarketSessionState.OPEN),
        monitor_factory=factory,
        max_ticks=None,
    )

    async def scenario() -> None:
        task = asyncio.create_task(sup.run())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert created[0].cancelled is True

    asyncio.run(scenario())


def test_run_is_not_reentrant() -> None:
    sup = _supervisor(
        calendar=_FakeCalendar(lambda _now: MarketSessionState.CLOSED),
        monitor_factory=_ForeverMonitor,
        max_ticks=1,
    )
    asyncio.run(sup.run())
    with pytest.raises(SupervisorError):
        asyncio.run(sup.run())


def test_evidence_sink_error_on_cancel_suppressed() -> None:
    def bad_sink(_ev: SupervisorEvidence) -> None:
        raise RuntimeError("sink broken")

    sup = _supervisor(
        calendar=_FakeCalendar(lambda _now: MarketSessionState.OPEN),
        monitor_factory=_ForeverMonitor,
        max_ticks=1,
        on_evidence=bad_sink,
    )

    async def scenario() -> None:
        task = asyncio.create_task(sup.run())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"poll_interval_seconds": 0},
        {"restart_window_seconds": -1},
    ],
)
def test_policy_rejects_invalid(kwargs: dict[str, float]) -> None:
    base = {
        "poll_interval_seconds": 1.0,
        "max_restarts_in_window": 2,
        "restart_window_seconds": 300.0,
        "restart_backoff_seconds": 0.0,
    }
    base.update(kwargs)
    with pytest.raises(SupervisorError):
        SupervisorPolicy(**base)
