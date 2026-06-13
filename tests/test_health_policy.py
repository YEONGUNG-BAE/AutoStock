"""RTM-7b.2 — transport/market-data health policy tests (pure; injected now)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from market_data.health_policy import (
    HealthPolicyError,
    HealthThresholds,
    MarketDataHealthStatus,
    MarketHealthTracker,
    RecordResult,
    TransportHealthStatus,
    provisional_thresholds,
)
from market_data.market_session import MarketSession, MarketSessionState

from domain.enums import Market

_KST = ZoneInfo("Asia/Seoul")
_T0 = datetime(2026, 6, 15, 10, 0, 0, tzinfo=_KST)
_THR = provisional_thresholds()


def _session(state: MarketSessionState, at: datetime = _T0) -> MarketSession:
    return MarketSession(market=Market.KR, state=state, as_of=at)


def _tracker() -> MarketHealthTracker:
    return MarketHealthTracker(_THR)


def _connected_subscribed(tracker: MarketHealthTracker, at: datetime) -> None:
    tracker.record_transport_event(kind="connected", at=at, now=at)
    tracker.record_transport_event(kind="all_subscribed", at=at, now=at)
    tracker.record_transport_event(kind="pong_sent", at=at, now=at)


# --- timestamp validation -----------------------------------------------------


def test_naive_transport_event_rejected() -> None:
    tracker = _tracker()
    naive = datetime(2026, 6, 15, 10, 0)
    with pytest.raises(HealthPolicyError, match="timezone-aware"):
        tracker.record_transport_event(kind="connected", at=naive, now=_T0)


def test_naive_market_event_rejected() -> None:
    tracker = _tracker()
    naive = datetime(2026, 6, 15, 10, 0)
    with pytest.raises(HealthPolicyError, match="timezone-aware"):
        tracker.record_market_event(event_type="best_bid_ask", at=naive, now=_T0)


def test_future_quote_rejected_state_unchanged() -> None:
    tracker = _tracker()
    _connected_subscribed(tracker, _T0)
    future = _T0 + timedelta(hours=1)
    result = tracker.record_market_event(event_type="best_bid_ask", at=future, now=_T0)
    assert result is RecordResult.FUTURE
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN), now=_T0)
    assert verdict.market_data is MarketDataHealthStatus.WARMING


def test_future_pong_rejected() -> None:
    tracker = _tracker()
    result = tracker.record_transport_event(
        kind="pong_sent", at=_T0 + timedelta(hours=1), now=_T0
    )
    assert result is RecordResult.FUTURE


def test_out_of_order_quote_rejected() -> None:
    tracker = _tracker()
    _connected_subscribed(tracker, _T0)
    at1 = _T0 + timedelta(seconds=10)
    tracker.record_market_event(event_type="best_bid_ask", at=at1, now=at1)
    at2 = _T0 + timedelta(seconds=5)
    result = tracker.record_market_event(event_type="best_bid_ask", at=at2, now=at1)
    assert result is RecordResult.OUT_OF_ORDER


def test_out_of_order_transport_rejected() -> None:
    tracker = _tracker()
    tracker.record_transport_event(kind="connected", at=_T0, now=_T0)
    result = tracker.record_transport_event(
        kind="pong_sent", at=_T0 - timedelta(seconds=1), now=_T0
    )
    assert result is RecordResult.OUT_OF_ORDER


def test_unknown_kind_rejected() -> None:
    tracker = _tracker()
    assert tracker.record_transport_event(kind="bogus", at=_T0, now=_T0) is RecordResult.UNKNOWN_KIND
    assert tracker.record_market_event(event_type="bogus", at=_T0, now=_T0) is RecordResult.UNKNOWN_KIND


# --- transport ----------------------------------------------------------------


def test_never_connected_transport_unknown() -> None:
    verdict = _tracker().evaluate(session=_session(MarketSessionState.CLOSED), now=_T0)
    assert verdict.transport is TransportHealthStatus.UNKNOWN


def test_connected_warming_before_minimum_uptime() -> None:
    thr = HealthThresholds(
        heartbeat_timeout_seconds=300.0,
        minimum_stable_uptime_seconds=300.0,
        reconnect_window_seconds=120.0,
        max_connects_in_window=5,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=30.0,
        max_quote_age_seconds=60.0,
    )
    tracker = MarketHealthTracker(thr)
    _connected_subscribed(tracker, _T0)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN), now=_T0 + timedelta(seconds=10))
    assert verdict.transport is TransportHealthStatus.WARMING


def test_disconnect_makes_transport_unhealthy() -> None:
    tracker = _tracker()
    _connected_subscribed(tracker, _T0)
    tracker.record_transport_event(kind="disconnect", at=_T0 + timedelta(seconds=5), now=_T0 + timedelta(seconds=5))
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN), now=_T0 + timedelta(seconds=6))
    assert verdict.transport is TransportHealthStatus.UNHEALTHY


def test_flapping_repeated_connects() -> None:
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
    for i in range(4):
        tracker.record_transport_event(kind="connected", at=_T0 + timedelta(seconds=i * 5), now=_T0 + timedelta(seconds=20))
    tracker.record_transport_event(kind="all_subscribed", at=_T0 + timedelta(seconds=20), now=_T0 + timedelta(seconds=21))
    tracker.record_transport_event(kind="pong_sent", at=_T0 + timedelta(seconds=20), now=_T0 + timedelta(seconds=21))
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN), now=_T0 + timedelta(seconds=21))
    assert verdict.transport is TransportHealthStatus.FLAPPING


def test_ping_pong_timeout_unhealthy() -> None:
    thr = HealthThresholds(
        heartbeat_timeout_seconds=30.0,
        minimum_stable_uptime_seconds=1.0,
        reconnect_window_seconds=120.0,
        max_connects_in_window=5,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=30.0,
        max_quote_age_seconds=60.0,
    )
    tracker = MarketHealthTracker(thr)
    at = _T0
    tracker.record_transport_event(kind="connected", at=at, now=at)
    tracker.record_transport_event(kind="all_subscribed", at=at, now=at)
    tracker.record_transport_event(kind="pong_sent", at=at, now=at)
    now = at + timedelta(seconds=60)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.transport is TransportHealthStatus.UNHEALTHY


# --- market data --------------------------------------------------------------


def test_open_with_fresh_quote_is_healthy() -> None:
    tracker = _tracker()
    _connected_subscribed(tracker, _T0)
    tracker.record_market_event(event_type="best_bid_ask", at=_T0 + timedelta(seconds=1), now=_T0 + timedelta(seconds=2))
    now = _T0 + timedelta(seconds=2)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.market_data is MarketDataHealthStatus.HEALTHY
    assert verdict.is_healthy is False  # transport still WARMING (min uptime)


def test_open_with_starvation_not_healthy() -> None:
    tracker = _tracker()
    _connected_subscribed(tracker, _T0)
    tracker.record_market_event(event_type="best_bid_ask", at=_T0, now=_T0)
    now = _T0 + timedelta(seconds=45)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.market_data is MarketDataHealthStatus.STARVED
    assert verdict.is_execution_ready is False


def test_closed_heartbeat_only_not_expected() -> None:
    tracker = _tracker()
    _connected_subscribed(tracker, _T0)
    tracker.record_market_event(event_type="heartbeat", at=_T0 + timedelta(seconds=10), now=_T0 + timedelta(seconds=10))
    verdict = tracker.evaluate(session=_session(MarketSessionState.CLOSED), now=_T0 + timedelta(seconds=300))
    assert verdict.market_data is MarketDataHealthStatus.NOT_EXPECTED


def test_open_warming_not_execution_ready() -> None:
    tracker = _tracker()
    _connected_subscribed(tracker, _T0)
    now = _T0 + timedelta(seconds=10)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.market_data is MarketDataHealthStatus.WARMING
    assert verdict.is_execution_ready is False


def test_strict_healthy_requires_both() -> None:
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
    tracker = MarketHealthTracker(thr)
    at = _T0
    tracker.record_transport_event(kind="connected", at=at, now=at)
    tracker.record_transport_event(kind="all_subscribed", at=at, now=at)
    tracker.record_transport_event(kind="pong_sent", at=at, now=at)
    tracker.record_market_event(event_type="best_bid_ask", at=at + timedelta(seconds=1), now=at + timedelta(seconds=2))
    now = at + timedelta(seconds=5)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.is_healthy is True
    assert verdict.is_execution_ready is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"heartbeat_timeout_seconds": 0},
        {"max_connects_in_window": 0},
        {"quote_grace_seconds": -1},
    ],
)
def test_thresholds_reject_invalid(kwargs: dict[str, float]) -> None:
    base = {
        "heartbeat_timeout_seconds": 60.0,
        "minimum_stable_uptime_seconds": 300.0,
        "reconnect_window_seconds": 120.0,
        "max_connects_in_window": 3,
        "flapping_min_uptime_seconds": 30.0,
        "flapping_min_market_events": 1,
        "quote_grace_seconds": 30.0,
        "quote_starvation_seconds": 30.0,
        "max_quote_age_seconds": 60.0,
    }
    base.update(kwargs)
    with pytest.raises(HealthPolicyError):
        HealthThresholds(**base)
