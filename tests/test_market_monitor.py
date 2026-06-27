"""RTM-3 — fake-transport market monitor tests (network/broker/ledger-free)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domain.enums import Currency, Market
from market_data.latest_state import LatestMarketStateStore, MissingMarketStateError
from market_data.models import (
    MarketEvent,
    MarketHeartbeat,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)
from market_data.monitor import (
    MarketMonitor,
    MonitorEvidence,
    MonitorExhaustedError,
    MonitorInternalError,
    MonitorState,
    ReconnectPolicy,
)
from market_data.protocols import MarketEventSource
from market_data.replay_source import ReplayMarketEventSource
from market_data.source_errors import (
    MalformedControlAfterAck,
    MalformedMarketFrameAfterAck,
    MalformedQuoteFieldCountAfterAck,
    SourceIteratorUnknownAfterAck,
    UnsupportedTrIdAfterAck,
    WebSocketClosedAfterAck,
    WebSocketProtocolErrorAfterAck,
    WebSocketReceiveTimeoutAfterAck,
)

_BASE = datetime(2026, 6, 8, 0, 5, 0, tzinfo=UTC)


def _trade(*, sequence: int, trade_at: datetime = _BASE, price: str = "70000") -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        price=Decimal(price),
        quantity=Decimal("10"),
        trade_at=trade_at,
        received_at=trade_at,
        provider_sequence=ProviderSequence(
            provider="kis", channel="H0STCNT0|005930", sequence=sequence, received_at=trade_at
        ),
    )


def _quote(*, sequence: int, quote_at: datetime = _BASE) -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        bid_price=Decimal("69900"),
        ask_price=Decimal("70100"),
        bid_quantity=Decimal("10"),
        ask_quantity=Decimal("10"),
        quote_at=quote_at,
        received_at=quote_at,
        provider_sequence=ProviderSequence(
            provider="kis", channel="H0STASP0|005930", sequence=sequence, received_at=quote_at
        ),
    )


def _heartbeat(*, received_at: datetime) -> MarketHeartbeat:
    return MarketHeartbeat(
        provider="kis", channel="PINGPONG", sent_at=received_at, received_at=received_at
    )


def _fixed_clock(now: datetime) -> Callable[[], datetime]:
    return lambda: now


class _HeartbeatThenDrop:
    """heartbeat 하나를 흘린 뒤 transport 단절을 모사한다."""

    def __init__(self, beat: MarketHeartbeat) -> None:
        self._beat = beat

    async def events(self) -> AsyncIterator[MarketEvent]:
        yield self._beat
        raise RuntimeError("simulated transport drop")


class _HeartbeatThenEof:
    """healthy source: fresh heartbeat 하나를 흘린 뒤 정상 EOF."""

    def __init__(self, beat: MarketHeartbeat) -> None:
        self._beat = beat

    async def events(self) -> AsyncIterator[MarketEvent]:
        yield self._beat


class _RaiseImmediately:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def events(self) -> AsyncIterator[MarketEvent]:
        raise self._exc
        yield _heartbeat(received_at=_BASE)  # pragma: no cover


class _RaiseTypedFromSecret:
    """Raises a typed MarketSourceIteratorError chained from a secret-bearing cause.

    Proves the monitor surfaces only the sanitized subcode/whitelisted metadata on the
    drop evidence and never the chained cause, its message, or a traceback."""

    def __init__(self, exc: BaseException, secret: str) -> None:
        self._exc = exc
        self._secret = secret

    async def events(self) -> AsyncIterator[MarketEvent]:
        try:
            raise RuntimeError(self._secret)
        except RuntimeError as cause:
            raise self._exc from cause
        yield _heartbeat(received_at=_BASE)  # pragma: no cover - unreachable


class _RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _SteppingClock:
    """첫 호출은 start, 이후 호출은 start+step. wait_for의 deadline 계산을 음수로
    만들어 실제 대기 없이 timeout 분기를 결정론적으로 태운다."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self._start = start
        self._step = step
        self._calls = 0

    def __call__(self) -> datetime:
        self._calls += 1
        return self._start if self._calls == 1 else self._start + self._step


class _ApplyRaisingStore:
    """store.apply가 generic 예외를 던지는 스텁. 내부 결함이 transport drop으로
    오인되지 않고 MonitorInternalError로 전파되는지 검증하는 데 쓴다."""

    def apply(self, event: MarketEvent, *, now: datetime) -> object:
        raise RuntimeError("internal store defect")

    def reset_stream(self, provider: str, channel: str) -> None:  # pragma: no cover
        pass


