from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from market_data.latest_state import (
    FutureMarketEventError,
    LatestMarketStateStore,
)
from market_data.models import (
    MarketEvent,
    MarketEventType,
    MarketHeartbeat,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
)
from market_data.protocols import MarketEventSource

__all__ = [
    "MonitorState",
    "MonitorEvidence",
    "MonitorSummary",
    "MonitorExhaustedError",
    "ReconnectPolicy",
    "MarketMonitor",
]


class MonitorState(StrEnum):
    IDLE = "idle"
    CONNECTING = "connecting"
    RUNNING = "running"
    BACKING_OFF = "backing_off"
    EXHAUSTED = "exhausted"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ReconnectPolicy:
    """deterministic 지수 백오프 정책. jitter는 RTM-3 기본 0(결정론).

    delay_for_attempt는 순수 함수이며 sleep/clock에 의존하지 않는다. 실제 sleep과
    clock은 monitor에 주입되므로 테스트는 실제 대기 없이 backoff를 검증할 수 있다.
    """

    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 30.0
    max_attempts: int = 5
    jitter_ratio: float = 0.0

    def delay_for_attempt(self, attempt: int) -> float:
        if attempt < 1:
            raise ValueError("attempt must be >= 1.")
        raw = self.initial_delay_seconds * (self.multiplier ** (attempt - 1))
        return min(raw, self.max_delay_seconds)


@dataclass(frozen=True)
class MonitorEvidence:
    """append-only evidence 한 건. raw frame/token/account/예외 repr은 절대 담지 않는다."""

    timestamp: datetime
    monitor_session_id: str
    state: MonitorState
    connection_attempt: int
    kind: str
    event_type: str | None = None
    provider: str | None = None
    channel: str | None = None
    market: str | None = None
    symbol: str | None = None
    sequence: int | None = None
    apply_status: str | None = None
    reason_code: str | None = None
    backoff_seconds: float | None = None


@dataclass
class _Counts:
    applied: int = 0
    duplicate: int = 0
    out_of_order: int = 0
    stream_mismatch: int = 0
    future_event_error: int = 0


@dataclass(frozen=True)
class MonitorSummary:
    monitor_session_id: str
    connection_attempts: int
    applied: int
    duplicate: int
    out_of_order: int
    stream_mismatch: int
    future_event_error: int
    final_state: MonitorState


class MonitorExhaustedError(Exception):
    """reconnect 시도를 max_attempts까지 모두 소진한 typed 실패. summary를 동반한다."""

    def __init__(self, summary: MonitorSummary) -> None:
        super().__init__(
            f"market monitor exhausted after {summary.connection_attempts} attempts."
        )
        self.summary = summary


def _evidence_meta(event: MarketEvent) -> dict[str, object | None]:
    if isinstance(event, (NormalizedTradeTick, NormalizedBestBidAsk)):
        seq = event.provider_sequence
        return {
            "event_type": event.event_type.value,
            "provider": seq.provider,
            "channel": seq.channel,
            "market": event.market.value,
            "symbol": event.symbol,
            "sequence": seq.sequence,
        }
    if isinstance(event, MarketHeartbeat):
        return {
            "event_type": MarketEventType.HEARTBEAT.value,
            "provider": event.provider,
            "channel": event.channel,
            "market": None,
            "symbol": None,
            "sequence": None,
        }
    raise TypeError("monitor only consumes normalized MarketEvent instances.")


