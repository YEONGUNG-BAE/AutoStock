"""RTM-7b.1 — 시장 세션 상태 + 캘린더 계약 (순수, 네트워크/asyncio 없음).

supervisor가 "평일 09:00~15:30"을 직접 판단하지 않게 하는 것이 핵심이다. 세션 상태는
오직 주입식 `MarketCalendarProvider`만 안다. 이 모듈은 datetime/zoneinfo/enum/dataclass만
쓰며 broker/ledger/network/asyncio를 import하지 않는다(market_data import guard 준수).

`FixtureMarketCalendar`는 결정론적 테스트용 공급자다. 휴장일/반장(half-day)/주말 집합을
주입으로 받으며 실제 KRX 휴장일을 하드코딩하지 않는다(실 KRX 공급자는 후속 레인에서 분리).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from domain.enums import Market


class MarketSessionState(StrEnum):
    """한 시장의 현재 거래 세션 상태. supervisor의 가동 판단 입력이다."""

    CLOSED = "CLOSED"  # 휴장일/주말/장 시간 밖 — 거래 불가
    PRE_OPEN = "PRE_OPEN"  # 장전(동시호가 등)
    OPEN = "OPEN"  # 정규장
    POST_CLOSE = "POST_CLOSE"  # 장후


class MarketSessionError(Exception):
    """세션/캘린더 설정 위반. 시각/시장 식별자 외 민감정보를 담지 않는다."""


@dataclass(frozen=True)
class SessionWindow:
    """하루 정규 세션의 경계 시각(시장 로컬 타임존 기준). 반열린 구간 [start, end)으로 해석.

    - [00:00, pre_open) -> CLOSED
    - [pre_open, open) -> PRE_OPEN
    - [open, close) -> OPEN
    - [close, post_close_end) -> POST_CLOSE
    - [post_close_end, 24:00) -> CLOSED
    """

    pre_open: time
    open: time
    close: time
    post_close_end: time

    def __post_init__(self) -> None:
        if not (self.pre_open <= self.open <= self.close <= self.post_close_end):
            raise MarketSessionError(
                "SessionWindow times must be non-decreasing: pre_open<=open<=close<=post_close_end."
            )


@dataclass(frozen=True)
class MarketSession:
    """특정 시각의 세션 판정 결과. supervisor/health policy의 입력으로 쓰인다."""

    market: Market
    state: MarketSessionState
    as_of: datetime

    @property
    def is_open(self) -> bool:
        return self.state is MarketSessionState.OPEN

    @property
    def is_tradable_quote_expected(self) -> bool:
        """이 상태에서 정상적으로 시세(quote)가 흘러야 하는가. OPEN에서만 True.

        PRE_OPEN/POST_CLOSE/CLOSED에서는 quote starvation을 unhealthy로 보지 않는다.
        """
        return self.state is MarketSessionState.OPEN


class MarketCalendarProvider(Protocol):
    """주입식 시장 캘린더 계약. supervisor는 이 인터페이스로만 세션을 질의한다.

    `instant`는 timezone-aware여야 한다(naive datetime은 거부). 구현체는 instant를
    시장 로컬 타임존으로 변환해 판정한다.
    """

    def session_at(self, market: Market, instant: datetime) -> MarketSession: ...

    def is_trading_day(self, market: Market, day: date) -> bool: ...


@dataclass(frozen=True)
class FixtureMarketCalendar:
    """결정론적 fixture 캘린더. 휴장일/반장/주말 집합과 세션 윈도우를 주입받는다.

    실제 KRX 휴장일을 추측해 하드코딩하지 않는다. 테스트는 필요한 날짜만 주입한다.
    """

    timezone: ZoneInfo
    window: SessionWindow
    holidays: frozenset[date] = frozenset()
    # 반장(half-day): 해당 날짜의 조기 정규장 종료 시각. post_close_end는 동일 margin만큼 이동.
    half_days: Mapping[date, time] | None = None
    # 비거래 요일(0=월 .. 6=일). 기본 주말(토/일).
    weekend_weekdays: frozenset[int] = frozenset({5, 6})

    @classmethod
    def for_krx(
        cls,
        *,
        holidays: Iterable[date] = (),
        half_days: Mapping[date, time] | None = None,
    ) -> FixtureMarketCalendar:
        """KRX 정규장 기본값(잠정): pre_open 08:30, open 09:00, close 15:30, post_close_end 16:00.

        이 시각들은 잠정값이며 월요일 live smoke evidence로 보정 후 확정한다.
        """
        return cls(
            timezone=ZoneInfo("Asia/Seoul"),
            window=SessionWindow(
                pre_open=time(8, 30),
                open=time(9, 0),
                close=time(15, 30),
                post_close_end=time(16, 0),
            ),
            holidays=frozenset(holidays),
            half_days=half_days,
        )

    def is_trading_day(self, market: Market, day: date) -> bool:  # noqa: ARG002 — 단일-시장 fixture
        if day.weekday() in self.weekend_weekdays:
            return False
        return day not in self.holidays

    def session_at(self, market: Market, instant: datetime) -> MarketSession:
        if instant.tzinfo is None:
            raise MarketSessionError("session_at requires a timezone-aware instant.")
        local = instant.astimezone(self.timezone)
        local_day = local.date()
        if not self.is_trading_day(market, local_day):
            return MarketSession(market=market, state=MarketSessionState.CLOSED, as_of=instant)

        window = self._window_for(local_day)
        now_t = local.timetz().replace(tzinfo=None)
        if now_t < window.pre_open or now_t >= window.post_close_end:
            state = MarketSessionState.CLOSED
        elif now_t < window.open:
            state = MarketSessionState.PRE_OPEN
        elif now_t < window.close:
            state = MarketSessionState.OPEN
        else:
            state = MarketSessionState.POST_CLOSE
        return MarketSession(market=market, state=state, as_of=instant)

    def _window_for(self, day: date) -> SessionWindow:
        half = None if self.half_days is None else self.half_days.get(day)
        if half is None:
            return self.window
        # 반장: 정규장 종료를 조기 시각으로 당기고, post_close 지속시간(default close→post_close_end)
        # 만큼 post_close_end도 이동한다. clamp으로 24h 경계를 넘지 않게 한다.
        margin = _minutes_between(self.window.close, self.window.post_close_end)
        post_end = _add_minutes_clamped(half, margin)
        return SessionWindow(
            pre_open=self.window.pre_open,
            open=self.window.open,
            close=half,
            post_close_end=post_end,
        )


def _minutes_between(start: time, end: time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def _add_minutes_clamped(base: time, minutes: int) -> time:
    total = base.hour * 60 + base.minute + minutes
    total = max(0, min(total, 23 * 60 + 59))
    return time(total // 60, total % 60)


__all__ = [
    "FixtureMarketCalendar",
    "MarketCalendarProvider",
    "MarketSession",
    "MarketSessionError",
    "MarketSessionState",
    "SessionWindow",
]
