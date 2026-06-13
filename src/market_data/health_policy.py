"""RTM-7b.2 — transport-health / market-data-health 분리 정책 (순수, asyncio 없음).

두 건강 계열을 하나의 budget으로 섞지 않는다.

- transport-health: 연결/ACK/ping-pong/uptime/reconnect/flapping — market starvation은 transport를
  UNHEALTHY로 만들지 않는다.
- market-data-health: quote/trade freshness/장중 starvation — transport restart 사유가 아니다.

핵심 규칙:
| 시장 상태 | heartbeat만 | quote 없음(OPEN) |
| 장외      | transport 정상 가능 | NOT_EXPECTED |
| 장중      | transport 정상 가능 | STARVED/STALE → HOLD_EXECUTION_ONLY |
| transport 결함 | RESTART_TRANSPORT | market-data 별도 |

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


# transport-health에 관여하는 kind(나머지는 UNKNOWN_KIND).
_KIND_CONNECTED = "connected"
_KIND_ALL_SUBSCRIBED = "all_subscribed"
_KIND_DISCONNECT = "disconnect"
_KIND_PING = "ping_received"
_KIND_PONG = "pong_sent"
_TRANSPORT_KINDS = frozenset(
    {_KIND_CONNECTED, _KIND_ALL_SUBSCRIBED, _KIND_DISCONNECT, _KIND_PING, _KIND_PONG}
)

# market-data freshness에 관여하는 event_type.
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
class HealthThresholds:
    """잠정 임계값. 모두 양수이며 caller가 명시해야 한다(hidden production default 금지)."""

    heartbeat_timeout_seconds: float
    minimum_stable_uptime_seconds: float
    reconnect_window_seconds: float
    max_connects_in_window: int
    flapping_min_uptime_seconds: float
    flapping_min_market_events: int
    quote_grace_seconds: float
    quote_starvation_seconds: float
    max_quote_age_seconds: float

    def __post_init__(self) -> None:
        for name, value in (
            ("heartbeat_timeout_seconds", self.heartbeat_timeout_seconds),
            ("minimum_stable_uptime_seconds", self.minimum_stable_uptime_seconds),
            ("reconnect_window_seconds", self.reconnect_window_seconds),
            ("flapping_min_uptime_seconds", self.flapping_min_uptime_seconds),
            ("quote_grace_seconds", self.quote_grace_seconds),
            ("quote_starvation_seconds", self.quote_starvation_seconds),
            ("max_quote_age_seconds", self.max_quote_age_seconds),
        ):
            _reject_nonfinite(name, value)
            if value <= 0:
                raise HealthPolicyError(f"{name} must be > 0.")
        if isinstance(self.max_connects_in_window, bool) or self.max_connects_in_window < 1:
            raise HealthPolicyError("max_connects_in_window must be >= 1.")
        if isinstance(self.flapping_min_market_events, bool) or self.flapping_min_market_events < 0:
            raise HealthPolicyError("flapping_min_market_events must be >= 0.")


@dataclass(frozen=True)
class HealthVerdict:
    """한 평가 시점의 분리된 건강 판정. raw frame/credential을 담지 않는다."""

    transport: TransportHealthStatus
    market_data: MarketDataHealthStatus
    session_state: str
    reconnects_in_window: int
    last_quote_age_seconds: float | None
    reasons: tuple[str, ...]

    @property
    def is_healthy(self) -> bool:
        """strict healthy: transport HEALTHY ∧ market_data HEALTHY."""
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
        """실행 준비: strict healthy와 동일(보수적). WARMING/STARVED/STALE/INVALID/UNKNOWN 불가."""
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
    _connect_times: deque[datetime] = field(default_factory=deque, init=False)
    _disconnect_times: deque[datetime] = field(default_factory=deque, init=False)
    _last_connected_at: datetime | None = field(default=None, init=False)
    _last_disconnect_at: datetime | None = field(default=None, init=False)
    _last_pong_at: datetime | None = field(default=None, init=False)
    _last_transport_at: datetime | None = field(default=None, init=False)
    _last_quote_at: datetime | None = field(default=None, init=False)
    _last_trade_at: datetime | None = field(default=None, init=False)
    _last_market_at: datetime | None = field(default=None, init=False)
    _market_events_since_connect: int = field(default=0, init=False)
    _last_uptime_at_connect: datetime | None = field(default=None, init=False)

    def snapshot_keys(self) -> dict[str, datetime | None]:
        """테스트용: 거부 후 state 불변 검증."""
        return {
            "last_connected_at": self._last_connected_at,
            "last_quote_at": self._last_quote_at,
            "last_pong_at": self._last_pong_at,
            "connected": None,  # bool placeholder
        }

    def record_transport_event(self, *, kind: str, at: datetime, now: datetime) -> RecordResult:
        _require_tz_aware(at)
        _require_tz_aware(now)
        if kind not in _TRANSPORT_KINDS:
            return RecordResult.UNKNOWN_KIND
        if at > now:
            return RecordResult.FUTURE
        if self._last_transport_at is not None and at < self._last_transport_at:
            return RecordResult.OUT_OF_ORDER
        if kind in (_KIND_PING, _KIND_PONG) and self._last_pong_at == at:
            return RecordResult.DUPLICATE

        self._last_transport_at = at
        if kind == _KIND_CONNECTED:
            self._connected = True
            self._all_subscribed = False
            self._last_connected_at = at
            self._last_uptime_at_connect = at
            self._market_events_since_connect = 0
            self._connect_times.append(at)
        elif kind == _KIND_ALL_SUBSCRIBED:
            self._all_subscribed = True
        elif kind == _KIND_DISCONNECT:
            self._connected = False
            self._all_subscribed = False
            self._last_disconnect_at = at
            self._disconnect_times.append(at)
        elif kind in (_KIND_PING, _KIND_PONG):
            self._last_pong_at = at
        return RecordResult.RECORDED

    def record_market_event(self, *, event_type: str, at: datetime, now: datetime) -> RecordResult:
        _require_tz_aware(at)
        _require_tz_aware(now)
        if event_type not in _MARKET_KINDS:
            return RecordResult.UNKNOWN_KIND
        if at > now:
            return RecordResult.FUTURE
        if self._last_market_at is not None and at < self._last_market_at:
            return RecordResult.OUT_OF_ORDER

        self._last_market_at = at
        if event_type == _ET_QUOTE:
            self._last_quote_at = at
        elif event_type == _ET_TRADE:
            self._last_trade_at = at
        if event_type in (_ET_QUOTE, _ET_TRADE):
            self._market_events_since_connect += 1
        return RecordResult.RECORDED

    def evaluate(self, *, session: MarketSession, now: datetime) -> HealthVerdict:
        _require_tz_aware(now)
        reasons: list[str] = []
        reconnects = self._reconnects_in_window(now)

        transport = self._transport_status(session, reconnects, now, reasons)
        market_data, quote_age = self._market_data_status(session, now, reasons)

        return HealthVerdict(
            transport=transport,
            market_data=market_data,
            session_state=str(session.state),
            reconnects_in_window=reconnects,
            last_quote_age_seconds=quote_age,
            reasons=tuple(reasons),
        )

    def _reconnects_in_window(self, now: datetime) -> int:
        cutoff = now - timedelta(seconds=self.thresholds.reconnect_window_seconds)
        while self._connect_times and self._connect_times[0] < cutoff:
            self._connect_times.popleft()
        return len(self._connect_times)

    def _transport_status(
        self,
        session: MarketSession,
        reconnects: int,
        now: datetime,
        reasons: list[str],
    ) -> TransportHealthStatus:
        # ping/pong timeout — transport UNHEALTHY (market starvation과 무관).
        if self._last_pong_at is not None and self._connected:
            pong_age = (now - self._last_pong_at).total_seconds()
            if pong_age > self.thresholds.heartbeat_timeout_seconds:
                reasons.append("heartbeat_timeout")
                return TransportHealthStatus.UNHEALTHY
        elif self._connected and self._all_subscribed and self._last_connected_at is not None:
            # pong 기록 없으면 connected 이후 heartbeat_timeout 경과 시 unhealthy.
            since_connect = (now - self._last_connected_at).total_seconds()
            if since_connect > self.thresholds.heartbeat_timeout_seconds and self._last_pong_at is None:
                reasons.append("heartbeat_timeout_no_pong")
                return TransportHealthStatus.UNHEALTHY

        # flapping: 짧은 uptime + 적은 market event 반복 connect.
        if reconnects > self.thresholds.max_connects_in_window:
            reasons.append("transport_flapping")
            return TransportHealthStatus.FLAPPING

        if self._last_connected_at is None:
            reasons.append("transport_never_connected")
            return TransportHealthStatus.UNKNOWN

        if not self._connected:
            reasons.append("transport_disconnected")
            return TransportHealthStatus.UNHEALTHY

        if not self._all_subscribed:
            reasons.append("transport_subscriptions_incomplete")
            return TransportHealthStatus.UNHEALTHY

        uptime = (now - self._last_connected_at).total_seconds()
        if uptime < self.thresholds.minimum_stable_uptime_seconds:
            reasons.append("transport_warming")
            return TransportHealthStatus.WARMING

        # one-event-then-drop 패턴: 짧은 uptime + market event 부족.
        if (
            uptime < self.thresholds.flapping_min_uptime_seconds
            and self._market_events_since_connect < self.thresholds.flapping_min_market_events
            and reconnects >= 2
        ):
            reasons.append("transport_one_event_then_drop")
            return TransportHealthStatus.FLAPPING

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

        # OPEN인데 아직 quote 없음.
        if self._last_connected_at is None:
            reasons.append("quote_pending_no_connection")
            return MarketDataHealthStatus.UNKNOWN, None
        since_connect = (now - self._last_connected_at).total_seconds()
        if since_connect <= self.thresholds.quote_grace_seconds:
            reasons.append("quote_pending_grace")
            return MarketDataHealthStatus.WARMING, None
        reasons.append("quote_starvation_no_quote")
        return MarketDataHealthStatus.STARVED, None


# 테스트/CLI용 잠정 threshold 팩토리. production은 caller가 명시값을 전달한다.
def provisional_thresholds() -> HealthThresholds:
    return HealthThresholds(
        heartbeat_timeout_seconds=60.0,
        minimum_stable_uptime_seconds=300.0,
        reconnect_window_seconds=120.0,
        max_connects_in_window=3,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=30.0,
        max_quote_age_seconds=60.0,
    )


__all__ = [
    "HealthPolicyError",
    "HealthThresholds",
    "HealthVerdict",
    "MarketDataHealthStatus",
    "MarketHealthTracker",
    "RecordResult",
    "TransportHealthStatus",
    "provisional_thresholds",
]
