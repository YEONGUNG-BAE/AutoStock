"""RTM-7b.4 — full-day long-running supervisor integration."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from market_data.health_policy import HealthThresholds, MarketHealthTracker
from market_data.market_session import SessionWindow, build_explicit_schedule
from market_data.monitor import MonitorState, MonitorSummary
from market_data.supervisor import MarketSupervisor, SupervisorAction, SupervisorPolicy, SupervisorState

from domain.enums import Market

_KST = ZoneInfo("Asia/Seoul")
_DAY = date(2026, 6, 15)
_WINDOW = SessionWindow(
    pre_open=time(8, 30), open=time(9, 0), close=time(15, 30), post_close_end=time(16, 0)
)


def _at(h: int, mi: int, s: int = 0) -> datetime:
    return datetime(2026, 6, 15, h, mi, s, tzinfo=_KST)


class _LongRunningMonitor:
    """장중 유지되는 scripted monitor — cancel/cleanup 카운트 기록."""

    instances: list["_LongRunningMonitor"] = []
    total_cancels = 0
    total_cleanups = 0
    exit_on_tick: int | None = None  # 해당 인스턴스 번호에서 즉시 종료

    def __init__(self) -> None:
        self.index = len(_LongRunningMonitor.instances)
        _LongRunningMonitor.instances.append(self)
        self.cancelled = False
        self.cleaned_up = False
        self.state = MonitorState.IDLE

    async def run(self) -> MonitorSummary:
        self.state = MonitorState.RUNNING
        if _LongRunningMonitor.exit_on_tick == self.index:
            self.state = MonitorState.STOPPED
            return MonitorSummary(
                monitor_session_id=f"lr-{self.index}",
                connection_attempts=1,
                consecutive_failures=0,
                applied=0,
                duplicate=0,
                out_of_order=0,
                stream_mismatch=0,
                future_event_error=0,
                final_state=MonitorState.STOPPED,
            )
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            self.cleaned_up = True
            _LongRunningMonitor.total_cancels += 1
            _LongRunningMonitor.total_cleanups += 1
            self.state = MonitorState.STOPPED
            raise
        return _summary_placeholder()


def _summary_placeholder() -> MonitorSummary:
    return MonitorSummary(
        monitor_session_id="lr",
        connection_attempts=1,
        consecutive_failures=0,
        applied=0,
        duplicate=0,
        out_of_order=0,
        stream_mismatch=0,
        future_event_error=0,
        final_state=MonitorState.STOPPED,
    )


def test_full_day_long_running_exact_counts() -> None:
    _LongRunningMonitor.instances.clear()
    _LongRunningMonitor.total_cancels = 0
    _LongRunningMonitor.total_cleanups = 0
    _LongRunningMonitor.exit_on_tick = None

    schedule = build_explicit_schedule(timezone=_KST, trading_days=[_DAY], window=_WINDOW)
    thr = HealthThresholds(
        subscription_grace_seconds=60.0,
        heartbeat_timeout_seconds=300.0,
        minimum_stable_uptime_seconds=1.0,
        flapping_window_seconds=600.0,
        flapping_max_short_epochs=5,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=30.0,
        max_quote_age_seconds=60.0,
    )
    tracker = MarketHealthTracker(thr)

    timeline: list[tuple[datetime, list[str], list[str]]] = [
        (_at(8, 50), [], []),
        (_at(9, 0), ["connected"], []),
        (_at(9, 0, 30), [], []),
        (_at(9, 1), ["all_subscribed", "pong_sent"], []),
        (_at(9, 5), [], ["best_bid_ask"]),
        (_at(10, 0), ["disconnect"], []),
        (_at(10, 0, 2), ["connected", "all_subscribed", "pong_sent"], []),
        (_at(11, 0), [], ["heartbeat"]),
        (_at(12, 0), [], ["best_bid_ask"]),
        (_at(15, 30), [], []),
        (_at(18, 0), [], []),
    ]
    tick = {"i": 0}
    actions: list[str] = []

    def clock() -> datetime:
        idx = min(tick["i"], len(timeline) - 1)
        at, transports, markets = timeline[idx]
        for kind in transports:
            tracker.record_transport_event(kind=kind, at=at, now=at)
        for et in markets:
            tracker.record_market_event(event_type=et, at=at, now=at)
        return at

    async def sleep(_s: float) -> None:
        tick["i"] += 1
        await asyncio.sleep(0)

    policy = SupervisorPolicy(
        poll_interval_seconds=0.01,
        max_restarts_in_window=2,
        restart_window_seconds=600.0,
        restart_backoff_seconds=0.0,
    )

    sup = MarketSupervisor(
        market=Market.KR,
        calendar=schedule,
        monitor_factory=_LongRunningMonitor,
        tracker=tracker,
        clock=clock,
        sleep=sleep,
        policy=policy,
        max_ticks=len(timeline),
        on_evidence=lambda ev: actions.append(ev.action),
    )

    async def _run() -> None:
        summary = await sup.run()
        pending = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        assert summary.monitor_initial_starts == 1
        assert summary.monitor_restarts == 1
        assert str(SupervisorAction.HOLD_EXECUTION_ONLY) in actions
        # transport restart(10:00) + POST_CLOSE(15:30) 각각 cancel 1회
        assert _LongRunningMonitor.total_cancels == 2
        assert _LongRunningMonitor.total_cleanups == 2
        assert summary.monitor_cancels == 2
        assert summary.final_state is SupervisorState.STOPPED
        assert pending == []

    asyncio.run(_run())