class _FaultySource:
    """yields_before개의 이벤트를 흘린 뒤 transport 단절을 모사해 예외를 던진다."""

    def __init__(self, events: list[MarketEvent], *, yields_before: int) -> None:
        self._events = events
        self._yields_before = yields_before

    async def events(self) -> AsyncIterator[MarketEvent]:
        for i, event in enumerate(self._events):
            if i >= self._yields_before:
                break
            yield event
        raise RuntimeError("simulated transport drop")


class _BlockingSource:
    def __init__(self, started: asyncio.Event) -> None:
        self._started = started

    async def events(self) -> AsyncIterator[MarketEvent]:
        self._started.set()
        await asyncio.Event().wait()  # never returns -> awaits cancellation
        yield  # pragma: no cover - unreachable


def _run(monitor: MarketMonitor):
    return asyncio.run(monitor.run())


# --- replay source ----------------------------------------------------------


def test_replay_source_yields_in_order_then_eof() -> None:
    events = [_trade(sequence=1), _trade(sequence=2)]
    source = ReplayMarketEventSource(events)

    async def drain() -> list[MarketEvent]:
        return [e async for e in source.events()]

    first = asyncio.run(drain())
    second = asyncio.run(drain())  # fresh iterator each call
    assert [e.provider_sequence.sequence for e in first] == [1, 2]
    assert [e.provider_sequence.sequence for e in second] == [1, 2]


def test_replay_source_satisfies_protocol() -> None:
    assert isinstance(ReplayMarketEventSource([]), MarketEventSource)


# --- happy path / counts ----------------------------------------------------


def test_monitor_applies_events_and_summarizes() -> None:
    store = LatestMarketStateStore()
    events = [_trade(sequence=1), _trade(sequence=2), _trade(sequence=3)]
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource(events),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        session_id="sess-1",
    )
    summary = _run(monitor)
    assert summary.applied == 3
    assert summary.connection_attempts == 1
    assert summary.final_state is MonitorState.STOPPED
    assert monitor.state is MonitorState.STOPPED


def test_monitor_counts_duplicate_and_out_of_order() -> None:
    store = LatestMarketStateStore()
    events = [_trade(sequence=2), _trade(sequence=2), _trade(sequence=1)]
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource(events),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        session_id="sess-2",
    )
    summary = _run(monitor)
    assert summary.applied == 1
    assert summary.duplicate == 1
    assert summary.out_of_order == 1


def test_monitor_future_event_counted_and_does_not_crash() -> None:
    store = LatestMarketStateStore()
    # clock fixed at base; event trade_at 10s in the future -> FutureMarketEventError
    events = [_trade(sequence=1, trade_at=_BASE + timedelta(seconds=10))]
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource(events),
        clock=_fixed_clock(_BASE),
        session_id="sess-3",
    )
    summary = _run(monitor)
    assert summary.future_event_error == 1
    assert summary.applied == 0
    assert summary.final_state is MonitorState.STOPPED


def test_monitor_max_events_budget_stops_early() -> None:
    store = LatestMarketStateStore()
    events = [_trade(sequence=i) for i in range(1, 6)]
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource(events),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        session_id="sess-4",
        max_events=2,
    )
    summary = _run(monitor)
    assert summary.applied == 2
    assert summary.final_state is MonitorState.STOPPED


# --- backoff (pure) ---------------------------------------------------------


def test_backoff_is_pure_and_deterministic() -> None:
    policy = ReconnectPolicy(initial_delay_seconds=1.0, multiplier=2.0, max_delay_seconds=10.0)
    assert policy.delay_for_attempt(1) == 1.0
    assert policy.delay_for_attempt(2) == 2.0
    assert policy.delay_for_attempt(3) == 4.0
    assert policy.delay_for_attempt(4) == 8.0
    assert policy.delay_for_attempt(5) == 10.0  # capped
    with pytest.raises(ValueError):
        policy.delay_for_attempt(0)


# --- reconnect + explicit stream reset --------------------------------------


