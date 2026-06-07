from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from domain._datetime import require_timezone_aware_datetime
from domain.enums import Market

from market_data.models import (
    MarketEvent,
    MarketEventType,
    MarketHeartbeat,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
)

# quote/trade가 stale로 간주되기까지의 기본 허용 age. paper 단계 보수적 단일 기본값이며
# 추후 settings로 튜닝 가능하다.
DEFAULT_MAX_AGE = timedelta(seconds=10)

# require_fresh 기본 요구 집합: 실행경로는 보통 trade+quote 모두 fresh를 요구한다.
DEFAULT_REQUIRED: frozenset[MarketEventType] = frozenset(
    {MarketEventType.TRADE, MarketEventType.BEST_BID_ASK}
)


class MarketStateError(Exception):
    """latest market-state store 계열 공통 예외."""


class FutureMarketEventError(MarketStateError):
    """이벤트 시각이 now보다 미래인 계약 위반(apply 시점 fail-closed)."""


class StaleMarketStateError(MarketStateError):
    """요구한 slot이 존재하지만 freshness 기준을 넘어 stale인 경우."""


class MissingMarketStateError(MarketStateError):
    """요구한 slot에 아직 상태가 없는 경우."""


class ApplyStatus(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    STREAM_MISMATCH = "stream_mismatch"


@dataclass(frozen=True)
class ApplyResult:
    """apply 결과. APPLIED가 아니면 내부 state는 변하지 않았음을 보장한다."""

    status: ApplyStatus
    event_type: MarketEventType
    reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.status is ApplyStatus.APPLIED


@dataclass(frozen=True)
class MarketStateFreshnessPolicy:
    """event-time 기준 freshness 정책. now는 주입식 tz-aware datetime이다."""

    max_age: timedelta = DEFAULT_MAX_AGE

    def is_fresh(self, event_time: datetime, *, now: datetime) -> bool:
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        if event_time > aware_now:
            return False
        return (aware_now - event_time) <= self.max_age


@dataclass(frozen=True)
class LatestMarketStateSnapshot:
    """(market, symbol)에 대한 최신 trade/quote 불변 스냅샷."""

    market: Market
    symbol: str
    trade: NormalizedTradeTick | None
    quote: NormalizedBestBidAsk | None
    trade_fresh: bool
    quote_fresh: bool


@dataclass(frozen=True)
class LivenessSnapshot:
    """(provider, channel) heartbeat liveness 불변 스냅샷."""

    provider: str
    channel: str
    heartbeat: MarketHeartbeat | None
    is_live: bool


def _stream_identity(event: NormalizedTradeTick | NormalizedBestBidAsk) -> tuple[str, str]:
    sequence = event.provider_sequence
    return (sequence.provider, sequence.channel)


def _event_time(event: NormalizedTradeTick | NormalizedBestBidAsk) -> datetime:
    if isinstance(event, NormalizedTradeTick):
        return event.trade_at
    return event.quote_at


class LatestMarketStateStore:
    """정규화 MarketEvent의 최신 상태를 (market, symbol)별 trade/quote slot으로 유지한다.

    network/broker/ledger 접근이 없으며, threading.Lock으로 check-and-update를 원자화한다.
    이미 frozen인 이벤트 모델을 그대로 보관하므로 반환 스냅샷도 불변이다.
    """

    def __init__(self, *, freshness_policy: MarketStateFreshnessPolicy | None = None) -> None:
        self._policy = freshness_policy or MarketStateFreshnessPolicy()
        self._lock = threading.Lock()
        self._trade: dict[tuple[Market, str], NormalizedTradeTick] = {}
        self._quote: dict[tuple[Market, str], NormalizedBestBidAsk] = {}
        self._liveness: dict[tuple[str, str], MarketHeartbeat] = {}

    def apply(self, event: MarketEvent, *, now: datetime) -> ApplyResult:
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        if isinstance(event, NormalizedTradeTick):
            return self._apply_market(event, MarketEventType.TRADE, self._trade, aware_now)
        if isinstance(event, NormalizedBestBidAsk):
            return self._apply_market(event, MarketEventType.BEST_BID_ASK, self._quote, aware_now)
        if isinstance(event, MarketHeartbeat):
            return self._apply_heartbeat(event, aware_now)
        raise TypeError("apply only accepts normalized MarketEvent instances.")

    def _apply_market(
        self,
        event: NormalizedTradeTick | NormalizedBestBidAsk,
        event_type: MarketEventType,
        store: dict[tuple[Market, str], object],
        now: datetime,
    ) -> ApplyResult:
        event_time = _event_time(event)
        if event_time > now:
            raise FutureMarketEventError(f"{event_type.value} event time is in the future.")
        if event.received_at > now:
            raise FutureMarketEventError(f"{event_type.value} received_at is in the future.")

        key = (event.market, event.symbol)
        incoming_stream = _stream_identity(event)
        with self._lock:
            stored = store.get(key)
            if stored is not None:
                if _stream_identity(stored) != incoming_stream:  # type: ignore[arg-type]
                    return ApplyResult(
                        ApplyStatus.STREAM_MISMATCH, event_type, "stream identity differs from stored slot"
                    )
                stored_sequence = stored.provider_sequence.sequence  # type: ignore[attr-defined]
                incoming_sequence = event.provider_sequence.sequence
                if incoming_sequence == stored_sequence:
                    return ApplyResult(ApplyStatus.DUPLICATE, event_type, "duplicate sequence")
                if incoming_sequence < stored_sequence:
                    return ApplyResult(ApplyStatus.OUT_OF_ORDER, event_type, "decreasing sequence")
                if event.received_at < stored.received_at:  # type: ignore[attr-defined]
                    return ApplyResult(ApplyStatus.OUT_OF_ORDER, event_type, "received_at regression")
                if event_time < _event_time(stored):  # type: ignore[arg-type]
                    return ApplyResult(ApplyStatus.OUT_OF_ORDER, event_type, "event time regression")
            store[key] = event
            return ApplyResult(ApplyStatus.APPLIED, event_type)

    def _apply_heartbeat(self, event: MarketHeartbeat, now: datetime) -> ApplyResult:
        if event.received_at > now:
            raise FutureMarketEventError("heartbeat received_at is in the future.")
        key = (event.provider, event.channel)
        with self._lock:
            stored = self._liveness.get(key)
            if stored is not None:
                if event.received_at < stored.received_at:
                    return ApplyResult(
                        ApplyStatus.OUT_OF_ORDER, MarketEventType.HEARTBEAT, "received_at regression"
                    )
                if event.received_at == stored.received_at:
                    return ApplyResult(
                        ApplyStatus.DUPLICATE, MarketEventType.HEARTBEAT, "duplicate received_at"
                    )
            self._liveness[key] = event
            return ApplyResult(ApplyStatus.APPLIED, MarketEventType.HEARTBEAT)

    def peek(self, market: Market, symbol: str, *, now: datetime) -> LatestMarketStateSnapshot:
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        key = (market, symbol)
        with self._lock:
            trade = self._trade.get(key)
            quote = self._quote.get(key)
        trade_fresh = trade is not None and self._policy.is_fresh(trade.trade_at, now=aware_now)
        quote_fresh = quote is not None and self._policy.is_fresh(quote.quote_at, now=aware_now)
        return LatestMarketStateSnapshot(
            market=market,
            symbol=symbol,
            trade=trade,
            quote=quote,
            trade_fresh=trade_fresh,
            quote_fresh=quote_fresh,
        )

    def require_fresh(
        self,
        market: Market,
        symbol: str,
        *,
        now: datetime,
        required: frozenset[MarketEventType] = DEFAULT_REQUIRED,
    ) -> LatestMarketStateSnapshot:
        if not required:
            raise ValueError("require_fresh requires at least one market event type.")
        for event_type in required:
            if event_type not in (MarketEventType.TRADE, MarketEventType.BEST_BID_ASK):
                raise ValueError("require_fresh only supports TRADE and BEST_BID_ASK event types.")
        snapshot = self.peek(market, symbol, now=now)
        for event_type in required:
            if event_type is MarketEventType.TRADE:
                self._require_slot(snapshot.trade, snapshot.trade_fresh, market, symbol, "trade")
            else:
                self._require_slot(snapshot.quote, snapshot.quote_fresh, market, symbol, "quote")
        return snapshot

    @staticmethod
    def _require_slot(
        slot: object | None,
        fresh: bool,
        market: Market,
        symbol: str,
        label: str,
    ) -> None:
        if slot is None:
            raise MissingMarketStateError(
                f"No {label} state for market={market.value} symbol={symbol}."
            )
        if not fresh:
            raise StaleMarketStateError(
                f"{label} state for market={market.value} symbol={symbol} is stale."
            )

    def peek_liveness(self, provider: str, channel: str, *, now: datetime) -> LivenessSnapshot:
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        key = (provider, channel)
        with self._lock:
            beat = self._liveness.get(key)
        is_live = beat is not None and self._policy.is_fresh(beat.received_at, now=aware_now)
        return LivenessSnapshot(provider=provider, channel=channel, heartbeat=beat, is_live=is_live)

    def require_live(self, provider: str, channel: str, *, now: datetime) -> LivenessSnapshot:
        snapshot = self.peek_liveness(provider, channel, now=now)
        if snapshot.heartbeat is None:
            raise MissingMarketStateError(
                f"No heartbeat liveness for provider={provider} channel={channel}."
            )
        if not snapshot.is_live:
            raise StaleMarketStateError(
                f"heartbeat liveness for provider={provider} channel={channel} is stale."
            )
        return snapshot


__all__ = [
    "DEFAULT_MAX_AGE",
    "DEFAULT_REQUIRED",
    "ApplyResult",
    "ApplyStatus",
    "FutureMarketEventError",
    "LatestMarketStateSnapshot",
    "LatestMarketStateStore",
    "LivenessSnapshot",
    "MarketStateError",
    "MarketStateFreshnessPolicy",
    "MissingMarketStateError",
    "StaleMarketStateError",
]
