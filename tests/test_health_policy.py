"""RTM-7b.2 — transport/market-data health policy tests (pure; injected now)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from market_data.health_policy import (
    HealthPolicyError,
    HealthStatus,
    HealthThresholds,
    MarketHealthTracker,
)
from market_data.market_session import MarketSession, MarketSessionState

from domain.enums import Market

_KST = ZoneInfo("Asia/Seoul")
_T0 = datetime(2026, 6, 15, 10, 0, 0, tzinfo=_KST)


def _session(state: MarketSessionState, at: datetime = _T0) -> MarketSession:
    return MarketSession(market=Market.KR, state=state, as_of=at)


def _connected_subscribed(tracker: MarketHealthTracker, at: datetime) -> None:
    tracker.record_transport_event(kind="connected", at=at)
    tracker.record_transport_event(kind="all_subscribed", at=at)


# --- transport ----------------------------------------------------------------


def test_never_connected_transport_unknown() -> None:
    tracker = MarketHealthTracker()
    verdict = tracker.evaluate(session=_session(MarketSessionState.CLOSED), now=_T0)
    assert verdict.transport is HealthStatus.UNKNOWN
    assert "transport_never_connected" in verdict.reasons


def test_connected_but_not_all_subscribed_is_unhealthy() -> None:
    tracker = MarketHealthTracker()
    tracker.record_transport_event(kind="connected", at=_T0)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN), now=_T0)
    assert verdict.transport is HealthStatus.UNHEALTHY
    assert "transport_subscriptions_incomplete" in verdict.reasons


def test_disconnect_makes_transport_unhealthy() -> None:
    tracker = MarketHealthTracker()
    _connected_subscribed(tracker, _T0)
    tracker.record_transport_event(kind="disconnect", at=_T0 + timedelta(seconds=5))
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN), now=_T0 + timedelta(seconds=6))
    assert verdict.transport is HealthStatus.UNHEALTHY
    assert "transport_disconnected" in verdict.reasons


def test_flapping_repeated_connects_in_window_is_unhealthy() -> None:
    tracker = MarketHealthTracker(HealthThresholds(max_connects_in_window=3))
    for i in range(4):  # 4 connects in window > max 3 -> flapping
        tracker.record_transport_event(kind="connected", at=_T0 + timedelta(seconds=i * 5))
    tracker.record_transport_event(kind="all_subscribed", at=_T0 + timedelta(seconds=20))
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN), now=_T0 + timedelta(seconds=21))
    assert verdict.transport is HealthStatus.UNHEALTHY
    assert "transport_flapping" in verdict.reasons
    assert verdict.reconnects_in_window == 4


def test_old_connects_fall_out_of_window() -> None:
    thr = HealthThresholds(reconnect_window_seconds=120.0, max_connects_in_window=3)
    tracker = MarketHealthTracker(thr)
    # 3 stale connects long ago + 1 recent -> only the recent counts.
    for i in range(3):
        tracker.record_transport_event(kind="connected", at=_T0 + timedelta(seconds=i))
    recent = _T0 + timedelta(seconds=600)
    tracker.record_transport_event(kind="connected", at=recent)
    tracker.record_transport_event(kind="all_subscribed", at=recent)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, recent), now=recent)
    assert verdict.reconnects_in_window == 1
    # stale connects fell out of the window, so it is not flapping; transport is healthy.
    assert verdict.transport is HealthStatus.HEALTHY
    assert "transport_flapping" not in verdict.reasons


# --- market data --------------------------------------------------------------


def test_open_with_fresh_quote_is_healthy() -> None:
    tracker = MarketHealthTracker()
    _connected_subscribed(tracker, _T0)
    tracker.record_market_event(event_type="best_bid_ask", at=_T0 + timedelta(seconds=1))
    now = _T0 + timedelta(seconds=2)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.transport is HealthStatus.HEALTHY
    assert verdict.market_data is HealthStatus.HEALTHY
    assert verdict.is_healthy is True
    assert verdict.last_quote_age_seconds == pytest.approx(1.0)


def test_open_with_stale_quote_is_starvation() -> None:
    thr = HealthThresholds(quote_starvation_seconds=30.0)
    tracker = MarketHealthTracker(thr)
    _connected_subscribed(tracker, _T0)
    tracker.record_market_event(event_type="best_bid_ask", at=_T0)
    now = _T0 + timedelta(seconds=45)  # 45s > 30s
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.transport is HealthStatus.HEALTHY
    assert verdict.market_data is HealthStatus.UNHEALTHY
    assert "quote_starvation" in verdict.reasons
    assert verdict.is_healthy is False


def test_after_hours_heartbeat_only_is_healthy() -> None:
    # 장외 + heartbeat만 + quote 없음 -> transport/market-data 모두 정상.
    tracker = MarketHealthTracker()
    _connected_subscribed(tracker, _T0)
    tracker.record_market_event(event_type="heartbeat", at=_T0 + timedelta(seconds=10))
    now = _T0 + timedelta(seconds=300)
    for state in (MarketSessionState.CLOSED, MarketSessionState.PRE_OPEN, MarketSessionState.POST_CLOSE):
        verdict = tracker.evaluate(session=_session(state, now), now=now)
        assert verdict.transport is HealthStatus.HEALTHY, state
        assert verdict.market_data is HealthStatus.HEALTHY, state
        assert verdict.is_healthy is True, state


def test_open_no_quote_within_grace_is_unknown() -> None:
    thr = HealthThresholds(quote_grace_seconds=30.0)
    tracker = MarketHealthTracker(thr)
    _connected_subscribed(tracker, _T0)
    now = _T0 + timedelta(seconds=10)  # within grace, no quote yet
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.market_data is HealthStatus.UNKNOWN
    assert "quote_pending_grace" in verdict.reasons


def test_open_no_quote_beyond_grace_is_starvation() -> None:
    thr = HealthThresholds(quote_grace_seconds=30.0)
    tracker = MarketHealthTracker(thr)
    _connected_subscribed(tracker, _T0)
    now = _T0 + timedelta(seconds=45)  # beyond grace, still no quote
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.market_data is HealthStatus.UNHEALTHY
    assert "quote_starvation_no_quote" in verdict.reasons


def test_open_flapping_makes_both_unhealthy() -> None:
    thr = HealthThresholds(max_connects_in_window=2, quote_starvation_seconds=30.0)
    tracker = MarketHealthTracker(thr)
    for i in range(3):  # 3 > 2 -> flapping
        tracker.record_transport_event(kind="connected", at=_T0 + timedelta(seconds=i))
    now = _T0 + timedelta(seconds=60)  # no quote -> beyond grace too
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.transport is HealthStatus.UNHEALTHY
    assert verdict.market_data is HealthStatus.UNHEALTHY
    assert verdict.is_healthy is False


# --- thresholds validation ----------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quote_starvation_seconds": 0},
        {"reconnect_window_seconds": -1},
        {"max_connects_in_window": 0},
        {"quote_grace_seconds": 0},
    ],
)
def test_thresholds_reject_non_positive(kwargs: dict[str, float]) -> None:
    with pytest.raises(HealthPolicyError):
        HealthThresholds(**kwargs)
