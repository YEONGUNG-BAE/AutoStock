"""RTM-7b.2 — transport/market-data health policy tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from market_data.health_policy import (
    ConnectionEpochResult,
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


def _thr(**overrides: object) -> HealthThresholds:
    base = {
        "subscription_grace_seconds": 30.0,
        "heartbeat_timeout_seconds": 300.0,
        "minimum_stable_uptime_seconds": 1.0,
        "flapping_window_seconds": 120.0,
        "flapping_max_short_epochs": 3,
        "flapping_min_uptime_seconds": 30.0,
        "flapping_min_market_events": 1,
        "quote_grace_seconds": 30.0,
        "quote_starvation_seconds": 30.0,
        "max_quote_age_seconds": 60.0,
    }
    base.update(overrides)
    return HealthThresholds(**base)  # type: ignore[arg-type]


def _session(state: MarketSessionState, at: datetime = _T0) -> MarketSession:
    return MarketSession(market=Market.KR, state=state, as_of=at)


def _tracker(**overrides: object) -> MarketHealthTracker:
    return MarketHealthTracker(_thr(**overrides))


def _connected(tracker: MarketHealthTracker, at: datetime) -> None:
    tracker.record_transport_event(kind="connected", at=at, now=at)


def _subscribed(tracker: MarketHealthTracker, at: datetime) -> None:
    tracker.record_transport_event(kind="all_subscribed", at=at, now=at)


def _pong(tracker: MarketHealthTracker, at: datetime) -> None:
    tracker.record_transport_event(kind="pong_sent", at=at, now=at)


# --- subscription grace -------------------------------------------------------


def test_subscription_grace_warming_not_execution_ready() -> None:
    tracker = _tracker(subscription_grace_seconds=60.0)
    _connected(tracker, _T0)
    now = _T0 + timedelta(seconds=10)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.transport is TransportHealthStatus.WARMING
    assert tracker.all_subscribed is False
    assert verdict.is_execution_ready is False


def test_subscription_grace_exceeded_unhealthy() -> None:
    tracker = _tracker(subscription_grace_seconds=30.0)
    _connected(tracker, _T0)
    now = _T0 + timedelta(seconds=45)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.transport is TransportHealthStatus.UNHEALTHY
    assert "subscription_grace_exceeded" in verdict.reasons


def test_all_subscribed_only_on_explicit_event() -> None:
    tracker = _tracker()
    _connected(tracker, _T0)
    assert tracker.all_subscribed is False
    _subscribed(tracker, _T0)
    assert tracker.all_subscribed is True


# --- timestamp ----------------------------------------------------------------


def test_naive_transport_rejected() -> None:
    tracker = _tracker()
    with pytest.raises(HealthPolicyError):
        tracker.record_transport_event(kind="connected", at=datetime(2026, 6, 15, 10, 0), now=_T0)


def test_future_quote_rejected_state_unchanged() -> None:
    tracker = _tracker()
    _connected(tracker, _T0)
    _subscribed(tracker, _T0)
    result = tracker.record_market_event(
        event_type="best_bid_ask", at=_T0 + timedelta(hours=1), now=_T0
    )
    assert result is RecordResult.FUTURE


def test_out_of_order_quote_rejected() -> None:
    tracker = _tracker()
    _connected(tracker, _T0)
    _subscribed(tracker, _T0)
    at1 = _T0 + timedelta(seconds=10)
    tracker.record_market_event(event_type="best_bid_ask", at=at1, now=at1)
    result = tracker.record_market_event(event_type="best_bid_ask", at=_T0 + timedelta(seconds=5), now=at1)
    assert result is RecordResult.OUT_OF_ORDER


# --- epoch isolation ----------------------------------------------------------


def test_epoch_pong_reset_no_immediate_unhealthy() -> None:
    tracker = _tracker(
        heartbeat_timeout_seconds=60.0,
        minimum_stable_uptime_seconds=120.0,
    )
    t1 = _T0
    _connected(tracker, t1)
    _subscribed(tracker, t1)
    _pong(tracker, t1)
    tracker.record_transport_event(kind="disconnect", at=t1 + timedelta(minutes=5), now=t1 + timedelta(minutes=5))
    t2 = t1 + timedelta(minutes=10)
    _connected(tracker, t2)
    _subscribed(tracker, t2)
    now = t2 + timedelta(seconds=5)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.transport is TransportHealthStatus.WARMING
    assert "heartbeat_timeout" not in verdict.reasons


def test_delayed_old_epoch_pong_rejected() -> None:
    tracker = _tracker()
    t2 = _T0 + timedelta(minutes=10)
    _connected(tracker, t2)
    old_pong = _T0  # epoch 1 시각
    result = tracker.record_transport_event(kind="pong_sent", at=old_pong, now=t2)
    assert result is RecordResult.OUT_OF_ORDER


# --- flapping completed epochs ------------------------------------------------


def test_one_event_then_drop_flapping() -> None:
    tracker = _tracker(
        flapping_max_short_epochs=3,
        flapping_window_seconds=600.0,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
    )
    for i in range(3):
        base = _T0 + timedelta(minutes=i * 2)
        _connected(tracker, base)
        _subscribed(tracker, base)
        tracker.record_market_event(event_type="best_bid_ask", at=base + timedelta(seconds=1), now=base + timedelta(seconds=1))
        tracker.record_transport_event(kind="disconnect", at=base + timedelta(seconds=5), now=base + timedelta(seconds=5))
    now = _T0 + timedelta(minutes=10)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.transport is TransportHealthStatus.FLAPPING


def test_stable_epoch_recovery() -> None:
    tracker = _tracker(flapping_max_short_epochs=3, minimum_stable_uptime_seconds=1.0)
    base = _T0
    _connected(tracker, base)
    _subscribed(tracker, base)
    _pong(tracker, base)
    tracker.record_market_event(event_type="best_bid_ask", at=base + timedelta(seconds=1), now=base + timedelta(seconds=1))
    tracker.record_market_event(event_type="best_bid_ask", at=base + timedelta(seconds=2), now=base + timedelta(seconds=2))
    tracker.record_transport_event(kind="disconnect", at=base + timedelta(seconds=60), now=base + timedelta(seconds=60))
    now = base + timedelta(seconds=61)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.transport is not TransportHealthStatus.FLAPPING


def test_completed_epoch_history_bounded() -> None:
    tracker = _tracker(flapping_max_short_epochs=100)
    for i in range(80):
        at = _T0 + timedelta(seconds=i * 3)
        _connected(tracker, at)
        tracker.record_transport_event(kind="disconnect", at=at + timedelta(seconds=1), now=at + timedelta(seconds=1))
    assert len(tracker._completed_epochs) <= 64


# --- market data --------------------------------------------------------------


def test_open_starvation_not_healthy() -> None:
    tracker = _tracker()
    _connected(tracker, _T0)
    _subscribed(tracker, _T0)
    _pong(tracker, _T0)
    tracker.record_market_event(event_type="best_bid_ask", at=_T0, now=_T0)
    now = _T0 + timedelta(seconds=45)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.market_data is MarketDataHealthStatus.STARVED
    assert verdict.is_execution_ready is False


def test_strict_healthy_requires_both() -> None:
    tracker = _tracker(minimum_stable_uptime_seconds=1.0)
    at = _T0
    _connected(tracker, at)
    _subscribed(tracker, at)
    _pong(tracker, at)
    tracker.record_market_event(event_type="best_bid_ask", at=at + timedelta(seconds=2), now=at + timedelta(seconds=2))
    now = at + timedelta(seconds=5)
    verdict = tracker.evaluate(session=_session(MarketSessionState.OPEN, now), now=now)
    assert verdict.is_healthy is True


@pytest.mark.parametrize("field", ["subscription_grace_seconds", "flapping_window_seconds"])
def test_thresholds_reject_non_positive(field: str) -> None:
    with pytest.raises(HealthPolicyError):
        _thr(**{field: 0})
