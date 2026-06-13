"""RTM-7b.4 — full-day supervisor integration (real orchestration; no network)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from market_data.health_policy import (
    MarketDataHealthStatus,
    MarketHealthTracker,
    TransportHealthStatus,
    provisional_thresholds,
)
from market_data.market_session import (
    MarketSessionState,
    SessionWindow,
    build_explicit_schedule,
)
from market_data.monitor import MonitorState, MonitorSummary
from market_data.supervisor import (
    MarketSupervisor,
    SupervisorAction,
    SupervisorEvidence,
    SupervisorPolicy,
    SupervisorState,
)

from domain.enums import Market

_KST = ZoneInfo("Asia/Seoul")
_DAY = date(2026, 6, 15)
_WINDOW = SessionWindow(
    pre_open=time(8, 30), open=time(9, 0), close=time(15, 30), post_close_end=time(16, 0)
)


def _at(h: int, mi: int, s: int = 0) -> datetime:
    return datetime(2026, 6, 15, h, mi, s, tzinfo=_KST)


class _ScriptedMonitor:
    """supervisor가 기동할 때마다 즉시 STOPPED로 종료하는 scripted runner."""

    instances: list["_ScriptedMonitor"] = []

    def __init__(self) -> None:
        self.cancelled = False
        self.state = MonitorState.IDLE
        _ScriptedMonitor.instances.append(self)

    async def run(self) -> MonitorSummary:
        self.state = MonitorState.RUNNING
        await asyncio.sleep(0)
        self.state = MonitorState.STOPPED
        return MonitorSummary(
            monitor_session_id="day-sim",
            connection_attempts=1,
            consecutive_failures=0,
            applied=0,
            duplicate=0,
            out_of_order=0,
            stream_mismatch=0,
            future_event_error=0,
            final_state=MonitorState.STOPPED,
        )


def _run_full_day() -> dict[str, object]:
    """08:50 PRE_OPEN → ... → 18:00 CLOSED 시나리오."""
    _ScriptedMonitor.instances.clear()
    schedule = build_explicit_schedule(timezone=_KST, trading_days=[_DAY], window=_WINDOW)

    # 타임라인: (clock, transport signals, market signals)
    timeline: list[tuple[datetime, list[str], list[str]]] = [
        (_at(8, 50), [], []),
        (_at(9, 0), ["connected", "all_subscribed", "pong_sent"], []),
        (_at(9, 0, 10), [], []),
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
    events: list[SupervisorEvidence] = []
    thr = provisional_thresholds()
    # full-day 테스트: warming 구간을 짧게.
    from market_data.health_policy import HealthThresholds

    thr = HealthThresholds(
        heartbeat_timeout_seconds=300.0,
        minimum_stable_uptime_seconds=1.0,
        reconnect_window_seconds=600.0,
        max_connects_in_window=5,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=30.0,
        max_quote_age_seconds=60.0,
    )
    tracker = MarketHealthTracker(thr)

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

    def on_evidence(ev: SupervisorEvidence) -> None:
        events.append(ev)
        actions.append(ev.action)

    sup = MarketSupervisor(
        market=Market.KR,
        calendar=schedule,
        monitor_factory=_ScriptedMonitor,
        tracker=tracker,
        clock=clock,
        sleep=sleep,
        policy=policy,
        max_ticks=len(timeline),
        on_evidence=on_evidence,
    )

    summary = asyncio.run(sup.run())

    return {
        "summary": summary,
        "actions": actions,
        "events": events,
        "monitors": list(_ScriptedMonitor.instances),
        "tracker": tracker,
        "schedule": schedule,
    }


def test_full_day_supervisor_orchestration() -> None:
    result = _run_full_day()
    summary = result["summary"]
    actions: list[str] = result["actions"]  # type: ignore[assignment]
    monitors: list[_ScriptedMonitor] = result["monitors"]  # type: ignore[assignment]
    tracker: MarketHealthTracker = result["tracker"]  # type: ignore[assignment]
    schedule = result["schedule"]

    # OPEN 구간에서 initial start ≥ 1.
    assert summary.monitor_initial_starts >= 1
    # HOLD_EXECUTION_ONLY 가 시나리오에 포함되어야 한다.
    assert str(SupervisorAction.HOLD_EXECUTION_ONLY) in actions
    assert summary.monitor_initial_starts >= 1

    # 18:00 CLOSED — no new starts expected at end.
    assert summary.final_state in (SupervisorState.STOPPED, SupervisorState.IDLE_CLOSED)


def test_health_only_day_replay_still_passes() -> None:
    """기존 health-only timeline 회귀."""
    from market_data.market_session import FixtureMarketCalendar

    cal = FixtureMarketCalendar.for_krx()
    thr = provisional_thresholds()
    from market_data.health_policy import HealthThresholds

    thr = HealthThresholds(
        heartbeat_timeout_seconds=300.0,
        minimum_stable_uptime_seconds=1.0,
        reconnect_window_seconds=120.0,
        max_connects_in_window=3,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=30.0,
        max_quote_age_seconds=60.0,
    )
    tracker = MarketHealthTracker(thr)
    tracker.record_transport_event(kind="connected", at=_at(9, 0), now=_at(9, 0))
    tracker.record_transport_event(kind="all_subscribed", at=_at(9, 0), now=_at(9, 0))
    tracker.record_market_event(event_type="best_bid_ask", at=_at(9, 15), now=_at(9, 15))
    v = tracker.evaluate(session=cal.session_at(Market.KR, _at(9, 15, 1)), now=_at(9, 15, 1))
    assert v.market_data is MarketDataHealthStatus.HEALTHY
