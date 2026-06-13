"""RTM-7b.4 — full trading-day replay + chaos (pure; fake clock; no socket/DNS/broker).

calendar(세션 게이트) + health policy(transport/market-data 분리)를 하루 타임라인으로 재생해
운영 표의 구분을 회귀로 못박는다: 장중 starvation은 unhealthy, 장외 heartbeat-only는 정상,
flapping은 unhealthy(무한 restart 금지의 근거), future timestamp/parser 결함은 freshness를
오염시키지 않는다. 실제 socket/DNS/asyncio/broker/ledger가 전혀 없다.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from market_data.health_policy import (
    HealthStatus,
    HealthThresholds,
    MarketHealthTracker,
)
from market_data.market_session import FixtureMarketCalendar, MarketSessionState

from domain.enums import Market

_KST = ZoneInfo("Asia/Seoul")
_DAY = date(2026, 6, 15)  # Monday, trading day


def _at(h: int, mi: int, s: int = 0) -> datetime:
    return datetime(2026, 6, 15, h, mi, s, tzinfo=_KST)


def _cal(**overrides: object) -> FixtureMarketCalendar:
    return FixtureMarketCalendar.for_krx(**overrides)  # type: ignore[arg-type]


# --- full day replay ----------------------------------------------------------


def test_full_trading_day_health_timeline() -> None:
    """08:50 PRE_OPEN → 09:00 OPEN → 09:15 quote → 10:00 disconnect → 10:00:02 reconnect
    → 11:00 heartbeat-only starvation → 12:00 회복 → 15:30 POST_CLOSE → 18:00 CLOSED."""
    cal = _cal()
    thr = HealthThresholds(quote_starvation_seconds=30.0, quote_grace_seconds=30.0)
    tracker = MarketHealthTracker(thr)

    def verdict(at: datetime):
        return tracker.evaluate(session=cal.session_at(Market.KR, at), now=at)

    # 08:50 — PRE_OPEN. quote 미기대. 아직 연결 전이라 transport UNKNOWN.
    v = verdict(_at(8, 50))
    assert v.session_state == str(MarketSessionState.PRE_OPEN)
    assert v.market_data is HealthStatus.HEALTHY  # 장외: quote 없어도 정상
    assert v.transport is HealthStatus.UNKNOWN

    # 09:00 — OPEN 시점에 연결 + 구독 완료(ACK barrier 통과). grace는 연결 시각 기준.
    tracker.record_transport_event(kind="connected", at=_at(9, 0))
    tracker.record_transport_event(kind="all_subscribed", at=_at(9, 0))

    # 09:00:10 — OPEN, 아직 quote 없음, grace(30s) 내 → market_data UNKNOWN.
    v = verdict(_at(9, 0, 10))
    assert v.session_state == str(MarketSessionState.OPEN)
    assert v.transport is HealthStatus.HEALTHY
    assert v.market_data is HealthStatus.UNKNOWN
    assert "quote_pending_grace" in v.reasons

    # 09:15 — 첫 quote 도착 → 완전 정상.
    tracker.record_market_event(event_type="best_bid_ask", at=_at(9, 15))
    v = verdict(_at(9, 15, 1))
    assert v.transport is HealthStatus.HEALTHY
    assert v.market_data is HealthStatus.HEALTHY
    assert v.is_healthy is True

    # 10:00 — 연결 drop → transport unhealthy(market_data는 마지막 quote freshness로 별도 판정).
    tracker.record_transport_event(kind="disconnect", at=_at(10, 0))
    v = verdict(_at(10, 0, 1))
    assert v.transport is HealthStatus.UNHEALTHY
    assert "transport_disconnected" in v.reasons

    # 10:00:02 — 재접속 + 재구독 완료 → transport 회복. quote는 09:15가 마지막이라 이미 stale.
    tracker.record_transport_event(kind="connected", at=_at(10, 0, 2))
    tracker.record_transport_event(kind="all_subscribed", at=_at(10, 0, 2))
    v = verdict(_at(10, 0, 3))
    assert v.transport is HealthStatus.HEALTHY
    assert v.market_data is HealthStatus.UNHEALTHY  # 45분+ 묵은 quote → starvation
    assert "quote_starvation" in v.reasons

    # 11:00 — heartbeat만 들어오고 quote 여전히 없음(장중) → market_data starvation 지속.
    tracker.record_market_event(event_type="heartbeat", at=_at(11, 0))
    v = verdict(_at(11, 0, 1))
    assert v.transport is HealthStatus.HEALTHY
    assert v.market_data is HealthStatus.UNHEALTHY
    assert v.is_healthy is False

    # 12:00 — quote 회복 → 정상.
    tracker.record_market_event(event_type="best_bid_ask", at=_at(12, 0))
    v = verdict(_at(12, 0, 1))
    assert v.market_data is HealthStatus.HEALTHY
    assert v.is_healthy is True

    # 15:30 — POST_CLOSE. quote 미기대 → 마지막 quote가 묵어도 market_data 정상.
    v = verdict(_at(15, 30))
    assert v.session_state == str(MarketSessionState.POST_CLOSE)
    assert v.market_data is HealthStatus.HEALTHY

    # 18:00 — CLOSED. 동일하게 정상.
    v = verdict(_at(18, 0))
    assert v.session_state == str(MarketSessionState.CLOSED)
    assert v.market_data is HealthStatus.HEALTHY


# --- chaos --------------------------------------------------------------------


def test_chaos_flapping_reconnects_is_unhealthy() -> None:
    """장중 짧은 시간에 반복 reconnect → transport flapping(unhealthy). 무한 restart 금지 근거."""
    cal = _cal()
    thr = HealthThresholds(max_connects_in_window=3, reconnect_window_seconds=120.0)
    tracker = MarketHealthTracker(thr)
    for i in range(4):  # 120초 내 4회 connect > max 3
        tracker.record_transport_event(kind="connected", at=_at(10, 0, i * 10))
    tracker.record_transport_event(kind="all_subscribed", at=_at(10, 0, 40))
    at = _at(10, 0, 41)
    v = tracker.evaluate(session=cal.session_at(Market.KR, at), now=at)
    assert v.transport is HealthStatus.UNHEALTHY
    assert "transport_flapping" in v.reasons
    assert v.reconnects_in_window == 4


def test_chaos_ack_delay_keeps_transport_unhealthy_until_subscribed() -> None:
    """연결됐지만 ACK(all_subscribed) 지연 → 구독 완료 전까지 transport unhealthy."""
    cal = _cal()
    tracker = MarketHealthTracker()
    tracker.record_transport_event(kind="connected", at=_at(9, 0))
    at = _at(9, 0, 5)
    v = tracker.evaluate(session=cal.session_at(Market.KR, at), now=at)
    assert v.transport is HealthStatus.UNHEALTHY
    assert "transport_subscriptions_incomplete" in v.reasons
    # ACK 도착 후 회복.
    tracker.record_transport_event(kind="all_subscribed", at=_at(9, 0, 10))
    at = _at(9, 0, 11)
    v = tracker.evaluate(session=cal.session_at(Market.KR, at), now=at)
    assert v.transport is HealthStatus.HEALTHY


def test_chaos_quote_delay_after_ack_is_grace_then_starvation() -> None:
    """ACK 후 첫 quote 지연: grace 내 UNKNOWN → grace 초과 starvation_no_quote."""
    cal = _cal()
    thr = HealthThresholds(quote_grace_seconds=30.0)
    tracker = MarketHealthTracker(thr)
    tracker.record_transport_event(kind="connected", at=_at(9, 0))
    tracker.record_transport_event(kind="all_subscribed", at=_at(9, 0))
    # grace 내 — UNKNOWN.
    at = _at(9, 0, 10)
    v = tracker.evaluate(session=cal.session_at(Market.KR, at), now=at)
    assert v.market_data is HealthStatus.UNKNOWN
    assert "quote_pending_grace" in v.reasons
    # grace 초과 — starvation.
    at = _at(9, 0, 45)
    v = tracker.evaluate(session=cal.session_at(Market.KR, at), now=at)
    assert v.market_data is HealthStatus.UNHEALTHY
    assert "quote_starvation_no_quote" in v.reasons


def test_chaos_after_hours_heartbeat_only_stays_healthy() -> None:
    """장 마감 후(POST_CLOSE/CLOSED) heartbeat만 + quote 없음 → market_data 정상."""
    cal = _cal()
    tracker = MarketHealthTracker()
    tracker.record_transport_event(kind="connected", at=_at(15, 30))
    tracker.record_transport_event(kind="all_subscribed", at=_at(15, 30))
    tracker.record_market_event(event_type="heartbeat", at=_at(16, 30))
    for at in (_at(15, 45), _at(16, 30, 1), _at(20, 0)):
        v = tracker.evaluate(session=cal.session_at(Market.KR, at), now=at)
        assert v.market_data is HealthStatus.HEALTHY, at
        assert v.is_healthy is True, at


def test_chaos_holiday_is_closed_and_quote_silence_is_fine() -> None:
    """주입 휴장일: 종일 CLOSED, quote 침묵이 unhealthy로 오판되지 않는다."""
    cal = _cal(holidays=[_DAY])
    tracker = MarketHealthTracker()
    tracker.record_transport_event(kind="connected", at=_at(9, 0))
    tracker.record_transport_event(kind="all_subscribed", at=_at(9, 0))
    at = _at(11, 0)  # 평소라면 장중이지만 휴장
    session = cal.session_at(Market.KR, at)
    assert session.state is MarketSessionState.CLOSED
    v = tracker.evaluate(session=session, now=at)
    assert v.market_data is HealthStatus.HEALTHY
    assert v.is_healthy is True


def test_chaos_half_day_early_close_shifts_quote_expectation() -> None:
    """반장: 조기 종료 후에는 quote 미기대로 전환되어 starvation으로 오판하지 않는다."""
    cal = _cal(half_days={_DAY: time(13, 0)})
    thr = HealthThresholds(quote_starvation_seconds=30.0)
    tracker = MarketHealthTracker(thr)
    tracker.record_transport_event(kind="connected", at=_at(9, 0))
    tracker.record_transport_event(kind="all_subscribed", at=_at(9, 0))
    tracker.record_market_event(event_type="best_bid_ask", at=_at(12, 59))
    # 12:59 여전히 OPEN — fresh quote 정상.
    at = _at(12, 59, 1)
    assert cal.session_at(Market.KR, at).state is MarketSessionState.OPEN
    v = tracker.evaluate(session=cal.session_at(Market.KR, at), now=at)
    assert v.market_data is HealthStatus.HEALTHY
    # 13:30 — 반장 종료 + margin 후 CLOSED. 묵은 quote여도 market_data 정상.
    at = _at(13, 30)
    assert cal.session_at(Market.KR, at).state is MarketSessionState.CLOSED
    v = tracker.evaluate(session=cal.session_at(Market.KR, at), now=at)
    assert v.market_data is HealthStatus.HEALTHY
