"""RTM-7b.1 — 시장 세션 상태 + 캘린더 계약 (순수, 네트워크/asyncio 없음).

supervisor가 "평일 09:00~15:30"을 직접 판단하지 않게 하는 것이 핵심이다. 세션 상태는
오직 주입식 `MarketCalendarProvider`만 안다. 이 모듈은 datetime/zoneinfo/enum/dataclass만
쓰며 broker/ledger/network/asyncio를 import하지 않는다(market_data import guard 준수).

`FixtureMarketCalendar`는 **테스트 전용** 결정론적 공급자다. production 경로에서는
`ExplicitMarketScheduleProvider`를 사용한다 — schedule에 명시된 날짜만 거래일이며, 누락은
UNKNOWN/CALENDAR_MISSING으로 fail-closed 대기한다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from zoneinfo import ZoneInfo

from domain.enums import Market


class MarketSessionState(StrEnum):
    """한 시장의 현재 거래 세션 상태. supervisor의 가동 판단 입력이다."""

    UNKNOWN = "UNKNOWN"  # 캘린더 미로드/판단 불가 — 안전 대기
    CLOSED = "CLOSED"  # 휴장일/주말/장 시간 밖 — 거래 불가
    PRE_OPEN = "PRE_OPEN"  # 장전(동시호가 등)
    OPEN = "OPEN"  # 정규장
    POST_CLOSE = "POST_CLOSE"  # 장후


class CalendarReason(StrEnum):
    """세션 판정에 부가된 캘린더 사유. provider contract 위반과 missing을 구분한다."""

    CALENDAR_MISSING = "CALENDAR_MISSING"  # schedule에 해당 날짜 없음 — 안전 대기
    PROVIDER_ERROR = "PROVIDER_ERROR"  # provider contract 위반 — fail-closed


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
    calendar_reason: CalendarReason | None = None

    @property
    def is_open(self) -> bool:
        return self.state is MarketSessionState.OPEN

    @property
    def is_tradable_quote_expected(self) -> bool:
        """이 상태에서 정상적으로 시세(quote)가 흘러야 하는가. OPEN에서만 True.

        PRE_OPEN/POST_CLOSE/CLOSED/UNKNOWN에서는 quote starvation을 unhealthy로 보지 않는다.
        """
        return self.state is MarketSessionState.OPEN

    @property
    def is_calendar_missing(self) -> bool:
        return self.calendar_reason is CalendarReason.CALENDAR_MISSING


class MarketCalendarProvider(Protocol):
    """주입식 시장 캘린더 계약. supervisor는 이 인터페이스로만 세션을 질의한다.

    `instant`는 timezone-aware여야 한다(naive datetime은 거부). 구현체는 instant를
    시장 로컬 타임존으로 변환해 판정한다. provider 예외는 supervisor가 FAILED_CLOSED로
    표면화한다.
    """

    def session_at(self, market: Market, instant: datetime) -> MarketSession: ...

    def is_trading_day(self, market: Market, day: date) -> bool: ...


def _state_from_window(window: SessionWindow, now_t: time) -> MarketSessionState:
    if now_t < window.pre_open or now_t >= window.post_close_end:
        return MarketSessionState.CLOSED
    if now_t < window.open:
        return MarketSessionState.PRE_OPEN
    if now_t < window.close:
        return MarketSessionState.OPEN
    return MarketSessionState.POST_CLOSE


def _minutes_between(start: time, end: time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def _add_minutes_clamped(base: time, minutes: int) -> time:
    total = base.hour * 60 + base.minute + minutes
    total = max(0, min(total, 23 * 60 + 59))
    return time(total // 60, total % 60)


def _window_for_day(
    base_window: SessionWindow, day: date, half_days: Mapping[date, time] | None
) -> SessionWindow:
    half = None if half_days is None else half_days.get(day)
    if half is None:
        return base_window
    margin = _minutes_between(base_window.close, base_window.post_close_end)
    post_end = _add_minutes_clamped(half, margin)
    return SessionWindow(
        pre_open=base_window.pre_open,
        open=base_window.open,
        close=half,
        post_close_end=post_end,
    )


@dataclass(frozen=True)
class ExplicitMarketScheduleProvider:
    """명시적 schedule만 거래일로 인정하는 production 계약 공급자.

    - schedule에 있는 날짜만 session 생성(평일이라도 schedule 누락 → UNKNOWN/CALENDAR_MISSING)
    - 주말(weekend_weekdays)은 schedule 없어도 CLOSED 가능
    - constructor 이후 원본 mapping 변경이 내부 상태에 영향 없음(defensive copy/freeze)
    - malformed schedule(시간 순서 위반)은 MarketSessionError로 fail-closed
    """

    timezone: ZoneInfo
    schedule: Mapping[date, SessionWindow]
    weekend_weekdays: frozenset[int] = frozenset({5, 6})

    def __post_init__(self) -> None:
        # defensive copy + freeze: 원본 dict 변경이 내부에 전파되지 않게 한다.
        frozen: dict[date, SessionWindow] = {}
        for day, window in self.schedule.items():
            if not isinstance(window, SessionWindow):
                raise MarketSessionError("schedule values must be SessionWindow instances.")
            frozen[day] = window
        object.__setattr__(self, "_frozen_schedule", MappingProxyType(frozen))

    @property
    def _schedule(self) -> MappingProxyType[date, SessionWindow]:
        return self._frozen_schedule  # type: ignore[attr-defined]

    def is_trading_day(self, market: Market, day: date) -> bool:  # noqa: ARG002
        return day in self._schedule

    def session_at(self, market: Market, instant: datetime) -> MarketSession:
        if instant.tzinfo is None:
            raise MarketSessionError("session_at requires a timezone-aware instant.")
        local = instant.astimezone(self.timezone)
        local_day = local.date()

        if local_day not in self._schedule:
            # 주말은 schedule 없어도 CLOSED. 평일 누락은 UNKNOWN/CALENDAR_MISSING.
            if local_day.weekday() in self.weekend_weekdays:
                return MarketSession(
                    market=market,
                    state=MarketSessionState.CLOSED,
                    as_of=instant,
                )
            return MarketSession(
                market=market,
                state=MarketSessionState.UNKNOWN,
                as_of=instant,
                calendar_reason=CalendarReason.CALENDAR_MISSING,
            )

        window = self._schedule[local_day]
        now_t = local.timetz().replace(tzinfo=None)
        state = _state_from_window(window, now_t)
        return MarketSession(market=market, state=state, as_of=instant)


@dataclass(frozen=True)
class FixtureMarketCalendar:
    """**테스트 전용** 결정론적 fixture 캘린더. production 경로에서 사용 금지.

    휴장일/반장/주말 집합과 세션 윈도우를 주입받으며, 주입되지 않은 평일은 자동 거래일로
    취급한다(fail-open). 실제 운영은 `ExplicitMarketScheduleProvider`를 쓴다.
    """

    timezone: ZoneInfo
    window: SessionWindow
    holidays: frozenset[date] = frozenset()
    half_days: Mapping[date, time] | None = None
    weekend_weekdays: frozenset[int] = frozenset({5, 6})

    @classmethod
    def for_krx(
        cls,
        *,
        holidays: Iterable[date] = (),
        half_days: Mapping[date, time] | None = None,
    ) -> FixtureMarketCalendar:
        """KRX 정규장 기본값(잠정): pre_open 08:30, open 09:00, close 15:30, post_close_end 16:00.

        **테스트 전용.** 잠정 시각이며 live smoke evidence로 보정 후 확정한다.
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

    def is_trading_day(self, market: Market, day: date) -> bool:  # noqa: ARG002
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

        window = _window_for_day(self.window, local_day, self.half_days)
        now_t = local.timetz().replace(tzinfo=None)
        state = _state_from_window(window, now_t)
        return MarketSession(market=market, state=state, as_of=instant)


def build_explicit_schedule(
    *,
    timezone: ZoneInfo,
    trading_days: Iterable[date],
    window: SessionWindow,
    half_days: Mapping[date, time] | None = None,
) -> ExplicitMarketScheduleProvider:
    """거래일 집합과 공통 window로 explicit schedule을 만든다(테스트/CLI 헬퍼)."""
    schedule: dict[date, SessionWindow] = {}
    for day in trading_days:
        schedule[day] = _window_for_day(window, day, half_days)
    return ExplicitMarketScheduleProvider(timezone=timezone, schedule=schedule)


__all__ = [
    "CalendarReason",
    "ExplicitMarketScheduleProvider",
    "FixtureMarketCalendar",
    "MarketCalendarProvider",
    "MarketSession",
    "MarketSessionError",
    "MarketSessionState",
    "SessionWindow",
    "build_explicit_schedule",
]