def test_monitor_reconnects_then_resets_stream() -> None:
    store = LatestMarketStateStore()
    clock = _fixed_clock(_BASE + timedelta(seconds=5))
    sleep = _RecordingSleep()
    first = _FaultySource([_trade(sequence=5, price="70000")], yields_before=1)
    second = ReplayMarketEventSource([_trade(sequence=1, price="80000")])
    sources: list[MarketEventSource] = [first, second]
    evidence: list[MonitorEvidence] = []

    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: sources.pop(0),
        clock=clock,
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=5),
        sleep=sleep,
        session_id="sess-5",
        on_evidence=evidence.append,
    )
    summary = _run(monitor)

    # backed off exactly once with the first-attempt delay
    assert sleep.calls == [1.0]
    assert summary.connection_attempts == 2
    # explicit stream reset happened on confirmed reconnect
    assert any(e.kind == "reset" for e in evidence)
    # after reset, a NEW epoch sequence=1 was accepted despite being < old sequence=5
    snap = store.peek(Market.KR, "005930", now=_BASE + timedelta(seconds=5))
    assert snap.trade is not None
    assert str(snap.trade.price) == "80000"
    assert snap.trade.provider_sequence.sequence == 1


def test_monitor_exhausts_after_max_attempts() -> None:
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _FaultySource([_trade(sequence=1)], yields_before=0),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=2),
        sleep=sleep,
        session_id="sess-6",
    )
    with pytest.raises(MonitorExhaustedError) as excinfo:
        _run(monitor)
    summary = excinfo.value.summary
    assert summary.final_state is MonitorState.EXHAUSTED
    assert summary.connection_attempts == 2
    assert sleep.calls == [1.0]  # backoff only after the first failure, not after exhaustion


# --- F3: connection_attempt(평생) vs consecutive_failures(재접속 정책) 분리 --------


def test_healthy_drops_do_not_exhaust_beyond_max_attempts() -> None:
    """접속마다 APPLIED 이벤트를 흘린 뒤 drop을 max_attempts보다 많이 반복해도
    EXHAUSTED되지 않는다. healthy drop은 consecutive_failures를 0으로 리셋한다(F3)."""
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()
    calls = {"n": 0}
    max_attempts = 3

    def factory() -> MarketEventSource:
        calls["n"] += 1
        if calls["n"] > max_attempts + 2:
            return ReplayMarketEventSource([])  # 결국 깔끔히 EOF로 종료
        return _FaultySource(
            [_trade(sequence=1), _trade(sequence=2), _trade(sequence=3)], yields_before=3
        )

    monitor = MarketMonitor(
        store=store,
        source_factory=factory,
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, multiplier=2.0, max_attempts=max_attempts),
        sleep=sleep,
        session_id="f3-1",
    )
    summary = _run(monitor)
    assert summary.final_state is MonitorState.STOPPED  # NOT exhausted
    assert summary.connection_attempts == max_attempts + 3  # 평생 카운터는 단조 증가
    assert summary.consecutive_failures == 0  # healthy drop이 매번 0으로 리셋
    # backoff는 healthy drop마다 initial(1.0)로 재시작한다 — 평생 누적으로 커지지 않는다.
    assert sleep.calls == [1.0] * (max_attempts + 2)


def test_zero_applied_failures_exhaust_exactly_at_max_attempts() -> None:
    """APPLIED 없는 연속 실패만 누적되어 정확히 max_attempts에서 EXHAUSTED한다.
    backoff는 consecutive_failures를 따라 커진다."""
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _FaultySource([_trade(sequence=1)], yields_before=0),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, multiplier=2.0, max_attempts=3),
        sleep=sleep,
        session_id="f3-2",
    )
    with pytest.raises(MonitorExhaustedError) as excinfo:
        _run(monitor)
    summary = excinfo.value.summary
    assert summary.final_state is MonitorState.EXHAUSTED
    assert summary.consecutive_failures == 3
    assert summary.connection_attempts == 3
    assert sleep.calls == [1.0, 2.0]  # 실패 1·2 후만 backoff, exhaustion 후엔 없음


def test_duplicate_only_connection_is_not_healthy() -> None:
    """APPLIED 없이 duplicate만 발생한 접속은 healthy가 아니다 → 실패로 누적된다."""
    store = LatestMarketStateStore()
    # 스트림을 미리 채워 다음 동일 sequence 이벤트가 APPLIED가 아닌 DUPLICATE가 되게 한다.
    store.apply(_trade(sequence=5), now=_BASE + timedelta(seconds=1))
    sleep = _RecordingSleep()
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _FaultySource([_trade(sequence=5)], yields_before=1),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=1),
        sleep=sleep,
        session_id="f3-3",
    )
    with pytest.raises(MonitorExhaustedError) as excinfo:
        _run(monitor)
    summary = excinfo.value.summary
    assert summary.applied == 0
    assert summary.duplicate == 1
    assert summary.consecutive_failures == 1
    assert summary.final_state is MonitorState.EXHAUSTED


