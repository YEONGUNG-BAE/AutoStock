"""RTM-7b.1 — market session/calendar tests (pure; fixture clock; no network)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from market_data.market_session import (
    FixtureMarketCalendar,
    MarketSession,
    MarketSessionError,
    MarketSessionState,
    SessionWindow,
)

from domain.enums import Market

_KST = ZoneInfo("Asia/Seoul")
_UTC = ZoneInfo("UTC")


def _kst(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_KST)


def _cal(**overrides: object) -> FixtureMarketCalendar:
    return FixtureMarketCalendar.for_krx(**overrides)  # type: ignore[arg-type]


# --- session state boundaries (반열린 구간) ------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, MarketSessionState.CLOSED),
        (8, 29, MarketSessionState.CLOSED),
        (8, 30, MarketSessionState.PRE_OPEN),  # pre_open 포함
        (8, 59, MarketSessionState.PRE_OPEN),
        (9, 0, MarketSessionState.OPEN),  # open 포함
        (12, 0, MarketSessionState.OPEN),
        (15, 29, MarketSessionState.OPEN),
        (15, 30, MarketSessionState.POST_CLOSE),  # close 시각은 POST_CLOSE
        (15, 59, MarketSessionState.POST_CLOSE),
        (16, 0, MarketSessionState.CLOSED),  # post_close_end 시각은 CLOSED
        (23, 59, MarketSessionState.CLOSED),
    ],
)
def test_session_boundaries_on_trading_day(hour: int, minute: int, expected: MarketSessionState) -> None:
    cal = _cal()
    # 2026-06-15 = Monday, trading day.
    session = cal.session_at(Market.KR, _kst(2026, 6, 15, hour, minute))
    assert session.state is expected
    assert session.market is Market.KR


def test_open_session_flags() -> None:
    cal = _cal()
    session = cal.session_at(Market.KR, _kst(2026, 6, 15, 10, 0))
    assert session.is_open is True
    assert session.is_tradable_quote_expected is True


@pytest.mark.parametrize("state_hour", [0, 8, 15, 16, 23])
def test_non_open_states_do_not_expect_quotes(state_hour: int) -> None:
    cal = _cal()
    session = cal.session_at(Market.KR, _kst(2026, 6, 15, state_hour, 45))
    if session.state is MarketSessionState.OPEN:
        pytest.skip("hour landed in OPEN")
    assert session.is_tradable_quote_expected is False


# --- weekend / holiday --------------------------------------------------------


def test_weekend_is_closed_all_day() -> None:
    cal = _cal()
    # 2026-06-13 = Saturday, 2026-06-14 = Sunday.
    assert cal.session_at(Market.KR, _kst(2026, 6, 13, 10, 0)).state is MarketSessionState.CLOSED
    assert cal.session_at(Market.KR, _kst(2026, 6, 14, 10, 0)).state is MarketSessionState.CLOSED
    assert cal.is_trading_day(Market.KR, date(2026, 6, 13)) is False


def test_injected_holiday_is_closed() -> None:
    holiday = date(2026, 6, 15)  # Monday made a holiday via injection (not hardcoded).
    cal = _cal(holidays=[holiday])
    assert cal.session_at(Market.KR, _kst(2026, 6, 15, 10, 0)).state is MarketSessionState.CLOSED
    assert cal.is_trading_day(Market.KR, holiday) is False
    # the following trading day is unaffected.
    assert cal.is_trading_day(Market.KR, date(2026, 6, 16)) is True


# --- half-day (반장) ----------------------------------------------------------


def test_half_day_brings_close_early() -> None:
    half = date(2026, 6, 15)
    cal = _cal(half_days={half: time(13, 0)})
    # 13:00 종료 -> 12:59 OPEN, 13:00 POST_CLOSE, post_close_end는 margin(30분) 적용 13:30.
    assert cal.session_at(Market.KR, _kst(2026, 6, 15, 12, 59)).state is MarketSessionState.OPEN
    assert cal.session_at(Market.KR, _kst(2026, 6, 15, 13, 0)).state is MarketSessionState.POST_CLOSE
    assert cal.session_at(Market.KR, _kst(2026, 6, 15, 13, 29)).state is MarketSessionState.POST_CLOSE
    assert cal.session_at(Market.KR, _kst(2026, 6, 15, 13, 30)).state is MarketSessionState.CLOSED


# --- timezone correctness -----------------------------------------------------


def test_utc_instant_is_converted_to_market_local_state() -> None:
    cal = _cal()
    # 2026-06-15 00:30 UTC == 09:30 KST -> OPEN.
    utc_instant = datetime(2026, 6, 15, 0, 30, tzinfo=_UTC)
    assert cal.session_at(Market.KR, utc_instant).state is MarketSessionState.OPEN


def test_utc_instant_crossing_local_day_boundary() -> None:
    cal = _cal()
    # 2026-06-14 22:00 UTC == 2026-06-15 07:00 KST -> Monday, CLOSED (before pre_open).
    utc_instant = datetime(2026, 6, 14, 22, 0, tzinfo=_UTC)
    session = cal.session_at(Market.KR, utc_instant)
    assert session.state is MarketSessionState.CLOSED


def test_naive_instant_is_rejected() -> None:
    cal = _cal()
    with pytest.raises(MarketSessionError, match="timezone-aware"):
        cal.session_at(Market.KR, datetime(2026, 6, 15, 10, 0))


# --- SessionWindow validation -------------------------------------------------


def test_session_window_rejects_out_of_order_times() -> None:
    with pytest.raises(MarketSessionError, match="non-decreasing"):
        SessionWindow(
            pre_open=time(9, 0), open=time(8, 30), close=time(15, 30), post_close_end=time(16, 0)
        )


def test_for_krx_defaults() -> None:
    cal = _cal()
    assert cal.timezone == _KST
    assert cal.window == SessionWindow(
        pre_open=time(8, 30), open=time(9, 0), close=time(15, 30), post_close_end=time(16, 0)
    )
    assert cal.weekend_weekdays == frozenset({5, 6})


def test_market_session_carries_as_of() -> None:
    cal = _cal()
    instant = _kst(2026, 6, 15, 10, 0)
    session = cal.session_at(Market.KR, instant)
    assert isinstance(session, MarketSession)
    assert session.as_of == instant
