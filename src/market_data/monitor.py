from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    "MonitorInternalError",
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
    """deterministic 지수 백오프 정책 (RTM-3는 jitter 없이 완전 결정론).

    delay_for_attempt는 순수 함수이며 sleep/clock에 의존하지 않는다. 실제 sleep과
    clock은 monitor에 주입되므로 테스트는 실제 대기 없이 backoff를 검증할 수 있다.
    생성자에서 인자 불변식을 검증해 잘못된 정책을 fail-closed로 막는다.
    """

    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 30.0
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be >= 0.")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1.")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds must be >= initial_delay_seconds.")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1.")

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


class MonitorInternalError(Exception):
    """transport 단절이 아닌 monitor 내부/저장소/evidence 결함.

    backoff·reconnect로 숨기지 않고 fail-closed로 즉시 전파한다. 실제 운영 결함을
    transport drop으로 오인해 무한 재접속하는 것을 막기 위한 경계 표식이다.
    """


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
    backoff로 재접속한다.

    reset 정책: 재접속을 시작하는 시점이 아니라, 새 source에서 해당 stream의 첫
    이벤트를 실제로 받은 직후(apply 직전)에만 그 stream을 reset한다. 새 source가
    첫 이벤트도 못 내고 죽으면 기존 state는 보존되고 freshness로 자연 stale 처리된다.

    오류 경계: source_factory/iterator 오류는 transport drop으로 보고 backoff·reconnect
    하지만, store.apply 등 내부 결함은 MonitorInternalError로 즉시 fail-closed 전파해
    운영 결함이 무한 재접속에 가려지지 않게 한다.

    watchdog: heartbeat_watch가 설정되면 다음 이벤트를 heartbeat_timeout 안에 받지
    못할 때 half-dead 연결로 간주해 drop·reconnect한다. max_runtime_seconds는 silent
    source에도 작동하도록 다음-이벤트 대기 자체에 timeout을 건다.

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
        heartbeat_watch: tuple[str, str] | None = None,
        heartbeat_timeout_seconds: float | None = None,
        on_evidence: Callable[[MonitorEvidence], None] | None = None,
    ) -> None:
        if max_events is not None and max_events < 1:
            raise ValueError("max_events must be >= 1 when set.")
        if max_runtime_seconds is not None and max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be > 0 when set.")
        if (heartbeat_watch is None) != (heartbeat_timeout_seconds is None):
            raise ValueError(
                "heartbeat_watch and heartbeat_timeout_seconds must be set together."
            )
        if heartbeat_timeout_seconds is not None and heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat_timeout_seconds must be > 0 when set.")

        self._store = store
        self._source_factory = source_factory
        self._clock = clock
        self._policy = policy or ReconnectPolicy()
        self._sleep = sleep or _default_sleep
        self._session_id = session_id
        self._max_events = max_events
        self._max_runtime_seconds = max_runtime_seconds
        self._heartbeat_watch = heartbeat_watch
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._on_evidence = on_evidence

        self._state = MonitorState.IDLE
        self._counts = _Counts()
        self._events_consumed = 0
        self._seen_streams: set[tuple[str, str]] = set()
        self._pending_reset: set[tuple[str, str]] = set()
        self._started_at: datetime | None = None
        self._epoch_started_at: datetime | None = None

    @property
    def state(self) -> MonitorState:
        return self._state

    async def run(self) -> MonitorSummary:
        self._started_at = self._clock()
        attempt = 0
        try:
            while True:
                attempt += 1
                self._state = MonitorState.CONNECTING
                self._epoch_started_at = self._clock()
                self._emit("connect", attempt)
                outcome = await self._run_attempt(attempt)
                if outcome in ("eof", "budget"):
                    self._state = MonitorState.STOPPED
                    return self._summary(attempt)
                # outcome == "drop" -> transport reconnect
                if attempt >= self._policy.max_attempts:
                    self._state = MonitorState.EXHAUSTED
                    self._emit("exhausted", attempt)
                    raise MonitorExhaustedError(self._summary(attempt))
                self._pending_reset |= set(self._seen_streams)
                delay = self._policy.delay_for_attempt(attempt)
                self._state = MonitorState.BACKING_OFF
                self._emit("backoff", attempt, backoff_seconds=delay)
                await self._sleep(delay)
        finally:
            if self._state not in (MonitorState.STOPPED, MonitorState.EXHAUSTED):
                self._state = MonitorState.STOPPED

    async def _run_attempt(self, attempt: int) -> str:
        """한 번의 접속 시도. 'eof'/'budget'/'drop' 중 하나를 반환한다.

        transport 오류(source_factory/__anext__/heartbeat-timeout)는 'drop'으로
        분류해 backoff·reconnect로 보낸다. 반면 _consume 내부 결함은
        MonitorInternalError로 이 메서드 밖으로 전파되어 fail-closed 종료된다.
        """
        try:
            iterator = self._source_factory().events().__aiter__()
        except asyncio.CancelledError:
            self._state = MonitorState.STOPPED
            self._emit("cancelled", attempt)
            raise
        except Exception:
            self._emit("drop", attempt, reason_code="source_error")
            return "drop"

        while True:
            timeout = self._next_event_timeout()
            try:
                if timeout is None:
                    event = await iterator.__anext__()
                else:
                    event = await asyncio.wait_for(iterator.__anext__(), timeout)
            except StopAsyncIteration:
                self._state = MonitorState.STOPPED
                self._emit("eof", attempt)
                return "eof"
            except asyncio.CancelledError:
                self._state = MonitorState.STOPPED
                self._emit("cancelled", attempt)
                raise
            except (asyncio.TimeoutError, TimeoutError):
                if self._runtime_exhausted():
                    self._state = MonitorState.STOPPED
                    self._emit("stop", attempt, reason_code="runtime_timeout")
                    return "budget"
                self._emit("drop", attempt, reason_code="heartbeat_stale")
                return "drop"
            except Exception:
                self._emit("drop", attempt, reason_code="source_error")
                return "drop"

            # 이벤트 처리(_consume)는 transport try 밖이다. 내부 결함은 여기서
            # MonitorInternalError로 전파되며 transport drop으로 오인되지 않는다.
            self._state = MonitorState.RUNNING
            self._consume(event, attempt)
            if self._max_events is not None and self._events_consumed >= self._max_events:
                self._state = MonitorState.STOPPED
                self._emit("stop", attempt, reason_code="budget_reached")
                return "budget"
            if self._runtime_exhausted():
                self._state = MonitorState.STOPPED
                self._emit("stop", attempt, reason_code="runtime_timeout")
                return "budget"

    def _consume(self, event: MarketEvent, attempt: int) -> None:
        meta = _evidence_meta(event)
        stream = _stream_key(event)
        # 확인된 재접속 후 이 stream의 첫 이벤트에서만 reset한다(apply 직전).
        if stream in self._pending_reset:
            try:
                self._store.reset_stream(*stream)
            except Exception as exc:
                raise MonitorInternalError("store.reset_stream failed") from exc
            self._emit(
                "reset",
                attempt,
                provider=stream[0],
                channel=stream[1],
                reason_code="reconnect_stream_reset",
            )
            self._pending_reset.discard(stream)
        self._seen_streams.add(stream)
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
        except MonitorInternalError:
            raise
        except Exception as exc:
            raise MonitorInternalError("store.apply failed") from exc
        status = result.status.value
        setattr(self._counts, status, getattr(self._counts, status) + 1)
        self._emit(
            "apply",
            attempt,
            apply_status=status,
            reason_code=result.reason,
            meta=meta,
        )

    def _next_event_timeout(self) -> float | None:
        """다음 이벤트를 기다릴 최대 시간(초). heartbeat watchdog/runtime budget이
        없으면 None(무한 대기). 주입 clock 기준 가장 가까운 deadline까지의 잔여시간을
        실제 wait_for timeout으로 사용한다(silent source도 종료 가능)."""
        now = self._clock()
        deadlines: list[datetime] = []
        if self._heartbeat_watch is not None and self._heartbeat_timeout_seconds is not None:
            deadlines.append(self._heartbeat_deadline(now))
        if self._max_runtime_seconds is not None and self._started_at is not None:
            deadlines.append(
                self._started_at + timedelta(seconds=self._max_runtime_seconds)
            )
        if not deadlines:
            return None
        remaining = (min(deadlines) - now).total_seconds()
        return max(remaining, 0.0)

    def _heartbeat_deadline(self, now: datetime) -> datetime:
        assert self._heartbeat_watch is not None
        assert self._heartbeat_timeout_seconds is not None
        provider, channel = self._heartbeat_watch
        snapshot = self._store.peek_liveness(provider, channel, now=now)
        base = (
            snapshot.heartbeat.received_at
            if snapshot.heartbeat is not None
            else (self._epoch_started_at or now)
        )
        return base + timedelta(seconds=self._heartbeat_timeout_seconds)

    def _runtime_exhausted(self) -> bool:
        if self._max_runtime_seconds is None or self._started_at is None:
            return False
        elapsed = (self._clock() - self._started_at).total_seconds()
        return elapsed >= self._max_runtime_seconds

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