def test_heartbeat_only_connection_is_not_healthy() -> None:
    """heartbeat만 APPLIED되고 시장 데이터(trade/quote)가 없던 접속은 healthy가 아니다.
    heartbeat-only drop이 반복되면 consecutive_failures가 누적돼 max_attempts에서
    EXHAUSTED한다(heartbeat APPLIED를 healthy로 오판하던 결함 차단)."""
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()

    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _HeartbeatThenDrop(_heartbeat(received_at=_BASE)),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, multiplier=2.0, max_attempts=3),
        sleep=sleep,
        session_id="f3-hb-1",
    )
    with pytest.raises(MonitorExhaustedError) as excinfo:
        _run(monitor)
    summary = excinfo.value.summary
    assert summary.final_state is MonitorState.EXHAUSTED
    assert summary.consecutive_failures == 3
    assert summary.connection_attempts == 3
    assert sleep.calls == [1.0, 2.0]  # 실패 1·2 후만 backoff


def test_heartbeat_then_trade_connection_is_healthy() -> None:
    """heartbeat에 이어 trade가 APPLIED된 접속은 healthy다 → consecutive_failures 0."""
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()
    calls = {"n": 0}
    max_attempts = 3

    def factory() -> MarketEventSource:
        calls["n"] += 1
        if calls["n"] > max_attempts + 2:
            return ReplayMarketEventSource([])
        return _FaultySource(
            [_heartbeat(received_at=_BASE), _trade(sequence=1)], yields_before=2
        )

    monitor = MarketMonitor(
        store=store,
        source_factory=factory,
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, multiplier=2.0, max_attempts=max_attempts),
        sleep=sleep,
        session_id="f3-hb-2",
    )
    summary = _run(monitor)
    assert summary.final_state is MonitorState.STOPPED  # NOT exhausted
    assert summary.consecutive_failures == 0
    assert sleep.calls == [1.0] * (max_attempts + 2)  # healthy drop마다 initial로 재시작


def test_heartbeat_then_quote_connection_is_healthy() -> None:
    """정책: trade 또는 quote 중 하나라도 APPLIED면 healthy. 거래가 뜸한 종목에서
    quote만 받는 정상 접속을 실패로 오판하지 않는다."""
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()
    calls = {"n": 0}
    max_attempts = 3

    def factory() -> MarketEventSource:
        calls["n"] += 1
        if calls["n"] > max_attempts + 2:
            return ReplayMarketEventSource([])
        return _FaultySource(
            [_heartbeat(received_at=_BASE), _quote(sequence=1)], yields_before=2
        )

    monitor = MarketMonitor(
        store=store,
        source_factory=factory,
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, multiplier=2.0, max_attempts=max_attempts),
        sleep=sleep,
        session_id="f3-hb-3",
    )
    summary = _run(monitor)
    assert summary.final_state is MonitorState.STOPPED  # NOT exhausted
    assert summary.consecutive_failures == 0
    assert sleep.calls == [1.0] * (max_attempts + 2)


def test_clean_eof_reports_zero_consecutive_failures() -> None:
    """정상 EOF 종료는 consecutive_failures=0으로 보고된다(정상 계약 보존)."""
    store = LatestMarketStateStore()
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource([_trade(sequence=1)]),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        session_id="f3-4",
    )
    summary = _run(monitor)
    assert summary.final_state is MonitorState.STOPPED
    assert summary.consecutive_failures == 0
    assert summary.applied == 1


def test_evidence_carries_connection_attempt_and_consecutive_failures() -> None:
    """evidence가 connection_attempt(평생)와 consecutive_failures(재접속)를 구분해 담는다."""
    store = LatestMarketStateStore()
    evidence: list[MonitorEvidence] = []
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _FaultySource([_trade(sequence=1)], yields_before=0),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=2),
        sleep=_RecordingSleep(),
        session_id="f3-5",
        on_evidence=evidence.append,
    )
    with pytest.raises(MonitorExhaustedError):
        _run(monitor)
    exhausted = [e for e in evidence if e.kind == "exhausted"]
    assert exhausted and exhausted[-1].connection_attempt == 2
    assert exhausted[-1].consecutive_failures == 2


# --- cancellation -----------------------------------------------------------


def test_monitor_cancellation_reraises_and_stops() -> None:
    store = LatestMarketStateStore()
    started = asyncio.Event()
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _BlockingSource(started),
        clock=_fixed_clock(_BASE),
        session_id="sess-7",
    )

    async def scenario() -> None:
        task = asyncio.create_task(monitor.run())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert monitor.state is MonitorState.STOPPED


