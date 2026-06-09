"""RTM-4b.1a — per-(market, symbol) bounded raw trade history store.

이 모듈은 RTM-4b 지표(이동평균/VWAP/롤링 거래량 등)의 기반이 되는 *원천 체결 history*만
종목별 단일 버퍼로 누적한다. 지표 계산·window 선택·readiness 판정은 하지 않는다(그건
indicators 레이어=RTM-4b.1b의 책임). 이 store는 어떤 IndicatorWindowSpec도 모르며
spec-agnostic하다.

핵심 계약(RTM-2 LatestMarketStateStore와 일치):
- observe()는 LatestMarketStateStore.apply()가 APPLIED로 판정한 trade tick만 받는 것을 전제로
  하되, 방어적으로 동일한 ordering 규칙을 재검증한다.
- 같은 stream(provider, channel): sequence 증가 + received_at/trade_at 비역행이면 APPLIED.
  trade_at가 같아도 sequence가 증가하면 APPLIED(여러 체결이 동일 거래소 timestamp를 가질 수 있다).
- 다른 stream: STREAM_MISMATCH로 거부하며 **history를 변경하지 않는다**(자동 reset 금지).
- 확인된 재접속 후 새 stream 수용은 monitor의 명시적 reset_stream() 호출로만 일어난다.

network/broker/ledger/LLM 접근이 없고, threading.Lock으로 observe/peek를 원자화한다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from domain._datetime import require_timezone_aware_datetime
from domain.enums import Market
from market_data.models import NormalizedTradeTick

__all__ = [
    "EpochStartReason",
    "RollingObserveResult",
    "RollingObserveStatus",
    "RollingRetentionPolicy",
    "RollingTradeHistoryStore",
    "TradeHistorySnapshot",
    "TradeSample",
]


class RollingObserveStatus(StrEnum):
    """observe 결과 상태. APPLIED가 아니면 history는 변하지 않았음을 보장한다."""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    STREAM_MISMATCH = "stream_mismatch"


@dataclass(frozen=True)
class RollingObserveResult:
    status: RollingObserveStatus
    reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.status is RollingObserveStatus.APPLIED


class EpochStartReason(StrEnum):
    """현재 연속(epoch)이 시작된 이유. gap 기반 불연속은 indicator 레이어가 판정하므로
    store는 INITIAL(최초 관찰)과 EXPLICIT_RESET(명시적 재접속 reset)만 구분한다."""

    INITIAL = "initial"
    EXPLICIT_RESET = "explicit_reset"


@dataclass(frozen=True)
class RollingRetentionPolicy:
    """전략 lookback과 무관한 *메모리 안전 상한*. caller가 반드시 명시한다(숨은 기본값 없음).

    indicator 레이어의 IndicatorWindowSpec은 이 cap을 초과하는 lookback을 요구할 수 없으며,
    cap 때문에 요청 window가 잘리면 INSUFFICIENT_RETENTION으로 fail-closed해야 한다(4b.1b).
    """

    hard_max_events: int
    hard_max_age_seconds: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.hard_max_events, bool) or not isinstance(self.hard_max_events, int):
            raise TypeError("hard_max_events must be an int.")
        if self.hard_max_events < 1:
            raise ValueError("hard_max_events must be >= 1.")
        if not isinstance(self.hard_max_age_seconds, Decimal):
            raise TypeError("hard_max_age_seconds must be a Decimal.")
        if not self.hard_max_age_seconds.is_finite() or self.hard_max_age_seconds <= 0:
            raise ValueError("hard_max_age_seconds must be a finite Decimal > 0.")
        # Decimal→float 변환은 정밀도를 잃고 큰 값에서 overflow하므로, microsecond 정수로
        # 정확히 표현 가능한 범위만 허용하고 그 결과를 결정론적으로 timedelta로 만든다.
        micros = self.hard_max_age_seconds * Decimal(1_000_000)
        if micros != micros.to_integral_value():
            raise ValueError("hard_max_age_seconds must be representable in whole microseconds.")
        try:
            timedelta(microseconds=int(micros))
        except (OverflowError, ValueError) as exc:
            raise ValueError("hard_max_age_seconds is out of timedelta range.") from exc

    @property
    def hard_max_age(self) -> timedelta:
        micros = int((self.hard_max_age_seconds * Decimal(1_000_000)).to_integral_value())
        return timedelta(microseconds=micros)


@dataclass(frozen=True)
class TradeSample:
    """history에 보관되는 단일 체결 표본(불변)."""

    price: Decimal
    quantity: Decimal
    trade_at: datetime
    received_at: datetime
    sequence: int


@dataclass(frozen=True)
class TradeHistorySnapshot:
    """(market, symbol)에 대한 bounded trade history의 불변 스냅샷.

    indicator 레이어는 samples + epoch/continuity 메타데이터 + retention truncation 메타데이터를
    보고 window 선택·readiness·coverage를 판정한다.
    """

    market: Market
    symbol: str
    samples: tuple[TradeSample, ...]
    retention: RollingRetentionPolicy
    provider: str | None
    channel: str | None
    was_ever_observed: bool
    continuity_epoch: int
    epoch_start_reason: EpochStartReason
    latest_sequence: int | None
    latest_event_time: datetime | None
    latest_received_at: datetime | None
    oldest_event_time: datetime | None
    evicted_event_count: int
    evicted_through_event_time: datetime | None
    retention_truncated: bool


@dataclass
class _SymbolState:
    """lock 안에서만 변경되는 종목별 가변 상태."""

    provider: str | None
    channel: str | None
    samples: list[TradeSample]
    was_ever_observed: bool
    continuity_epoch: int
    epoch_start_reason: EpochStartReason
    evicted_event_count: int
    evicted_through_event_time: datetime | None
    last_sequence: int | None
    last_event_time: datetime | None
    last_received_at: datetime | None


class RollingTradeHistoryStore:
    """APPLIED trade tick을 종목별 단일 bounded history로 누적한다.

    retention은 caller가 생성 시 명시한 hard cap만 적용한다. 전략 lookback 의미값은
    이 store가 알지 못한다.
    """

    def __init__(self, *, retention: RollingRetentionPolicy) -> None:
        self._retention = retention
        self._lock = threading.Lock()
        self._states: dict[tuple[Market, str], _SymbolState] = {}

    def observe(self, tick: NormalizedTradeTick, *, now: datetime) -> RollingObserveResult:
        # 정상 monitor 경로에서는 LatestMarketStateStore.apply()가 미래 이벤트를
        # FutureMarketEventError로 먼저 차단하므로 future tick은 여기 도달하지 않는다.
        # direct API 오용에 대한 방어로, RTM-2와 달리 예외 대신 OUT_OF_ORDER typed result로
        # fail-closed 거부한다(observe는 예외 없는 typed 계약을 유지).
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        if tick.trade_at > aware_now or tick.received_at > aware_now:
            return RollingObserveResult(RollingObserveStatus.OUT_OF_ORDER, "future event")

        key = (tick.market, tick.symbol)
        incoming_stream = (tick.provider_sequence.provider, tick.provider_sequence.channel)
        sample = TradeSample(
            price=tick.price,
            quantity=tick.quantity,
            trade_at=tick.trade_at,
            received_at=tick.received_at,
            sequence=tick.provider_sequence.sequence,
        )
        with self._lock:
            state = self._states.get(key)
            if state is None:
                self._states[key] = self._initial_state(incoming_stream, sample)
                return RollingObserveResult(RollingObserveStatus.APPLIED)

            if state.provider is None and state.channel is None:
                # 명시적 reset 직후 첫 event: 새 stream을 수용하되 epoch 메타데이터는 보존한다.
                self._adopt_after_reset(state, incoming_stream, sample)
                return RollingObserveResult(RollingObserveStatus.APPLIED)

            if (state.provider, state.channel) != incoming_stream:
                return RollingObserveResult(
                    RollingObserveStatus.STREAM_MISMATCH, "stream identity differs from current epoch"
                )

            assert state.last_sequence is not None  # provider 설정 시 항상 동반 설정됨
            if sample.sequence == state.last_sequence:
                return RollingObserveResult(RollingObserveStatus.DUPLICATE, "duplicate sequence")
            if sample.sequence < state.last_sequence:
                return RollingObserveResult(RollingObserveStatus.OUT_OF_ORDER, "decreasing sequence")
            assert state.last_received_at is not None
            if sample.received_at < state.last_received_at:
                return RollingObserveResult(RollingObserveStatus.OUT_OF_ORDER, "received_at regression")
            assert state.last_event_time is not None
            if sample.trade_at < state.last_event_time:
                return RollingObserveResult(RollingObserveStatus.OUT_OF_ORDER, "event time regression")

            self._append(state, sample)
            return RollingObserveResult(RollingObserveStatus.APPLIED)

    def reset_stream(self, provider: str, channel: str) -> None:
        """확인된 재접속 후 monitor가 명시 호출하는 epoch reset.

        해당 (provider, channel) stream identity를 가진 종목 history를 비우되 continuity 메타데이터
        (was_ever_observed, continuity_epoch)는 보존해 indicator 레이어가 DISCONTINUOUS를 관찰할 수
        있게 한다. 새 stream 수용은 다음 observe()에서 일어난다(자동 sequence reset 없음).
        """
        target = (provider, channel)
        with self._lock:
            for state in self._states.values():
                if (state.provider, state.channel) == target:
                    state.samples = []
                    state.provider = None
                    state.channel = None
                    state.continuity_epoch += 1
                    state.epoch_start_reason = EpochStartReason.EXPLICIT_RESET
                    state.evicted_event_count = 0
                    state.evicted_through_event_time = None
                    state.last_sequence = None
                    state.last_event_time = None
                    state.last_received_at = None

    def peek_history(self, market: Market, symbol: str, *, now: datetime) -> TradeHistorySnapshot:
        require_timezone_aware_datetime(now, field_name="now")
        key = (market, symbol)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return TradeHistorySnapshot(
                    market=market,
                    symbol=symbol,
                    samples=(),
                    retention=self._retention,
                    provider=None,
                    channel=None,
                    was_ever_observed=False,
                    continuity_epoch=0,
                    epoch_start_reason=EpochStartReason.INITIAL,
                    latest_sequence=None,
                    latest_event_time=None,
                    latest_received_at=None,
                    oldest_event_time=None,
                    evicted_event_count=0,
                    evicted_through_event_time=None,
                    retention_truncated=False,
                )
            samples = tuple(state.samples)
            oldest = samples[0].trade_at if samples else None
            return TradeHistorySnapshot(
                market=market,
                symbol=symbol,
                samples=samples,
                retention=self._retention,
                provider=state.provider,
                channel=state.channel,
                was_ever_observed=state.was_ever_observed,
                continuity_epoch=state.continuity_epoch,
                epoch_start_reason=state.epoch_start_reason,
                latest_sequence=state.last_sequence,
                latest_event_time=state.last_event_time,
                latest_received_at=state.last_received_at,
                oldest_event_time=oldest,
                evicted_event_count=state.evicted_event_count,
                evicted_through_event_time=state.evicted_through_event_time,
                retention_truncated=state.evicted_event_count > 0,
            )

    # --- internal (lock held by caller) ---

    def _initial_state(
        self, stream: tuple[str, str], sample: TradeSample
    ) -> _SymbolState:
        state = _SymbolState(
            provider=stream[0],
            channel=stream[1],
            samples=[sample],
            was_ever_observed=True,
            continuity_epoch=1,
            epoch_start_reason=EpochStartReason.INITIAL,
            evicted_event_count=0,
            evicted_through_event_time=None,
            last_sequence=sample.sequence,
            last_event_time=sample.trade_at,
            last_received_at=sample.received_at,
        )
        self._truncate(state)
        return state

    def _adopt_after_reset(
        self, state: _SymbolState, stream: tuple[str, str], sample: TradeSample
    ) -> None:
        state.provider = stream[0]
        state.channel = stream[1]
        state.samples = [sample]
        state.last_sequence = sample.sequence
        state.last_event_time = sample.trade_at
        state.last_received_at = sample.received_at
        self._truncate(state)

    def _append(self, state: _SymbolState, sample: TradeSample) -> None:
        state.samples.append(sample)
        state.last_sequence = sample.sequence
        state.last_event_time = sample.trade_at
        state.last_received_at = sample.received_at
        self._truncate(state)

    def _truncate(self, state: _SymbolState) -> None:
        # count cap: 최신 hard_max_events개만 유지(FIFO eviction).
        while len(state.samples) > self._retention.hard_max_events:
            removed = state.samples.pop(0)
            state.evicted_event_count += 1
            state.evicted_through_event_time = removed.trade_at
        # age cap: latest event_time 기준 hard_max_age를 넘은 표본 제거(최소 1개는 보존).
        if state.samples:
            cutoff = state.samples[-1].trade_at - self._retention.hard_max_age
            while len(state.samples) > 1 and state.samples[0].trade_at < cutoff:
                removed = state.samples.pop(0)
                state.evicted_event_count += 1
                state.evicted_through_event_time = removed.trade_at
