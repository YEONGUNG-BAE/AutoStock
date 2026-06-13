"""RTM-7b.2 — transport-health / market-data-health 분리 정책 (순수, asyncio 없음).

두 건강 계열을 하나의 budget으로 섞지 않는다.

- transport-health: 연결/ACK/ping-pong/uptime/completed-epoch flapping.
- market-data-health: quote/trade freshness/장중 starvation — transport restart 사유가 아니다.

threshold는 **잠정 configurable 값**이다. caller가 모두 명시해야 하며 hidden default가 없다.
이 모듈은 구체 transport/evidence 타입을 import하지 않는다(중립 시그널만 수용).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from market_data.market_session import MarketSession

# completed epoch history 상한(메모리 안전).
_MAX_EPOCH_HISTORY = 64


class HealthPolicyError(Exception):
    """health threshold 설정 위반."""


class RecordResult(StrEnum):
    """record_* 호출 결과. 거부 시 state는 변경되지 않는다."""

    RECORDED = "RECORDED"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    FUTURE = "FUTURE"
    UNKNOWN_KIND = "UNKNOWN_KIND"


class TransportHealthStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    WARMING = "WARMING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FLAPPING = "FLAPPING"
    UNHEALTHY = "UNHEALTHY"


class MarketDataHealthStatus(StrEnum):
    NOT_EXPECTED = "NOT_EXPECTED"
    WARMING = "WARMING"
    HEALTHY = "HEALTHY"
    STARVED = "STARVED"
    STALE = "STALE"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


_KIND_CONNECTED = "connected"
_KIND_ALL_SUBSCRIBED = "all_subscribed"
_KIND_DISCONNECT = "disconnect"
_KIND_PING = "ping_received"
_KIND_PONG = "pong_sent"
_TRANSPORT_KINDS = frozenset(
    {_KIND_CONNECTED, _KIND_ALL_SUBSCRIBED, _KIND_DISCONNECT, _KIND_PING, _KIND_PONG}
)

_ET_QUOTE = "best_bid_ask"
_ET_TRADE = "trade"
_ET_HEARTBEAT = "heartbeat"
_MARKET_KINDS = frozenset({_ET_QUOTE, _ET_TRADE, _ET_HEARTBEAT})


def _reject_nonfinite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HealthPolicyError(f"{name} must be a numeric type, not bool.")
    if not math.isfinite(value):
        raise HealthPolicyError(f"{name} must be finite.")


@dataclass(frozen=True)
class ConnectionEpochResult:
    """종료된 connection epoch 기록. flapping 판정은 completed epoch만 본다."""

    connected_at: datetime
    disconnected_at: datetime
    uptime_seconds: float
    market_event_count: int
    all_subscribed: bool
    disconnect_reason: str = "disconnect"


@dataclass(frozen=True)
class HealthThresholds:
    """잠정 임계값. 모두 양수이며 caller가 명시해야 한다(hidden production default 금지)."""

    subscription_grace_seconds: float
    heartbeat_timeout_seconds: float
    minimum_stable_uptime_seconds: float
    flapping_window_seconds: float
    flapping_max_short_epochs: int
    flapping_min_uptime_seconds: float
    flapping_min_market_events: int
    quote_grace_seconds: float
    quote_starvation_seconds: float
    max_quote_age_seconds: float

    def __post_init__(self) -> None:
        for name, value in (
            ("subscription_grace_seconds", self.subscription_grace_seconds),
            ("heartbeat_timeout_seconds", self.heartbeat_timeout_seconds),
            ("minimum_stable_uptime_seconds", self.minimum_stable_uptime_seconds),
            ("flapping_window_seconds", self.flapping_window_seconds),
            ("flapping_min_uptime_seconds", self.flapping_min_uptime_seconds),
            ("quote_grace_seconds", self.quote_grace_seconds),
            ("quote_starvation_seconds", self.quote_starvation_seconds),
            ("max_quote_age_seconds", self.max_quote_age_seconds),
        ):
            _reject_nonfinite(name, value)
            if value <= 0:
                raise HealthPolicyError(f"{name} must be > 0.")
        if isinstance(self.flapping_max_short_epochs, bool) or self.flapping_max_short_epochs < 1:
            raise HealthPolicyError("flapping_max_short_epochs must be >= 1.")
        if isinstance(self.flapping_min_market_events, bool) or self.flapping_min_market_events < 0:
            raise HealthPolicyError("flapping_min_market_events must be >= 0.")


@dataclass(frozen=True)
class HealthVerdict:
    """한 평가 시점의 분리된 건강 판정. raw frame/credential을 담지 않는다."""

    transport: TransportHealthStatus
    market_data: MarketDataHealthStatus
    session_state: str
    short_epochs_in_window: int
    last_quote_age_seconds: float | None
    reasons: tuple[str, ...]

    @property
    def is_healthy(self) -> bool:
        return (
            self.transport is TransportHealthStatus.HEALTHY
            and self.market_data is MarketDataHealthStatus.HEALTHY
        )

    @property
    def is_observable(self) -> bool:
        return self.transport not in (
            TransportHealthStatus.UNKNOWN,
            TransportHealthStatus.UNHEALTHY,
        )

    @property
    def is_warming(self) -> bool:
        return (
            self.transport is TransportHealthStatus.WARMING
            or self.market_data is MarketDataHealthStatus.WARMING
        )

    @property
    def is_execution_ready(self) -> bool:
        return self.is_healthy


def _require_tz_aware(at: datetime) -> None:
    if at.tzinfo is None:
        raise HealthPolicyError("timestamp must be timezone-aware.")


@dataclass
class MarketHealthTracker:
    """transport/market-data 중립 시그널을 누적하고 시점별 verdict를 산출하는 순수 상태기계."""

    thresholds: HealthThresholds
    _connected: bool = field(default=False, init=False)
    _all_subscribed: bool = field(default=False, init=False)
    _epoch_connected_at: datetime | None = field(default=None, init=False)
    _last_pong_at: datetime | None = field(default=None, init=False)
    _last_transport_at: datetime | None = field(default=None, init=False)
    _last_quote_at: datetime | None = field(default=None, init=False)
    _last_market_at: datetime | None = field(default=None, init=False)
    _market_events_in_epoch: int = field(default=0, init=False)
    _completed_epochs: deque[ConnectionEpochResult] = field(default_factory=deque, init=False)

    @property
    def all_subscribed(self) -> bool:
        return self._all_subscribed

    def record_transport_event(self, *, kind: str, at: datetime, now: datetime) -> RecordResult:
        _require_tz_aware(at)
        _require_tz_aware(now)
        if kind not in _TRANSPORT_KINDS:
            return RecordResult.UNKNOWN_KIND
        if at > now:
            return RecordResult.FUTURE
        # 새 epoch(connected) 이후 시각만 수용 — 이전 epoch delayed event 거부.
        if self._epoch_connected_at is not None and at < self._epoch_connected_at:
            if kind != _KIND_CONNECTED:
                return RecordResult.OUT_OF_ORDER
        if self._last_transport_at is not None and at < self._last_transport_at:
            return RecordResult.OUT_OF_ORDER
        if kind in (_KIND_PING, _KIND_PONG) and self._last_pong_at == at:
            return RecordResult.DUPLICATE

        if kind == _KIND_CONNECTED:
            if self._connected and self._epoch_connected_at is not None:
                self._finalize_epoch(at, reason="superseded")
            self._begin_epoch(at)
        elif kind == _KIND_ALL_SUBSCRIBED:
            if not self._connected:
                return RecordResult.OUT_OF_ORDER
            self._all_subscribed = True
        elif kind == _KIND_DISCONNECT:
            if not self._connected:
                return RecordResult.OUT_OF_ORDER
            self._finalize_epoch(at, reason="disconnect")
            self._connected = False
            self._all_subscribed = False
        elif kind in (_KIND_PING, _KIND_PONG):
            if not self._connected:
                return RecordResult.OUT_OF_ORDER
            self._last_pong_at = at

        self._last_transport_at = at
        return RecordResult.RECORDED

    def record_market_event(self, *, event_type: str, at: datetime, now: datetime) -> RecordResult:
        _require_tz_aware(at)
        _require_tz_aware(now)
        if event_type not in _MARKET_KINDS:
            return RecordResult.UNKNOWN_KIND
        if at > now:
            return RecordResult.FUTURE
        if self._epoch_connected_at is not None and at < self._epoch_connected_at:
            return RecordResult.OUT_OF_ORDER
        if self._last_market_at is not None and at < self._last_market_at:
            return RecordResult.OUT_OF_ORDER

        self._last_market_at = at
        if event_type == _ET_QUOTE:
            self._last_quote_at = at
        if event_type in (_ET_QUOTE, _ET_TRADE):
            self._market_events_in_epoch += 1
        return RecordResult.RECORDED

    def evaluate(self, *, session: MarketSession, now: datetime) -> HealthVerdict:
        _require_tz_aware(now)
        reasons: list[str] = []
        short_epochs = self._short_epochs_in_window(now)

        transport = self._transport_status(now, reasons, short_epochs)
        market_data, quote_age = self._market_data_status(session, now, reasons)

        return HealthVerdict(
            transport=transport,
            market_data=market_data,
            session_state=str(session.state),
            short_epochs_in_window=short_epochs,
            last_quote_age_seconds=quote_age,
            reasons=tuple(reasons),
        )

    def _begin_epoch(self, at: datetime) -> None:
        """새 connection epoch — 이전 pong/heartbeat를 새 연결 health에 쓰지 않는다."""
        self._connected = True
        self._all_subscribed = False
        self._epoch_connected_at = at
        self._last_pong_at = None
        self._market_events_in_epoch = 0

    def _finalize_epoch(self, at: datetime, *, reason: str) -> None:
        if self._epoch_connected_at is None:
            return
        uptime = max(0.0, (at - self._epoch_connected_at).total_seconds())
        result = ConnectionEpochResult(
            connected_at=self._epoch_connected_at,
            disconnected_at=at,
            uptime_seconds=uptime,
            market_event_count=self._market_events_in_epoch,
            all_subscribed=self._all_subscribed,
            disconnect_reason=reason,
        )
        self._completed_epochs.append(result)
        while len(self._completed_epochs) > _MAX_EPOCH_HISTORY:
            self._completed_epochs.popleft()
        self._epoch_connected_at = None
        self._market_events_in_epoch = 0

    def _trim_epochs(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.thresholds.flapping_window_seconds)
        while self._completed_epochs and self._completed_epochs[0].disconnected_at < cutoff:
            self._completed_epochs.popleft()

    def _is_short_unstable(self, epoch: ConnectionEpochResult) -> bool:
        # short-unstable = uptime 부족 AND event 부족. 둘 중 하나라도 충분하면 안정 근거로 본다
        # (긴 안정 연결의 적은 quote, 또는 짧지만 이벤트가 충분한 연결은 flapping 아님).
        return (
            epoch.uptime_seconds < self.thresholds.flapping_min_uptime_seconds
            and epoch.market_event_count < self.thresholds.flapping_min_market_events
        )

    def _short_epochs_in_window(self, now: datetime) -> int:
        self._trim_epochs(now)
        return sum(1 for e in self._completed_epochs if self._is_short_unstable(e))

    def _transport_status(
        self,
        now: datetime,
        reasons: list[str],
        short_epochs: int,
    ) -> TransportHealthStatus:
        # completed-epoch flapping은 WARMING보다 먼저 판정한다.
        if short_epochs >= self.thresholds.flapping_max_short_epochs:
            reasons.append("transport_flapping_epochs")
            return TransportHealthStatus.FLAPPING

        if self._epoch_connected_at is None and not self._connected:
            if self._completed_epochs:
                reasons.append("transport_disconnected")
                return TransportHealthStatus.UNHEALTHY
            reasons.append("transport_never_connected")
            return TransportHealthStatus.UNKNOWN

        since_connect = (now - self._epoch_connected_at).total_seconds()

        if not self._all_subscribed:
            if since_connect <= self.thresholds.subscription_grace_seconds:
                reasons.append("subscription_pending_grace")
                return TransportHealthStatus.WARMING
            reasons.append("subscription_grace_exceeded")
            return TransportHealthStatus.UNHEALTHY

        # pong timeout — 현재 epoch의 pong만 본다(이전 epoch pong 미사용).
        if self._last_pong_at is not None:
            pong_age = (now - self._last_pong_at).total_seconds()
            if pong_age > self.thresholds.heartbeat_timeout_seconds:
                reasons.append("heartbeat_timeout")
                return TransportHealthStatus.UNHEALTHY
        elif since_connect > self.thresholds.heartbeat_timeout_seconds:
            reasons.append("heartbeat_timeout_no_pong")
            return TransportHealthStatus.UNHEALTHY

        if since_connect < self.thresholds.minimum_stable_uptime_seconds:
            reasons.append("transport_warming")
            return TransportHealthStatus.WARMING

        return TransportHealthStatus.HEALTHY

    def _market_data_status(
        self, session: MarketSession, now: datetime, reasons: list[str]
    ) -> tuple[MarketDataHealthStatus, float | None]:
        if not session.is_tradable_quote_expected:
            return MarketDataHealthStatus.NOT_EXPECTED, None

        quote_age = (
            None if self._last_quote_at is None else (now - self._last_quote_at).total_seconds()
        )

        if self._last_quote_at is not None:
            if quote_age is not None and quote_age > self.thresholds.max_quote_age_seconds:
                reasons.append("quote_stale")
                return MarketDataHealthStatus.STALE, quote_age
            if quote_age is not None and quote_age > self.thresholds.quote_starvation_seconds:
                reasons.append("quote_starvation")
                return MarketDataHealthStatus.STARVED, quote_age
            return MarketDataHealthStatus.HEALTHY, quote_age

        if self._epoch_connected_at is None:
            reasons.append("quote_pending_no_connection")
            return MarketDataHealthStatus.UNKNOWN, None
        since_connect = (now - self._epoch_connected_at).total_seconds()
        if since_connect <= self.thresholds.quote_grace_seconds:
            reasons.append("quote_pending_grace")
            return MarketDataHealthStatus.WARMING, None
        reasons.append("quote_starvation_no_quote")
        return MarketDataHealthStatus.STARVED, None


def provisional_thresholds() -> HealthThresholds:
    return HealthThresholds(
        subscription_grace_seconds=30.0,
        heartbeat_timeout_seconds=60.0,
        minimum_stable_uptime_seconds=300.0,
        flapping_window_seconds=120.0,
        flapping_max_short_epochs=3,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=30.0,
        max_quote_age_seconds=60.0,
    )


__all__ = [
    "ConnectionEpochResult",
    "HealthPolicyError",
    "HealthThresholds",
    "HealthVerdict",
    "MarketDataHealthStatus",
    "MarketHealthTracker",
    "RecordResult",
    "TransportHealthStatus",
    "provisional_thresholds",
]