# --- evidence isolation -----------------------------------------------------


def test_evidence_never_leaks_price_or_raw_values() -> None:
    store = LatestMarketStateStore()
    sentinel = "999999999"
    events = [_trade(sequence=1, price=sentinel)]
    evidence: list[MonitorEvidence] = []
    # 첫 접속은 sentinel가격 trade를 적용한 뒤 drop(apply+drop evidence를 모두 만든다),
    # 둘째 접속은 즉시 EOF로 깔끔히 종료한다. F3 이후 healthy drop은 EXHAUSTED로 죽지
    # 않으므로 종료는 EOF로 못박고, evidence isolation만 검증한다.
    first = _FaultySource(events, yields_before=1)
    second = ReplayMarketEventSource([])  # immediate EOF
    sources: list[MarketEventSource] = [first, second]
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: sources.pop(0),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=5),
        sleep=_RecordingSleep(),
        session_id="sess-8",
        on_evidence=evidence.append,
    )
    summary = _run(monitor)
    assert summary.final_state is MonitorState.STOPPED
    # drop evidence carries only sanitized reason fields, never the exception text or price
    for e in evidence:
        assert sentinel not in str(e)
        assert "simulated transport drop" not in str(e)
    drops = [e for e in evidence if e.kind == "drop"]
    assert drops and all(e.reason_code == "source_error" for e in drops)
    assert {e.reason_subcode for e in drops} == {"source_iterator_unknown_after_ack"}


@pytest.mark.parametrize(
    ("exc", "expected_subcode"),
    [
        (WebSocketClosedAfterAck(), "websocket_closed_after_ack"),
        (WebSocketReceiveTimeoutAfterAck(), "websocket_receive_timeout_after_ack"),
        (WebSocketProtocolErrorAfterAck(), "websocket_protocol_error_after_ack"),
        (MalformedMarketFrameAfterAck(), "malformed_market_frame_after_ack"),
        (UnsupportedTrIdAfterAck(), "unsupported_tr_id_after_ack"),
        (SourceIteratorUnknownAfterAck(), "source_iterator_unknown_after_ack"),
        (
            RuntimeError("raw websocket URL wss://example.invalid?token=SECRET"),
            "source_iterator_unknown_after_ack",
        ),
    ],
)
def test_source_iterator_error_drop_has_sanitized_subreason(
    exc: BaseException, expected_subcode: str
) -> None:
    store = LatestMarketStateStore()
    evidence: list[MonitorEvidence] = []
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _RaiseImmediately(exc),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=1),
        sleep=_RecordingSleep(),
        session_id="source-subcode",
        on_evidence=evidence.append,
    )

    with pytest.raises(MonitorExhaustedError):
        _run(monitor)

    drops = [e for e in evidence if e.kind == "drop"]
    assert len(drops) == 1
    assert drops[0].reason_code == "source_error"
    assert drops[0].reason_subcode == expected_subcode
    rendered = str(drops[0])
    for forbidden in (
        "wss://",
        "token=",
        "SECRET",
        "Traceback",
        "approval",
        "app_key",
    ):
        assert forbidden not in rendered


def test_source_error_drop_propagates_sanitized_parser_metadata() -> None:
    store = LatestMarketStateStore()
    evidence: list[MonitorEvidence] = []
    metadata = {
        "parser_stage": "field_count",
        "tr_id": "H0STASP0",
        "expected_field_count": 59,
        "observed_field_count": 2,
        "declared_count": 1,
        "record_len": 59,
        "has_trailing_empty_extra": False,
    }
    exc = MalformedQuoteFieldCountAfterAck(parser_metadata=metadata)
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _RaiseImmediately(exc),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=1),
        sleep=_RecordingSleep(),
        session_id="parser-metadata",
        on_evidence=evidence.append,
    )
    with pytest.raises(MonitorExhaustedError):
        _run(monitor)
    drops = [e for e in evidence if e.kind == "drop"]
    assert len(drops) == 1
    assert drops[0].reason_subcode == "malformed_quote_field_count_after_ack"
    assert drops[0].parser_metadata == metadata


def test_non_parser_source_error_drop_has_no_parser_metadata() -> None:
    store = LatestMarketStateStore()
    evidence: list[MonitorEvidence] = []
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _RaiseImmediately(WebSocketClosedAfterAck()),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=1),
        sleep=_RecordingSleep(),
        session_id="no-metadata",
        on_evidence=evidence.append,
    )
    with pytest.raises(MonitorExhaustedError):
        _run(monitor)
    drops = [e for e in evidence if e.kind == "drop"]
    assert len(drops) == 1
    assert drops[0].parser_metadata is None


