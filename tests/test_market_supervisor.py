"""RTM-7b.3 — calendar-gated monitor supervisor tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from market_data.health_policy import HealthThresholds, MarketHealthTracker, provisional_thresholds
from market_data.market_session import MarketSession, MarketSessionState
from market_data.monitor import MonitorExhaustedError, MonitorState, MonitorSummary
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
_THR = provisional_thresholds()
_POLICY = provisional_supervisor_policy()


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
        self.cancelled = False
        self.state = MonitorState.IDLE

    async def run(self) -> MonitorSummary:
        self.state = MonitorState.RUNNING
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            self.state = MonitorState.STOPPED
            raise
        return _summary()


class _InstantMonitor:
    async def run(self) -> MonitorSummary:
        return _summary()


class _FailFactory:
    def __call__(self) -> _ForeverMonitor:
        raise RuntimeError("factory boom")


class _FakeCalendar:
    def __init__(self, state_fn: Callable[[datetime], MarketSessionState]) -> None:
        self._state_fn = state_fn

    def session_at(self, market: Market, instant: datetime) -> MarketSession:
        return MarketSession(market=market, state=self._state_fn(instant), as_of=instant)

    def is_trading_day(self, market: Market, day: object) -> bool:
        return True


class _MissingCalendar:
    def session_at(self, market: Market, instant: datetime) -> MarketSession:
        from market_data.market_session import CalendarReason

        return MarketSession(
            market=market,
            state=MarketSessionState.UNKNOWN,
            as_of=instant,
            calendar_reason=CalendarReason.CALENDAR_MISSING,
        )

    def is_trading_day(self, market: Market, day: object) -> bool:
        return False


async def _fake_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


def _supervisor(**kwargs: object) -> MarketSupervisor:
    defaults: dict[str, object] = {
        "market": Market.KR,
        "calendar": _FakeCalendar(lambda _n: MarketSessionState.OPEN),
        "monitor_factory": _ForeverMonitor,
        "tracker": MarketHealthTracker(_THR),
        "clock": lambda: _T0,
        "sleep": _fake_sleep,
        "policy": _POLICY,
    }
    defaults.update(kwargs)
    return MarketSupervisor(**defaults)  # type: ignore[arg-type]


def test_closed_never_starts_monitor() -> None:
    sup = _supervisor(calendar=_FakeCalendar(lambda _n: MarketSessionState.CLOSED), max_ticks=2)
    summary = asyncio.run(sup.run())
    assert summary.monitor_initial_starts == 0


def test_initial_start_free_from_restart_budget() -> None:
    sup = _supervisor(max_ticks=2)
    summary = asyncio.run(sup.run())
    assert summary.monitor_initial_starts == 1
    assert summary.monitor_restarts == 0
    assert summary.restarts_in_current_window == 0


def test_starvation_hold_no_restart() -> None:
    events: list[SupervisorEvidence] = []
    clock_at = {"now": _T0}
    thr = HealthThresholds(
        subscription_grace_seconds=30.0,
        heartbeat_timeout_seconds=300.0,
        minimum_stable_uptime_seconds=1.0,
        flapping_window_seconds=120.0,
        flapping_max_short_epochs=5,
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
        calendar=_FakeCalendar(lambda _n: MarketSessionState.OPEN),
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
    assert str(SupervisorAction.HOLD_EXECUTION_ONLY) in {e.action for e in events}
    assert summary.monitor_restarts == 0


def test_starved_after_monitor_exit_restarts_for_transport_absence() -> None:
    # STARVED 자체로 restart하지 않지만, monitor가 자연 종료(transport session absent)하면
    # HOLD 상태에서도 transport 원인으로 restart해야 한다. (live-monitor no-restart의 짝)
    events: list[SupervisorEvidence] = []
    clock_at = {"now": _T0}
    thr = HealthThresholds(
        subscription_grace_seconds=30.0,
        heartbeat_timeout_seconds=300.0,
        minimum_stable_uptime_seconds=1.0,
        flapping_window_seconds=120.0,
        flapping_max_short_epochs=5,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=30.0,
        max_quote_age_seconds=300.0,
    )

    def clock() -> datetime:
        return clock_at["now"]

    policy = SupervisorPolicy(
        poll_interval_seconds=0.01,
        max_restarts_in_window=5,
        restart_window_seconds=300.0,
        restart_backoff_seconds=0.0,
    )
    tracker = MarketHealthTracker(thr)
    sup = MarketSupervisor(
        market=Market.KR,
        calendar=_FakeCalendar(lambda _n: MarketSessionState.OPEN),
        monitor_factory=_InstantMonitor,
        tracker=tracker,
        clock=clock,
        sleep=_fake_sleep,
        policy=policy,
        max_ticks=3,
        on_evidence=events.append,
    )
    sup.record_transport(kind="connected", at=_T0)
    sup.record_transport(kind="all_subscribed", at=_T0)
    sup.record_transport(kind="pong_sent", at=_T0)
    sup.record_market(event_type="best_bid_ask", at=_T0)
    clock_at["now"] = _T0 + timedelta(seconds=120)  # quote starvation (>30, <max 300) → STARVED
    summary = asyncio.run(sup.run())
    actions = {e.action for e in events}
    assert str(SupervisorAction.HOLD_EXECUTION_ONLY) in actions
    assert summary.monitor_initial_starts == 1
    assert summary.monitor_restarts >= 1
    # restart 사유는 starvation이 아니라 transport session absent여야 한다.
    assert any(e.reason_code == "transport_session_absent" for e in events)


def test_restart_budget_window_not_total() -> None:
    created: list[_InstantMonitor] = []

    def factory() -> _InstantMonitor:
        created.append(_InstantMonitor())
        return created[-1]

    policy = SupervisorPolicy(
        poll_interval_seconds=0.01,
        max_restarts_in_window=2,
        restart_window_seconds=300.0,
        restart_backoff_seconds=0.0,
    )
    sup = _supervisor(monitor_factory=factory, policy=policy, max_ticks=10)
    summary = asyncio.run(sup.run())
    assert summary.monitor_initial_starts == 1
    assert summary.monitor_restarts <= 2
    assert summary.monitor_restarts == summary.restarts_in_current_window


def test_factory_failure_sticky_failed_closed() -> None:
    sup = _supervisor(monitor_factory=_FailFactory(), max_ticks=5)
    summary = asyncio.run(sup.run())
    assert summary.final_state is SupervisorState.FAILED_CLOSED
    assert summary.ticks == 1


def test_evidence_sink_failure_sticky() -> None:
    calls = {"n": 0}

    def bad_sink(_ev: SupervisorEvidence) -> None:
        calls["n"] += 1
        raise RuntimeError("sink broken")

    sup = _supervisor(max_ticks=3, on_evidence=bad_sink)
    summary = asyncio.run(sup.run())
    assert summary.final_state is SupervisorState.FAILED_CLOSED
    assert calls["n"] == 1


def test_evidence_sink_on_cancel_suppressed() -> None:
    events: list[SupervisorEvidence] = []

    def good_sink(ev: SupervisorEvidence) -> None:
        events.append(ev)

    sup = _supervisor(max_ticks=None, on_evidence=good_sink)

    async def scenario() -> None:
        task = asyncio.create_task(sup.run())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_calendar_missing_wait() -> None:
    sup = _supervisor(calendar=_MissingCalendar(), max_ticks=2)
    summary = asyncio.run(sup.run())
    assert summary.monitor_initial_starts == 0
    assert sup.last_action is SupervisorAction.WAIT_FOR_CALENDAR
