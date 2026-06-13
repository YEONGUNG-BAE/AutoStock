"""RTM-7b.2 — transport-health / market-data-health 분리 정책 (순수, asyncio 없음).

두 건강 계열을 하나의 budget으로 섞지 않는다.

- transport-health: 연결/ACK 완료/ping-pong/uptime/reconnect 빈도/flapping.
- market-data-health: 마지막 quote·trade 수신 시각/freshness/장중 starvation.

핵심 규칙(operator 표):
| 시장 상태 | heartbeat만 | quote 없음 |
| 장외      | transport 정상 가능 | 정상 |
| 장중      | transport 정상 가능 | market-data unhealthy |
| 장중 반복 reconnect | unhealthy | unhealthy |

threshold는 **잠정 configurable 값**이다. 월요일 live smoke evidence로 보정 후 확정한다.

이 모듈은 구체 transport/evidence 타입을 import하지 않는다(중립 시그널만 수용). supervisor가
`KisWsTransportEvent.kind`/`MonitorEvidence`를 이 tracker의 record_* 호출로 어댑트한다. 그래서
market_data → data/broker/ledger 역의존이 생기지 않는다. datetime/deque/enum만 쓴다.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from market_data.market_session import MarketSession


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"  # 판단 근거가 아직 없음(연결 전/그레이스 구간)


class HealthPolicyError(Exception):
    """health threshold 설정 위반."""


# transport-health에 관여하는 transport event kind(나머지는 무시).
_KIND_CONNECTED = "connected"
_KIND_ALL_SUBSCRIBED = "all_subscribed"
_KIND_DISCONNECT = "disconnect"
_KIND_PING = "ping_received"
_KIND_PONG = "pong_sent"

# market-data freshness에 관여하는 event_type.
_ET_QUOTE = "best_bid_ask"
_ET_TRADE = "trade"
_ET_HEARTBEAT = "heartbeat"


@dataclass(frozen=True)
class HealthThresholds:
    """잠정 임계값. 모두 양수. live smoke evidence 후 확정."""

    quote_starvation_seconds: float = 30.0
    reconnect_window_seconds: float = 120.0
    max_connects_in_window: int = 3  # window 내 connect 횟수 초과 시 flapping
    quote_grace_seconds: float = 30.0  # OPEN 직후 첫 quote 대기 허용(연결 기준)

    def __post_init__(self) -> None:
        if self.quote_starvation_seconds <= 0:
            raise HealthPolicyError("quote_starvation_seconds must be > 0.")
        if self.reconnect_window_seconds <= 0:
            raise HealthPolicyError("reconnect_window_seconds must be > 0.")
        if self.max_connects_in_window < 1:
            raise HealthPolicyError("max_connects_in_window must be >= 1.")
        if self.quote_grace_seconds <= 0:
            raise HealthPolicyError("quote_grace_seconds must be > 0.")


@dataclass(frozen=True)
class HealthVerdict:
    """한 평가 시점의 분리된 건강 판정. raw frame/credential을 담지 않는다."""

    transport: HealthStatus
    market_data: HealthStatus
    session_state: str
    reconnects_in_window: int
    last_quote_age_seconds: float | None
    reasons: tuple[str, ...]

    @property
    def is_healthy(self) -> bool:
        return self.transport is HealthStatus.HEALTHY and self.market_data in (
            HealthStatus.HEALTHY,
            HealthStatus.UNKNOWN,
        )


@dataclass
class MarketHealthTracker:
    """transport/market-data 시그널을 누적하고 시점별 verdict를 산출하는 순수 상태기계.

    record_* 는 supervisor가 주입한 중립 시그널(kind/event_type + at)만 받는다. evaluate는
    주입된 now와 MarketSession으로 판정하며 외부 I/O·clock·asyncio가 없다.
    """

    thresholds: HealthThresholds = field(default_factory=HealthThresholds)
    _connected: bool = field(default=False, init=False)
    _all_subscribed: bool = field(default=False, init=False)
    _connect_times: deque[datetime] = field(default_factory=deque, init=False)
    _last_connected_at: datetime | None = field(default=None, init=False)
    _last_pong_at: datetime | None = field(default=None, init=False)
    _last_quote_at: datetime | None = field(default=None, init=False)
    _last_trade_at: datetime | None = field(default=None, init=False)
    _last_heartbeat_at: datetime | None = field(default=None, init=False)

    def record_transport_event(self, *, kind: str, at: datetime) -> None:
        if kind == _KIND_CONNECTED:
            self._connected = True
            self._all_subscribed = False  # 새 epoch은 ACK를 다시 받아야 한다.
            self._last_connected_at = at
            self._connect_times.append(at)
        elif kind == _KIND_ALL_SUBSCRIBED:
            self._all_subscribed = True
        elif kind == _KIND_DISCONNECT:
            self._connected = False
            self._all_subscribed = False
        elif kind in (_KIND_PING, _KIND_PONG):
            self._last_pong_at = at

    def record_market_event(self, *, event_type: str, at: datetime) -> None:
        # freshness는 "프레임을 받았는가"의 신호이므로 apply_status와 무관하게 수신 시각을 쓴다.
        if event_type == _ET_QUOTE:
            self._last_quote_at = at
        elif event_type == _ET_TRADE:
            self._last_trade_at = at
        elif event_type == _ET_HEARTBEAT:
            self._last_heartbeat_at = at

    def evaluate(self, *, session: MarketSession, now: datetime) -> HealthVerdict:
        reasons: list[str] = []
        reconnects = self._reconnects_in_window(now)

        transport = self._transport_status(reconnects, reasons)
        market_data, quote_age = self._market_data_status(session, now, reasons)

        return HealthVerdict(
            transport=transport,
            market_data=market_data,
            session_state=str(session.state),
            reconnects_in_window=reconnects,
            last_quote_age_seconds=quote_age,
            reasons=tuple(reasons),
        )

    # --- transport ------------------------------------------------------------

    def _reconnects_in_window(self, now: datetime) -> int:
        cutoff = now - timedelta(seconds=self.thresholds.reconnect_window_seconds)
        while self._connect_times and self._connect_times[0] < cutoff:
            self._connect_times.popleft()
        return len(self._connect_times)

    def _transport_status(self, reconnects: int, reasons: list[str]) -> HealthStatus:
        if reconnects > self.thresholds.max_connects_in_window:
            reasons.append("transport_flapping")
            return HealthStatus.UNHEALTHY
        if self._last_connected_at is None:
            reasons.append("transport_never_connected")
            return HealthStatus.UNKNOWN
        if not self._connected:
            reasons.append("transport_disconnected")
            return HealthStatus.UNHEALTHY
        if not self._all_subscribed:
            reasons.append("transport_subscriptions_incomplete")
            return HealthStatus.UNHEALTHY
        return HealthStatus.HEALTHY

    # --- market data ----------------------------------------------------------

    def _market_data_status(
        self, session: MarketSession, now: datetime, reasons: list[str]
    ) -> tuple[HealthStatus, float | None]:
        quote_age = (
            None if self._last_quote_at is None else (now - self._last_quote_at).total_seconds()
        )
        # 장외(quote 미기대): heartbeat-only/quote 없음 모두 정상.
        if not session.is_tradable_quote_expected:
            return HealthStatus.HEALTHY, quote_age

        # 장중(OPEN): quote freshness를 본다.
        if self._last_quote_at is not None:
            if quote_age is not None and quote_age > self.thresholds.quote_starvation_seconds:
                reasons.append("quote_starvation")
                return HealthStatus.UNHEALTHY, quote_age
            return HealthStatus.HEALTHY, quote_age

        # OPEN인데 아직 quote가 한 건도 없다: 연결 기준 grace 안이면 UNKNOWN, 넘으면 starvation.
        if self._last_connected_at is None:
            reasons.append("quote_pending_no_connection")
            return HealthStatus.UNKNOWN, None
        since_connect = (now - self._last_connected_at).total_seconds()
        if since_connect > self.thresholds.quote_grace_seconds:
            reasons.append("quote_starvation_no_quote")
            return HealthStatus.UNHEALTHY, None
        reasons.append("quote_pending_grace")
        return HealthStatus.UNKNOWN, None


__all__ = [
    "HealthPolicyError",
    "HealthStatus",
    "HealthThresholds",
    "HealthVerdict",
    "MarketHealthTracker",
]