def test_pilot3_malformed_control_drop_evidence_is_sanitized() -> None:
    """pilot-3 operational noise (malformed_control_after_ack=656): a source raising
    MalformedControlAfterAck yields a sanitized source_error drop with the stable subcode,
    whitelist-only parser_metadata, and no raw frame/cause/traceback leak."""
    store = LatestMarketStateStore()
    evidence: list[MonitorEvidence] = []
    secret = "wss://kis.invalid?approval_key=SECRET&account=12345678"
    exc = MalformedControlAfterAck(
        parser_metadata={"parser_stage": "control", "tr_id": "H0STASP0"}
    )
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _RaiseTypedFromSecret(exc, secret),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=1),
        sleep=_RecordingSleep(),
        session_id="pilot3-control",
        on_evidence=evidence.append,
    )
    with pytest.raises(MonitorExhaustedError):
        _run(monitor)
    drops = [e for e in evidence if e.kind == "drop"]
    assert len(drops) == 1
    drop = drops[0]
    assert drop.reason_code == "source_error"
    assert drop.reason_subcode == "malformed_control_after_ack"
    assert drop.parser_metadata is None or set(drop.parser_metadata) <= {
        "parser_stage",
        "tr_id",
        "expected_field_count",
        "observed_field_count",
        "declared_count",
        "record_len",
        "has_trailing_empty_extra",
    }
    rendered = str(drop)
    for forbidden in (
        "wss://",
        "approval_key",
        "SECRET",
        "account",
        "12345678",
        "Traceback",
    ):
        assert forbidden not in rendered


def test_pilot3_websocket_closed_drop_evidence_is_sanitized() -> None:
    """pilot-3 operational noise (websocket_closed_after_ack=1): a source raising
    WebSocketClosedAfterAck yields a sanitized source_error drop with the stable subcode,
    no parser_metadata, and no raw close-reason/cause/traceback leak."""
    store = LatestMarketStateStore()
    evidence: list[MonitorEvidence] = []
    secret = "wss://kis.invalid?token=SECRET_TOKEN&account=87654321"
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _RaiseTypedFromSecret(WebSocketClosedAfterAck(), secret),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=1),
        sleep=_RecordingSleep(),
        session_id="pilot3-closed",
        on_evidence=evidence.append,
    )
    with pytest.raises(MonitorExhaustedError):
        _run(monitor)
    drops = [e for e in evidence if e.kind == "drop"]
    assert len(drops) == 1
    drop = drops[0]
    assert drop.reason_code == "source_error"
    assert drop.reason_subcode == "websocket_closed_after_ack"
    assert drop.parser_metadata is None
    rendered = str(drop)
    for forbidden in (
        "wss://",
        "token=",
        "SECRET_TOKEN",
        "account",
        "87654321",
        "Traceback",
    ):
        assert forbidden not in rendered


def test_require_fresh_missing_after_reset() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=3), now=_BASE + timedelta(seconds=1))
    store.reset_stream("kis", "H0STCNT0|005930")
    with pytest.raises(MissingMarketStateError):
        store.require_fresh(Market.KR, "005930", now=_BASE + timedelta(seconds=2))


# --- hardening item 1: reset is deferred to the new epoch's first event -----


def test_reconnect_without_new_event_preserves_old_state() -> None:
    """새 source가 첫 이벤트도 못 내고 EOF면 reset이 일어나지 않아 기존 state가 보존된다.
    reset이 '재접속 시작'이 아니라 '새 stream 첫 이벤트 수신 직후'에만 일어남을 못박는다."""
    store = LatestMarketStateStore()
    clock = _fixed_clock(_BASE + timedelta(seconds=5))
    first = _FaultySource([_trade(sequence=5, price="70000")], yields_before=1)
    second = ReplayMarketEventSource([])  # immediate EOF, no events
    sources: list[MarketEventSource] = [first, second]
    evidence: list[MonitorEvidence] = []

    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: sources.pop(0),
        clock=clock,
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=5),
        sleep=_RecordingSleep(),
        session_id="hard-1",
        on_evidence=evidence.append,
    )
    summary = _run(monitor)

    assert summary.final_state is MonitorState.STOPPED
    assert summary.connection_attempts == 2
    # no reset evidence: the second epoch never delivered an event to trigger it
    assert not any(e.kind == "reset" for e in evidence)
    # old state from the first epoch survives untouched
    snap = store.peek(Market.KR, "005930", now=_BASE + timedelta(seconds=5))
    assert snap.trade is not None
    assert snap.trade.provider_sequence.sequence == 5
    assert str(snap.trade.price) == "70000"


