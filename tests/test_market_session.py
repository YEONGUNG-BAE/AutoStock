"""RTM-7b.1 — market session/calendar tests (pure; fixture clock; no network)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from market_data.market_session import (
    CalendarReason,
    ExplicitMarketScheduleProvider,
    FixtureMarketCalendar,
    MarketSession,
    MarketSessionError,
    MarketSessionState,
    SessionWindow,
    build_explicit_schedule,
)

from domain.enums import Market

_KST = ZoneInfo("Asia/Seoul")
_UTC = ZoneInfo("UTC")
_WINDOW = SessionWindow(
    pre_open=time(8, 30), open=time(9, 0), close=time(15, 30), post_close_end=time(16, 0)
)


def _kst(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_KST)


def _cal(**overrides: object) -> FixtureMarketCalendar:
    return FixtureMarketCalendar.for_krx(**overrides)  # type: ignore[arg-type]


def _explicit(days: list[date], **kwargs: object) -> ExplicitMarketScheduleProvider:
    return build_explicit_schedule(
        timezone=_KST, trading_days=days, window=_WINDOW, **kwargs  # type: ignore[arg-type]
    )


# --- FixtureMarketCalendar (테스트 전용) ---------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, MarketSessionState.CLOSED),
        (8, 30, MarketSessionState.PRE_OPEN),
        (9, 0, MarketSessionState.OPEN),
        (15, 30, MarketSessionState.POST_CLOSE),
        (16, 0, MarketSessionState.CLOSED),
    ],
)
def test_fixture_session_boundaries(hour: int, minute: int, expected: MarketSessionState) -> None:
    cal = _cal()
    session = cal.session_at(Market.KR, _kst(2026, 6, 15, hour, minute))
    assert session.state is expected


def test_fixture_weekend_is_closed() -> None:
    cal = _cal()
    assert cal.session_at(Market.KR, _kst(2026, 6, 13, 10, 0)).state is MarketSessionState.CLOSED


def test_fixture_injected_holiday() -> None:
    holiday = date(2026, 6, 15)
    cal = _cal(holidays=[holiday])
    assert cal.session_at(Market.KR, _kst(2026, 6, 15, 10, 0)).state is MarketSessionState.CLOSED


def test_fixture_half_day() -> None:
    half = date(2026, 6, 15)
    cal = _cal(half_days={half: time(13, 0)})
    assert cal.session_at(Market.KR, _kst(2026, 6, 15, 12, 59)).state is MarketSessionState.OPEN
    assert cal.session_at(Market.KR, _kst(2026, 6, 15, 13, 0)).state is MarketSessionState.POST_CLOSE


# --- ExplicitMarketScheduleProvider (production contract) ---------------------


def test_explicit_missing_weekday_is_unknown_calendar_missing() -> None:
    cal = _explicit([date(2026, 6, 16)])  # Tuesday only; Monday 15 missing.
    session = cal.session_at(Market.KR, _kst(2026, 6, 15, 10, 0))
    assert session.state is MarketSessionState.UNKNOWN
    assert session.calendar_reason is CalendarReason.CALENDAR_MISSING


def test_explicit_missing_schedule_weekend_is_closed() -> None:
    cal = _explicit([date(2026, 6, 15)])
    session = cal.session_at(Market.KR, _kst(2026, 6, 13, 10, 0))  # Saturday
    assert session.state is MarketSessionState.CLOSED
    assert session.calendar_reason is None


def test_explicit_trading_day_open() -> None:
    cal = _explicit([date(2026, 6, 15)])
    session = cal.session_at(Market.KR, _kst(2026, 6, 15, 10, 0))
    assert session.state is MarketSessionState.OPEN
    assert session.is_tradable_quote_expected is True


def test_explicit_defensive_copy_freeze() -> None:
    source: dict[date, SessionWindow] = {date(2026, 6, 15): _WINDOW}
    cal = ExplicitMarketScheduleProvider(timezone=_KST, schedule=source)
    source[date(2026, 6, 16)] = _WINDOW
    assert cal.is_trading_day(Market.KR, date(2026, 6, 16)) is False


def test_explicit_holiday_not_in_schedule_is_unknown() -> None:
    cal = _explicit([date(2026, 6, 16)])  # only Tuesday
    session = cal.session_at(Market.KR, _kst(2026, 6, 15, 10, 0))  # Monday not scheduled
    assert session.state is MarketSessionState.UNKNOWN


def test_session_window_rejects_out_of_order_times() -> None:
    with pytest.raises(MarketSessionError, match="non-decreasing"):
        SessionWindow(
            pre_open=time(9, 0), open=time(8, 30), close=time(15, 30), post_close_end=time(16, 0)
        )


def test_naive_instant_is_rejected() -> None:
    cal = _explicit([date(2026, 6, 15)])
    with pytest.raises(MarketSessionError, match="timezone-aware"):
        cal.session_at(Market.KR, datetime(2026, 6, 15, 10, 0))


def test_utc_instant_converted_to_kst() -> None:
    cal = _explicit([date(2026, 6, 15)])
    utc_instant = datetime(2026, 6, 15, 0, 30, tzinfo=_UTC)
    assert cal.session_at(Market.KR, utc_instant).state is MarketSessionState.OPEN


def test_market_session_carries_as_of() -> None:
    cal = _explicit([date(2026, 6, 15)])
    instant = _kst(2026, 6, 15, 10, 0)
    session = cal.session_at(Market.KR, instant)
    assert isinstance(session, MarketSession)
    assert session.as_of == instant