def _stream_key(event: MarketEvent) -> tuple[str, str]:
    if isinstance(event, (NormalizedTradeTick, NormalizedBestBidAsk)):
        return (event.provider_sequence.provider, event.provider_sequence.channel)
    if isinstance(event, MarketHeartbeat):
        return (event.provider, event.channel)
    raise TypeError("monitor only consumes normalized MarketEvent instances.")


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class MarketMonitor:
    """fake/replay transport 위에서 도는 always-on 모니터 오케스트레이션.

    network/broker/ledger/trigger/LLM 접근이 없다. source_factory가 매 접속마다
    fresh MarketEventSource(=fresh sequence epoch)를 만들고, 이벤트를
    LatestMarketStateStore.apply로 흘려보내며, transport 단절 시 deterministic
    backoff로 재접속한다. 확인된 재접속에서만 store.reset_stream을 명시 호출한다.
    clock/sleep/session id/evidence sink는 모두 주입식이라 테스트가 결정론적이다.
    """

    def __init__(
        self,
        *,
        store: LatestMarketStateStore,
        source_factory: Callable[[], MarketEventSource],
        clock: Callable[[], datetime],
        policy: ReconnectPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        session_id: str,
        max_events: int | None = None,
        max_runtime_seconds: float | None = None,
        on_evidence: Callable[[MonitorEvidence], None] | None = None,
    ) -> None:
        self._store = store
        self._source_factory = source_factory
        self._clock = clock
        self._policy = policy or ReconnectPolicy()
        self._sleep = sleep or _default_sleep
        self._session_id = session_id
        self._max_events = max_events
        self._max_runtime_seconds = max_runtime_seconds
        self._on_evidence = on_evidence

        self._state = MonitorState.IDLE
        self._counts = _Counts()
        self._events_consumed = 0
        self._seen_streams: set[tuple[str, str]] = set()
        self._started_at: datetime | None = None

    @property
    def state(self) -> MonitorState:
        return self._state

    async def run(self) -> MonitorSummary:
        self._started_at = self._clock()
        attempt = 0
        had_connection = False
        try:
            while True:
                attempt += 1
                self._state = MonitorState.CONNECTING
                if had_connection:
                    self._reset_streams(attempt)
                self._emit("connect", attempt)
                source = self._source_factory()
                try:
                    async for event in source.events():
                        had_connection = True
                        self._state = MonitorState.RUNNING
                        self._consume(event, attempt)
                        if self._budget_exhausted():
                            self._state = MonitorState.STOPPED
                            self._emit("stop", attempt, reason_code="budget_reached")
                            return self._summary(attempt)
                    self._state = MonitorState.STOPPED
                    self._emit("eof", attempt)
                    return self._summary(attempt)
                except asyncio.CancelledError:
                    self._state = MonitorState.STOPPED
                    self._emit("cancelled", attempt)
                    raise
                except Exception:
                    self._emit("drop", attempt, reason_code="source_error")
                    if attempt >= self._policy.max_attempts:
                        self._state = MonitorState.EXHAUSTED
                        self._emit("exhausted", attempt)
                        raise MonitorExhaustedError(self._summary(attempt)) from None
                    delay = self._policy.delay_for_attempt(attempt)
                    self._state = MonitorState.BACKING_OFF
                    self._emit("backoff", attempt, backoff_seconds=delay)
                    await self._sleep(delay)
        finally:
            if self._state not in (
                MonitorState.STOPPED,
                MonitorState.EXHAUSTED,
            ):
                self._state = MonitorState.STOPPED

    def _consume(self, event: MarketEvent, attempt: int) -> None:
        meta = _evidence_meta(event)
        self._seen_streams.add(_stream_key(event))
        self._events_consumed += 1
        try:
            result = self._store.apply(event, now=self._clock())
        except FutureMarketEventError:
            self._counts.future_event_error += 1
            self._emit(
                "apply",
                attempt,
                apply_status="future_event_error",
                reason_code="future_event",
                meta=meta,
            )
            return
        status = result.status.value
        setattr(self._counts, status, getattr(self._counts, status) + 1)
        self._emit(
            "apply",
            attempt,
            apply_status=status,
            reason_code=result.reason,
            meta=meta,
        )

    def _reset_streams(self, attempt: int) -> None:
        for provider, channel in sorted(self._seen_streams):
            self._store.reset_stream(provider, channel)
            self._emit(
                "reset",
                attempt,
                provider=provider,
                channel=channel,
                reason_code="reconnect_stream_reset",
            )
        self._seen_streams.clear()

    def _budget_exhausted(self) -> bool:
        if self._max_events is not None and self._events_consumed >= self._max_events:
            return True
        if self._max_runtime_seconds is not None and self._started_at is not None:
            elapsed = (self._clock() - self._started_at).total_seconds()
            if elapsed >= self._max_runtime_seconds:
                return True
        return False

    def _summary(self, attempt: int) -> MonitorSummary:
        return MonitorSummary(
            monitor_session_id=self._session_id,
            connection_attempts=attempt,
            applied=self._counts.applied,
            duplicate=self._counts.duplicate,
            out_of_order=self._counts.out_of_order,
            stream_mismatch=self._counts.stream_mismatch,
            future_event_error=self._counts.future_event_error,
            final_state=self._state,
        )

    def _emit(
        self,
        kind: str,
        attempt: int,
        *,
        apply_status: str | None = None,
        reason_code: str | None = None,
        backoff_seconds: float | None = None,
        provider: str | None = None,
        channel: str | None = None,
        meta: dict[str, object | None] | None = None,
    ) -> None:
        if self._on_evidence is None:
            return
        fields: dict[str, object | None] = {
            "event_type": None,
            "provider": provider,
            "channel": channel,
            "market": None,
            "symbol": None,
            "sequence": None,
        }
        if meta is not None:
            fields.update(meta)
        evidence = MonitorEvidence(
            timestamp=self._clock(),
            monitor_session_id=self._session_id,
            state=self._state,
            connection_attempt=attempt,
            kind=kind,
            event_type=fields["event_type"],  # type: ignore[arg-type]
            provider=fields["provider"],  # type: ignore[arg-type]
            channel=fields["channel"],  # type: ignore[arg-type]
            market=fields["market"],  # type: ignore[arg-type]
            symbol=fields["symbol"],  # type: ignore[arg-type]
            sequence=fields["sequence"],  # type: ignore[arg-type]
            apply_status=apply_status,
            reason_code=reason_code,
            backoff_seconds=backoff_seconds,
        )
        self._on_evidence(evidence)