# --- hardening item 2: heartbeat stale watchdog reconnects -------------------


def test_heartbeat_watchdog_drops_on_silence() -> None:
    """heartbeat_watch가 설정되면 다음 이벤트를 timeout 안에 못 받을 때 half-dead
    연결로 보고 drop·reconnect한다."""
    store = LatestMarketStateStore()
    started = asyncio.Event()
    evidence: list[MonitorEvidence] = []
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _BlockingSource(started),
        clock=_fixed_clock(_BASE),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=1),
        sleep=_RecordingSleep(),
        session_id="hard-2",
        heartbeat_watch=("kis", "PINGPONG"),
        heartbeat_timeout_seconds=0.05,
        on_evidence=evidence.append,
    )
    with pytest.raises(MonitorExhaustedError) as excinfo:
        _run(monitor)
    assert excinfo.value.summary.final_state is MonitorState.EXHAUSTED
    drops = [e for e in evidence if e.kind == "drop"]
    assert drops and all(e.reason_code == "heartbeat_stale" for e in drops)


# --- hardening item 3: runtime timeout fires even on a silent source ---------


def test_runtime_timeout_stops_silent_source() -> None:
    """silent source(이벤트 무한 대기)에도 max_runtime_seconds가 작동해 budget 종료한다.
    stepping clock으로 deadline을 음수화해 실제 대기 없이 timeout 분기를 태운다."""
    store = LatestMarketStateStore()
    started = asyncio.Event()
    evidence: list[MonitorEvidence] = []
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _BlockingSource(started),
        clock=_SteppingClock(_BASE, timedelta(seconds=100)),
        session_id="hard-3",
        max_runtime_seconds=0.05,
        on_evidence=evidence.append,
    )
    summary = _run(monitor)
    assert summary.final_state is MonitorState.STOPPED
    assert summary.connection_attempts == 1
    stops = [e for e in evidence if e.kind == "stop"]
    assert stops and all(e.reason_code == "runtime_timeout" for e in stops)


# --- hardening item 4: internal vs transport error boundary ------------------


def test_internal_store_error_propagates_not_reconnect() -> None:
    """store.apply 내부 결함은 MonitorInternalError로 즉시 전파되고, transport drop으로
    오인해 backoff·reconnect하지 않는다."""
    sleep = _RecordingSleep()
    monitor = MarketMonitor(
        store=_ApplyRaisingStore(),  # type: ignore[arg-type]
        source_factory=lambda: ReplayMarketEventSource([_trade(sequence=1)]),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=5),
        sleep=sleep,
        session_id="hard-4a",
    )
    with pytest.raises(MonitorInternalError):
        _run(monitor)
    assert sleep.calls == []  # never backed off
    assert monitor.state is MonitorState.STOPPED


def test_factory_error_triggers_backoff_not_death() -> None:
    """source_factory 자체 오류는 transport drop으로 분류돼 backoff·reconnect한다."""
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()
    calls = {"n": 0}

    def factory() -> MarketEventSource:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connect failed")
        return ReplayMarketEventSource([_trade(sequence=1, price="80000")])

    monitor = MarketMonitor(
        store=store,
        source_factory=factory,
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=5),
        sleep=sleep,
        session_id="hard-4b",
    )
    summary = _run(monitor)
    assert summary.applied == 1
    assert summary.connection_attempts == 2
    assert sleep.calls == [1.0]
    assert summary.final_state is MonitorState.STOPPED


def test_factory_source_error_drop_has_sanitized_subreason() -> None:
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()
    evidence: list[MonitorEvidence] = []
    calls = {"n": 0}

    def factory() -> MarketEventSource:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("credentialed websocket url must not leak")
        return ReplayMarketEventSource([])

    monitor = MarketMonitor(
        store=store,
        source_factory=factory,
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=5),
        sleep=sleep,
        session_id="hard-4c",
        on_evidence=evidence.append,
    )
    summary = _run(monitor)
    assert summary.final_state is MonitorState.STOPPED
    drops = [e for e in evidence if e.kind == "drop"]
    assert len(drops) == 1
    assert drops[0].reason_code == "source_error"
    assert drops[0].reason_subcode == "post_startup_source_factory_error"
    assert "credentialed" not in str(drops[0])


