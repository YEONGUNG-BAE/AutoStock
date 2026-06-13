"""RTM-7b.3 — calendar-gated bounded monitor supervisor (순수 asyncio; broker/ledger 미연결).

이 supervisor는 MarketMonitor **위**에 있다. 책임 경계를 명확히 한다.

- KIS source: 한 connection lifecycle(connect→subscribe→ACK→yield→disconnect). 내부 reconnect 없음.
- MarketMonitor: 한 run() 안에서 reconnect/backoff/heartbeat-timeout/연속실패 budget/EXHAUSTED.
- MarketSupervisor(여기): run() **호출 사이**. calendar로 세션을 게이트해 start/stop,
  run() 종료(EXHAUSTED 포함) 간 restart budget+backoff, health 평가, graceful shutdown,
  stale 시 fail-closed. broker/ledger/paper coordinator를 절대 호출하지 않는다.

MarketMonitor와 책임이 겹치지 않는다. monitor는 한 연결 수명 안의 재접속을, supervisor는
세션 경계와 monitor 재기동을 다룬다. supervisor는 scheduler/runtime을 기본 활성화하지 않으며
생성자에서 side effect(task 생성·clock 읽기·I/O)가 없다. 실제 socket/DNS/asyncio 외부 의존이
없고 clock/sleep/monitor_factory/세션 provider가 모두 주입식이라 fake clock으로 결정론 검증된다.

import 가드: 이 파일은 asyncio만 예외 허용(monitor.py와 동일). broker/ledger/decision/
paper_loop/llm/socket/... 은 market_data 전역 금지가 그대로 적용되어 구조적으로 차단된다.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from market_data.health_policy import HealthStatus, HealthVerdict, MarketHealthTracker
from market_data.market_session import MarketCalendarProvider, MarketSession
from market_data.monitor import MonitorSummary

from domain.enums import Market

__all__ = [
    "SupervisorState",
    "SupervisorPolicy",
    "SupervisorError",
    "SupervisedMonitor",
    "SupervisorEvidence",
    "SupervisorSummary",
    "MarketSupervisor",
]


class SupervisorState(StrEnum):
    IDLE = "idle"  # 시작 전
    RUNNING = "running"  # 세션 활성 + monitor 가동 중
    IDLE_CLOSED = "idle_closed"  # 세션 비활성(장외/휴장) — monitor 미가동
    BACKING_OFF = "backing_off"  # monitor 종료 후 재기동 대기
    STOPPED = "stopped"  # graceful shutdown 완료
    FAILED_CLOSED = "failed_closed"  # restart budget 소진 — fail-closed


class SupervisorError(Exception):
    """supervisor 설정/정책 위반."""


@dataclass(frozen=True)
class SupervisorPolicy:
    """잠정 정책값. 모두 양수. live smoke evidence 후 확정."""

    poll_interval_seconds: float = 1.0
    max_restarts_in_window: int = 5
    restart_window_seconds: float = 300.0
    restart_backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise SupervisorError("poll_interval_seconds must be > 0.")
        if self.max_restarts_in_window < 1:
            raise SupervisorError("max_restarts_in_window must be >= 1.")
        if self.restart_window_seconds <= 0:
            raise SupervisorError("restart_window_seconds must be > 0.")
        if self.restart_backoff_seconds < 0:
            raise SupervisorError("restart_backoff_seconds must be >= 0.")


class SupervisedMonitor:
    """supervisor가 기동하는 monitor의 최소 계약(Protocol-유사 덕타이핑).

    MarketMonitor가 이 형태를 만족한다: `state` 속성 + `async run() -> MonitorSummary`.
    구체 타입을 import하지 않으려고 별도 Protocol을 두지 않고 덕타이핑으로 수용한다.
    """

    async def run(self) -> MonitorSummary:  # pragma: no cover - 계약 문서용
        raise NotImplementedError

    @property
    def state(self) -> object:  # pragma: no cover - 계약 문서용
        raise NotImplementedError


@dataclass(frozen=True)
class SupervisorEvidence:
    """append-only supervisor evidence 한 건. raw frame/token/account/예외 repr 미포함."""

    timestamp: datetime
    state: SupervisorState
    session_state: str
    transport: str
    market_data: str
    restarts_in_window: int
    kind: str
    reason_code: str | None = None
    backoff_seconds: float | None = None


@dataclass(frozen=True)
class SupervisorSummary:
    final_state: SupervisorState
    monitor_starts: int
    monitor_exits: int
    restarts_in_window: int
    ticks: int


@dataclass
class MarketSupervisor:
    """calendar로 게이트되는 bounded monitor supervisor (순수 asyncio).

    한 틱마다: (1) 주입 clock으로 now를 읽고 calendar로 세션 판정, (2) 세션이 거래 활성이면
    monitor를 1개만 가동(없으면 budget 안에서 새로 start), 비활성이면 graceful cancel,
    (3) 종료된 monitor task를 reap하고 budget을 갱신(초과 시 FAILED_CLOSED), (4) health
    verdict를 sanitized evidence로 emit, (5) poll_interval만큼 sleep. supervisor 자신이
    cancel되면 가동 중 monitor를 graceful cancel 후 CancelledError를 재전파한다(구조적 취소).

    broker/ledger/paper coordinator를 호출하지 않는다. health가 unhealthy/stale이어도
    execution을 트리거하지 않는다 — 이 레이어는 관측·가동 제어만 한다(fail-closed 기본).
    """

    market: Market
    calendar: MarketCalendarProvider
    monitor_factory: Callable[[], SupervisedMonitor]
    tracker: MarketHealthTracker
    clock: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]]
    policy: SupervisorPolicy = field(default_factory=SupervisorPolicy)
    max_ticks: int | None = None
    on_evidence: Callable[[SupervisorEvidence], None] | None = None

    _state: SupervisorState = field(default=SupervisorState.IDLE, init=False)
    _task: asyncio.Task[MonitorSummary] | None = field(default=None, init=False)
    _restart_times: list[datetime] = field(default_factory=list, init=False)
    _monitor_starts: int = field(default=0, init=False)
    _monitor_exits: int = field(default=0, init=False)
    _ticks: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.max_ticks is not None and self.max_ticks < 1:
            raise SupervisorError("max_ticks must be >= 1 when set.")

    @property
    def state(self) -> SupervisorState:
        return self._state

    # --- 외부 신호 주입(supervisor가 source/monitor sink를 어댑트해 호출) ------------

    def record_transport(self, *, kind: str, at: datetime) -> None:
        self.tracker.record_transport_event(kind=kind, at=at)

    def record_market(self, *, event_type: str, at: datetime) -> None:
        self.tracker.record_market_event(event_type=event_type, at=at)

    # --- run loop -------------------------------------------------------------

    async def run(self) -> SupervisorSummary:
        if self._state is not SupervisorState.IDLE:
            raise SupervisorError("supervisor.run() is not re-entrant.")
        try:
            while True:
                self._ticks += 1
                now = self.clock()
                session = self.calendar.session_at(self.market, now)
                self._reap_finished(now)
                if self._is_active(session):
                    await self._ensure_running(now, session)
                else:
                    await self._ensure_idle(now, session)
                if self._state is SupervisorState.FAILED_CLOSED:
                    self._emit("failed_closed", session, reason_code="restart_budget_exhausted")
                    return self._summary()
                self._emit_health(now, session)
                if self.max_ticks is not None and self._ticks >= self.max_ticks:
                    await self._graceful_cancel(now, session, reason_code="max_ticks")
                    self._state = SupervisorState.STOPPED
                    return self._summary()
                await self.sleep(self.policy.poll_interval_seconds)
        except asyncio.CancelledError:
            # 구조적 취소: 가동 중 monitor를 graceful cancel 후 재전파.
            await self._graceful_cancel(self.clock(), None, reason_code="supervisor_cancelled")
            self._state = SupervisorState.STOPPED
            raise
        finally:
            # 어떤 경로로 빠져나가도 task leak이 없도록 보장.
            await self._graceful_cancel(self.clock(), None, reason_code="shutdown")

    # --- session gating -------------------------------------------------------

    @staticmethod
    def _is_active(session: MarketSession) -> bool:
        # 정규장(OPEN)만 monitor를 가동한다. PRE_OPEN/POST_CLOSE/CLOSED는 비활성.
        return session.is_open

    async def _ensure_running(self, now: datetime, session: MarketSession) -> None:
        if self._task is not None:
            self._state = SupervisorState.RUNNING
            return
        if not self._restart_allowed(now):
            self._state = SupervisorState.FAILED_CLOSED
            return
        # 직전 monitor가 종료(BACKING_OFF)해 재기동하는 경우 backoff를 적용한다. 최초
        # 기동(IDLE/IDLE_CLOSED)에는 backoff 없이 즉시 가동한다.
        if self._state is SupervisorState.BACKING_OFF and self.policy.restart_backoff_seconds > 0:
            self._emit("restart_backoff", session, backoff_seconds=self.policy.restart_backoff_seconds)
            await self.sleep(self.policy.restart_backoff_seconds)
        self._restart_times.append(now)
        self._task = asyncio.create_task(self.monitor_factory().run())
        self._monitor_starts += 1
        self._state = SupervisorState.RUNNING
        self._emit("monitor_start", session, reason_code="session_active")
        # 막 만든 task가 최소 한 스텝 진행할 기회를 준다(fake sleep이 yield 안 할 때 대비).
        await asyncio.sleep(0)

    async def _ensure_idle(self, now: datetime, session: MarketSession) -> None:
        if self._task is not None:
            await self._graceful_cancel(now, session, reason_code="session_inactive")
        self._state = SupervisorState.IDLE_CLOSED

    # --- monitor lifecycle ----------------------------------------------------

    def _reap_finished(self, now: datetime) -> None:
        task = self._task
        if task is None or not task.done():
            return
        self._task = None
        self._monitor_exits += 1
        # monitor가 자체 종료(STOPPED/EXHAUSTED)했다. 예외는 삼키지 않되 CancelledError만
        # 정상 취소로 본다. EXHAUSTED는 MonitorExhaustedError로 올라오므로 재기동 대상이다.
        if not task.cancelled():
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                # transport/내부 결함으로 종료 — 재기동 budget 대상. 예외 repr은 evidence에
                # 담지 않고 reason_code만 남긴다.
                self._state = SupervisorState.BACKING_OFF
                self._emit("monitor_exit", None, reason_code="monitor_error")
                return
        self._state = SupervisorState.BACKING_OFF
        self._emit("monitor_exit", None, reason_code="monitor_stopped")

    async def _graceful_cancel(
        self, now: datetime, session: MarketSession | None, *, reason_code: str
    ) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        if not task.done():
            task.cancel()
        # 취소 완료를 기다려 task leak을 방지한다. monitor의 CancelledError는 정상 종료다.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(task, return_exceptions=True)
        self._monitor_exits += 1
        self._emit("monitor_cancelled", session, reason_code=reason_code)

    # --- restart budget -------------------------------------------------------

    def _restart_allowed(self, now: datetime) -> bool:
        cutoff = now - timedelta(seconds=self.policy.restart_window_seconds)
        self._restart_times = [t for t in self._restart_times if t >= cutoff]
        return len(self._restart_times) < self.policy.max_restarts_in_window

    def _restarts_in_window(self, now: datetime) -> int:
        cutoff = now - timedelta(seconds=self.policy.restart_window_seconds)
        return sum(1 for t in self._restart_times if t >= cutoff)

    # --- evidence -------------------------------------------------------------

    def _emit_health(self, now: datetime, session: MarketSession) -> None:
        verdict = self.tracker.evaluate(session=session, now=now)
        self._emit_verdict(now, session, verdict)

    def _emit_verdict(
        self, now: datetime, session: MarketSession, verdict: HealthVerdict
    ) -> None:
        if self.on_evidence is None:
            return
        kind = "health_healthy" if verdict.is_healthy else "health_unhealthy"
        self.on_evidence(
            SupervisorEvidence(
                timestamp=now,
                state=self._state,
                session_state=str(session.state),
                transport=str(verdict.transport),
                market_data=str(verdict.market_data),
                restarts_in_window=self._restarts_in_window(now),
                kind=kind,
                reason_code=verdict.reasons[0] if verdict.reasons else None,
            )
        )

    def _emit(
        self,
        kind: str,
        session: MarketSession | None,
        *,
        reason_code: str | None = None,
        backoff_seconds: float | None = None,
    ) -> None:
        if self.on_evidence is None:
            return
        now = self.clock()
        session_state = str(session.state) if session is not None else "UNKNOWN"
        self.on_evidence(
            SupervisorEvidence(
                timestamp=now,
                state=self._state,
                session_state=session_state,
                transport=str(HealthStatus.UNKNOWN),
                market_data=str(HealthStatus.UNKNOWN),
                restarts_in_window=self._restarts_in_window(now),
                kind=kind,
                reason_code=reason_code,
                backoff_seconds=backoff_seconds,
            )
        )

    def _summary(self) -> SupervisorSummary:
        return SupervisorSummary(
            final_state=self._state,
            monitor_starts=self._monitor_starts,
            monitor_exits=self._monitor_exits,
            restarts_in_window=self._restarts_in_window(self.clock()),
            ticks=self._ticks,
        )
