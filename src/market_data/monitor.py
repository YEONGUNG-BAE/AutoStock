from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from market_data.latest_state import (
    ApplyStatus,
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
from market_data.rolling_window import RollingObserveStatus, RollingTradeHistoryStore
from market_data.source_errors import (
    MarketSourceIteratorError,
    SourceIteratorUnknownAfterAck,
    WebSocketReceiveTimeoutAfterAck,
)

from domain.enums import Market

__all__ = [
    "AppliedMarketUpdate",
    "MonitorState",
    "MonitorEvidence",
    "MonitorSummary",
    "MonitorExhaustedError",
    "MonitorInternalError",
    "ReconnectPolicy",
    "MarketMonitor",
]


def _source_iterator_reason_subcode(exc: BaseException) -> str:
    if isinstance(exc, MarketSourceIteratorError):
        return exc.reason_subcode
    return SourceIteratorUnknownAfterAck.reason_subcode


def _source_iterator_parser_metadata(exc: BaseException) -> dict[str, object] | None:
    if isinstance(exc, MarketSourceIteratorError):
        return exc.parser_metadata
    return None


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
    consecutive_failures: int
    kind: str
    event_type: str | None = None
    provider: str | None = None
    channel: str | None = None
    market: str | None = None
    symbol: str | None = None
    sequence: int | None = None
    apply_status: str | None = None
    reason_code: str | None = None
    reason_subcode: str | None = None
    backoff_seconds: float | None = None
    parser_metadata: dict[str, object] | None = None


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
    consecutive_failures: int
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
    """transport 단절이 아닌 monitor 내부/저장소/evidence/post-apply hook 결함.

    backoff·reconnect로 숨기지 않고 fail-closed로 즉시 전파한다. 실제 운영 결함을
    transport drop으로 오인해 무한 재접속하는 것을 막기 위한 경계 표식이다.
    """


@dataclass(frozen=True)
class AppliedMarketUpdate:
    """APPLIED trade/quote 한 건에 대한 중립 post-apply 알림.

    execution/orchestration/broker 의존성 없이 monitor가 orchestration에 넘기는
    최소 식별자만 담는다(raw frame/credential/account/order/broker result 금지).
    `applied_at`은 latest apply와 rolling observe가 공유한 exact `now`이다.
    """

    market: Market
    symbol: str
    event_type: MarketEventType
    provider: str
    channel: str
    sequence: int
    applied_at: datetime


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
        rolling_store: RollingTradeHistoryStore | None = None,
        policy: ReconnectPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        session_id: str,
        max_events: int | None = None,
        max_runtime_seconds: float | None = None,
        heartbeat_watch: tuple[str, str] | None = None,
        heartbeat_timeout_seconds: float | None = None,
        on_evidence: Callable[[MonitorEvidence], None] | None = None,
        on_applied_update: Callable[[AppliedMarketUpdate], None] | None = None,
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
        self._rolling_store = rolling_store
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
        self._on_applied_update = on_applied_update

        self._state = MonitorState.IDLE
        self._counts = _Counts()
        self._events_consumed = 0
        # F3: connection_attempt(평생 단조 증가, 감사용)과 consecutive_failures(재접속
        # 정책·backoff를 구동, healthy 접속에서 0으로 리셋)를 분리한다. 정상 데이터를
        # 흘린 접속이 drop돼도 EXHAUSTED로 죽지 않게 한다.
        self._consecutive_failures = 0
        self._market_applied_this_attempt = 0
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
                self._market_applied_this_attempt = 0
                self._state = MonitorState.CONNECTING
                self._epoch_started_at = self._clock()
                self._emit("connect", attempt)
                outcome = await self._run_attempt(attempt)
                if outcome in ("eof", "budget"):
                    self._state = MonitorState.STOPPED
                    return self._summary(attempt)
                # outcome == "drop" -> transport reconnect.
                # F3: 이번 접속이 최소 1건의 시장 데이터(trade/quote) APPLIED를 흘렸다면
                # healthy로 보고 consecutive_failures를 0으로 리셋한다(backoff도 initial부터
                # 재시작). heartbeat는 APPLIED여도 시장 데이터가 아니므로 healthy 근거가 아니다
                # (heartbeat-only 접속이 무한 reconnect로 EXHAUSTED를 회피하던 결함 차단).
                # 시장 데이터 APPLIED가 없던 접속(factory/iterator 오류·heartbeat-only·heartbeat
                # timeout·중복/역순/future-only)만 연속 실패로 누적해 max_attempts에서 EXHAUSTED.
                if self._market_applied_this_attempt > 0:
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1
                if self._consecutive_failures >= self._policy.max_attempts:
                    self._state = MonitorState.EXHAUSTED
                    self._emit("exhausted", attempt)
                    raise MonitorExhaustedError(self._summary(attempt))
                self._pending_reset |= set(self._seen_streams)
                delay = self._policy.delay_for_attempt(max(self._consecutive_failures, 1))
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
            self._emit_cancelled(attempt)
            raise
        except Exception:
            self._emit_source_error_drop(attempt, subcode="post_startup_source_factory_error")
            return "drop"

        while True:
            timeout, timeout_reason = self._next_event_timeout()
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
                self._emit_cancelled(attempt)
                raise
            except (asyncio.TimeoutError, TimeoutError):
                # 원인은 wait_for 직전에 고른 deadline(timeout_reason) 그대로 쓴다.
                if timeout_reason == "runtime":
                    self._state = MonitorState.STOPPED
                    self._emit("stop", attempt, reason_code="runtime_timeout")
                    return "budget"
                if timeout_reason is None:
                    self._emit_source_error_drop(
                        attempt,
                        subcode=WebSocketReceiveTimeoutAfterAck.reason_subcode,
                    )
                    return "drop"
                self._emit("drop", attempt, reason_code="heartbeat_stale")
                return "drop"
            except Exception as exc:
                self._emit_source_error_drop(
                    attempt,
                    subcode=_source_iterator_reason_subcode(exc),
                    parser_metadata=_source_iterator_parser_metadata(exc),
                )
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
        # latest를 먼저 reset하고 그 다음 rolling을 reset한다 — rolling reset이 실패해도
        # latest는 이미 비워져 downstream trigger가 MISSING으로 fail-closed하기 쉽다.
        if stream in self._pending_reset:
            try:
                self._store.reset_stream(*stream)
            except Exception as exc:
                raise MonitorInternalError("store.reset_stream failed") from exc
            if self._rolling_store is not None:
                try:
                    self._rolling_store.reset_stream(*stream)
                except Exception as exc:
                    raise MonitorInternalError("rolling_store.reset_stream failed") from exc
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
        # latest apply와 rolling observe는 동일한 now를 공유한다(중간에 clock을 다시
        # 읽지 않는다) — 두 store의 시점 판정이 어긋나지 않게 하기 위함이다.
        now = self._clock()
        try:
            result = self._store.apply(event, now=now)
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
        # heartbeat APPLIED는 liveness 신호일 뿐 시장 데이터가 아니므로 healthy 근거에서 제외한다.
        if result.status is ApplyStatus.APPLIED and result.event_type in (
            MarketEventType.TRADE,
            MarketEventType.BEST_BID_ASK,
        ):
            self._market_applied_this_attempt += 1
        # APPLIED trade만 rolling history에 mirror한다(quote/heartbeat는 제외). latest가
        # APPLIED로 판정한 trade를 rolling이 비-APPLIED로 거부하면 두 store 계약이 어긋난
        # 내부 invariant 위반이므로 MonitorInternalError로 fail-closed한다(조용히 진행 금지).
        if (
            self._rolling_store is not None
            and result.status is ApplyStatus.APPLIED
            and result.event_type is MarketEventType.TRADE
        ):
            assert isinstance(event, NormalizedTradeTick)
            try:
                roll = self._rolling_store.observe(event, now=now)
            except Exception as exc:
                raise MonitorInternalError("rolling_store.observe failed") from exc
            if roll.status is not RollingObserveStatus.APPLIED:
                raise MonitorInternalError(
                    f"rolling observe was not APPLIED for a latest-APPLIED trade "
                    f"(status={roll.status.value})."
                )
        status = result.status.value
        setattr(self._counts, status, getattr(self._counts, status) + 1)
        self._emit(
            "apply",
            attempt,
            apply_status=status,
            reason_code=result.reason,
            meta=meta,
        )
        # APPLIED trade/quote만 post-apply hook 후보. heartbeat/duplicate/역순 등은 제외.
        if (
            self._on_applied_update is not None
            and result.status is ApplyStatus.APPLIED
            and result.event_type in (MarketEventType.TRADE, MarketEventType.BEST_BID_ASK)
        ):
            assert isinstance(event, (NormalizedTradeTick, NormalizedBestBidAsk))
            update = AppliedMarketUpdate(
                market=event.market,
                symbol=event.symbol,
                event_type=result.event_type,
                provider=event.provider_sequence.provider,
                channel=event.provider_sequence.channel,
                sequence=event.provider_sequence.sequence,
                applied_at=now,
            )
            try:
                self._on_applied_update(update)
            except MonitorInternalError:
                raise
            except Exception as exc:
                raise MonitorInternalError("post_apply_hook failed") from exc

    def _next_event_timeout(self) -> tuple[float | None, str | None]:
        """다음 이벤트를 기다릴 최대 시간(초)과 그 deadline의 원인을 함께 반환한다.

        heartbeat watchdog/runtime budget이 없으면 (None, None)=무한 대기. 둘 다 있으면
        가장 가까운 deadline을 택하되 동률이면 종료성(runtime)을 우선한다. timeout이
        실제로 터졌을 때 clock을 다시 읽어 원인을 추정하지 않고 여기서 정한 reason을
        그대로 쓴다 — fixed/non-ticking clock에서도 runtime timeout이 heartbeat_stale로
        오분류돼 재접속 루프에 빠지지 않게 하기 위함이다."""
        now = self._clock()
        candidates: list[tuple[datetime, str]] = []
        if self._heartbeat_watch is not None and self._heartbeat_timeout_seconds is not None:
            candidates.append((self._heartbeat_deadline(now), "heartbeat"))
        if self._max_runtime_seconds is not None and self._started_at is not None:
            candidates.append(
                (self._started_at + timedelta(seconds=self._max_runtime_seconds), "runtime")
            )
        if not candidates:
            return None, None
        deadline, reason = min(
            candidates, key=lambda c: (c[0], 0 if c[1] == "runtime" else 1)
        )
        remaining = (deadline - now).total_seconds()
        return max(remaining, 0.0), reason

    def _heartbeat_deadline(self, now: datetime) -> datetime:
        assert self._heartbeat_watch is not None
        assert self._heartbeat_timeout_seconds is not None
        watch = self._heartbeat_watch
        # 재접속 직후(이 stream의 첫 새 이벤트 수신 전, 즉 reset 대기 중)에는 이전 epoch의
        # heartbeat를 liveness 기준으로 쓰지 않는다. backoff 동안 wall-clock이 전진해
        # 옛 deadline이 이미 지났을 수 있고, 그러면 새 source가 첫 heartbeat를 낼 기회조차
        # 없이 wait_for(...,0)이 즉시 stale drop→재접속을 반복해 정상 연결도 exhausted된다.
        # 따라서 reset 대기 중에는 새 epoch 시작 시각을 기준으로 deadline을 잡는다.
        if watch in self._pending_reset:
            base = self._epoch_started_at or now
        else:
            snapshot = self._store.peek_liveness(*watch, now=now)
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
            consecutive_failures=self._consecutive_failures,
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
        reason_subcode: str | None = None,
        backoff_seconds: float | None = None,
        provider: str | None = None,
        channel: str | None = None,
        meta: dict[str, object | None] | None = None,
        parser_metadata: dict[str, object] | None = None,
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
            consecutive_failures=self._consecutive_failures,
            kind=kind,
            event_type=fields["event_type"],  # type: ignore[arg-type]
            provider=fields["provider"],  # type: ignore[arg-type]
            channel=fields["channel"],  # type: ignore[arg-type]
            market=fields["market"],  # type: ignore[arg-type]
            symbol=fields["symbol"],  # type: ignore[arg-type]
            sequence=fields["sequence"],  # type: ignore[arg-type]
            apply_status=apply_status,
            reason_code=reason_code,
            reason_subcode=reason_subcode,
            backoff_seconds=backoff_seconds,
            parser_metadata=parser_metadata,
        )
        # evidence sink 결함(disk full, broken pipe, callback bug)은 transport 단절이
        # 아니라 monitor 내부 계약 위반이므로 MonitorInternalError로 fail-closed 전파한다.
        try:
            self._on_evidence(evidence)
        except MonitorInternalError:
            raise
        except Exception as exc:
            raise MonitorInternalError("evidence sink failed") from exc

    def _emit_source_error_drop(
        self,
        attempt: int,
        *,
        subcode: str,
        parser_metadata: dict[str, object] | None = None,
    ) -> None:
        """Emit a source drop without leaking raw exception details.

        ``reason_code`` stays stable for existing counters, while ``reason_subcode``
        separates post-startup factory/open failures from iterator/recv drops.
        ``parser_metadata`` carries only sanitized counts/identifiers when present.
        """
        self._emit(
            "drop",
            attempt,
            reason_code="source_error",
            reason_subcode=subcode,
            parser_metadata=parser_metadata,
        )

    def _emit_cancelled(self, attempt: int) -> None:
        """취소 경로 전용 best-effort emit. sink가 터져도 CancelledError 전파를
        가리지 않도록 예외를 삼킨다(구조적 취소 의미 보존)."""
        with contextlib.suppress(Exception):
            self._emit("cancelled", attempt)