# --- hardening item 5: policy + budget argument validation -------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"multiplier": 0.0},
        {"initial_delay_seconds": -1.0},
        {"initial_delay_seconds": 1.0, "max_delay_seconds": 0.5},
    ],
)
def test_reconnect_policy_rejects_invalid_args(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ReconnectPolicy(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_events": 0},
        {"max_runtime_seconds": 0.0},
        {"heartbeat_watch": ("kis", "PINGPONG")},  # timeout missing
        {"heartbeat_timeout_seconds": 1.0},  # watch missing
        {"heartbeat_watch": ("kis", "PINGPONG"), "heartbeat_timeout_seconds": 0.0},
    ],
)
def test_monitor_rejects_invalid_budget_args(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        MarketMonitor(
            store=LatestMarketStateStore(),
            source_factory=lambda: ReplayMarketEventSource([]),
            clock=_fixed_clock(_BASE),
            session_id="hard-5",
            **kwargs,  # type: ignore[arg-type]
        )


# --- follow-up hardening: heartbeat reconnect / runtime reason / sink errors -


def test_reconnect_recovers_after_heartbeat_stale() -> None:
    """heartbeat stale drop 후, backoff로 clock이 옛 deadline을 지났더라도 healthy
    source가 새 heartbeat를 낼 기회를 얻어 정상 복구해야 한다.

    regression: 재접속 중 deadline을 이전 epoch heartbeat로 잡으면 wait_for(...,0)으로
    새 source 첫 이벤트도 못 받고 stale drop→재접속을 반복해 exhausted된다."""
    store = LatestMarketStateStore()
    sources: list[MarketEventSource] = [
        _HeartbeatThenDrop(_heartbeat(received_at=_BASE)),
        _HeartbeatThenEof(_heartbeat(received_at=_BASE + timedelta(seconds=29))),
    ]
    # clock이 backoff 동안 옛 deadline(_BASE+10)을 지나 _BASE+30에 있다고 가정.
    clock = _fixed_clock(_BASE + timedelta(seconds=30))
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: sources.pop(0),
        clock=clock,
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=4),
        sleep=_RecordingSleep(),
        session_id="hard-6",
        heartbeat_watch=("kis", "PINGPONG"),
        heartbeat_timeout_seconds=10.0,
    )
    summary = _run(monitor)
    assert summary.final_state is MonitorState.STOPPED
    assert summary.connection_attempts == 2  # recovered on second attempt, not exhausted
    assert summary.applied == 2  # both heartbeats applied (old + fresh)


def test_runtime_timeout_fixed_clock_not_misclassified() -> None:
    """max_runtime_seconds만 설정하고 injected clock이 고정돼 있어도 runtime timeout이
    heartbeat_stale로 오분류되지 않고 깔끔히 budget 종료(STOPPED)해야 한다.

    regression: timeout 원인을 사후에 clock으로 추정하면 고정 clock에서 runtime을
    heartbeat_stale로 오판해 재접속 루프에 빠진다."""
    store = LatestMarketStateStore()
    started = asyncio.Event()
    evidence: list[MonitorEvidence] = []
    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: _BlockingSource(started),
        clock=_fixed_clock(_BASE),  # never ticks
        policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=3),
        sleep=_RecordingSleep(),
        session_id="hard-7",
        max_runtime_seconds=0.03,
        on_evidence=evidence.append,
    )
    summary = _run(monitor)
    assert summary.final_state is MonitorState.STOPPED
    assert summary.connection_attempts == 1  # no reconnect loop
    stops = [e for e in evidence if e.kind == "stop"]
    assert stops and all(e.reason_code == "runtime_timeout" for e in stops)
    assert not any(e.kind == "drop" for e in evidence)


def test_evidence_sink_failure_is_internal_error() -> None:
    """evidence sink 결함은 MonitorInternalError로 fail-closed 전파된다(계약 일치).
    transport drop으로 오인해 backoff·reconnect하지 않는다."""
    store = LatestMarketStateStore()
    sleep = _RecordingSleep()

    def boom(_evidence: MonitorEvidence) -> None:
        raise RuntimeError("disk full")

    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource([_trade(sequence=1)]),
        clock=_fixed_clock(_BASE + timedelta(seconds=5)),
        policy=ReconnectPolicy(initial_delay_seconds=1.0, max_attempts=5),
        sleep=sleep,
        session_id="hard-8",
        on_evidence=boom,
    )
    with pytest.raises(MonitorInternalError):
        _run(monitor)
    assert sleep.calls == []
    assert monitor.state is MonitorState.STOPPED
