"""RTM-7b.3 — calendar-gated bounded monitor supervisor (순수 asyncio; broker/ledger 미연결).

책임 경계:
- MarketMonitor: 한 run() 안에서 reconnect/backoff/heartbeat-timeout.
- MarketSupervisor(여기): run() **호출 사이** process-level lifecycle, typed action, restart budget.

market-data starvation(quote/trade)은 transport restart 사유가 **아니다** — HOLD_EXECUTION_ONLY.
transport 결함(disconnect/flapping/heartbeat timeout)만 RESTART_TRANSPORT.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from market_data.health_policy import (
    HealthVerdict,
    MarketDataHealthStatus,
    MarketHealthTracker,
    TransportHealthStatus,
)
from market_data.market_session import (
    CalendarReason,
    MarketCalendarProvider,
    MarketSession,
    MarketSessionError,
    MarketSessionState,
)
from market_data.monitor import MonitorSummary

from domain.enums import Market

__all__ = [
    "SupervisorAction",
    "SupervisorState",
    "SupervisorPolicy",
    "SupervisorError",
    "SupervisedMonitor",
    "SupervisorEvidence",
    "SupervisorSummary",
    "MarketSupervisor",
]


class SupervisorState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    IDLE_CLOSED = "idle_closed"
    WAITING_CALENDAR = "waiting_calendar"
    BACKING_OFF = "backing_off"
    STOPPED = "stopped"
    FAILED_CLOSED = "failed_closed"


class SupervisorAction(StrEnum):
    KEEP_RUNNING = "KEEP_RUNNING"
    WAIT_FOR_SESSION = "WAIT_FOR_SESSION"
    WAIT_FOR_CALENDAR = "WAIT_FOR_CALENDAR"
    HOLD_EXECUTION_ONLY = "HOLD_EXECUTION_ONLY"
    RESTART_TRANSPORT = "RESTART_TRANSPORT"
    STOP_FOR_SESSION = "STOP_FOR_SESSION"
    FAILED_CLOSED = "FAILED_CLOSED"


class SupervisorError(Exception):
    """supervisor 설정/정책 위반."""


@dataclass(frozen=True)
class SupervisorPolicy:
    """잠정 정책값. caller가 명시해야 한다."""

    poll_interval_seconds: float
    max_restarts_in_window: int
    restart_window_seconds: float
    restart_backoff_seconds: float

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise SupervisorError("poll_interval_seconds must be > 0.")
        if self.max_restarts_in_window < 0:
            raise SupervisorError("max_restarts_in_window must be >= 0.")
        if self.restart_window_seconds <= 0:
            raise SupervisorError("restart_window_seconds must be > 0.")
        if self.restart_backoff_seconds < 0:
            raise SupervisorError("restart_backoff_seconds must be >= 0.")


class SupervisedMonitor:
    """supervisor가 기동하는 monitor의 최소 계약."""

    async def run(self) -> MonitorSummary:  # pragma: no cover
        raise NotImplementedError

    @property
    def state(self) -> object:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True)
class SupervisorEvidence:
    """append-only supervisor evidence 한 건. raw frame/token/account/예외 repr 미포함."""

    timestamp: datetime
    state: SupervisorState
    session_state: str
    transport: str
    market_data: str
    action: str
    monitor_initial_starts: int
    monitor_restarts: int
    monitor_cancels: int
    kind: str
    reason_code: str | None = None
    backoff_seconds: float | None = None
    execution_ready: bool = False


@dataclass(frozen=True)
class SupervisorSummary:
    final_state: SupervisorState
    monitor_initial_starts: int
    monitor_restarts: int
    monitor_cancels: int
    ticks: int
    final_action: SupervisorAction | None = None


def provisional_supervisor_policy() -> SupervisorPolicy:
    return SupervisorPolicy(
        poll_interval_seconds=1.0,
        max_restarts_in_window=2,
        restart_window_seconds=300.0,
        restart_backoff_seconds=1.0,
    )


@dataclass
class MarketSupervisor:
    """calendar로 게이트되는 bounded monitor supervisor."""

    market: Market
    calendar: MarketCalendarProvider
    monitor_factory: Callable[[], SupervisedMonitor]
    tracker: MarketHealthTracker
    clock: Callable[[], datetime]
    sleep: Callable[[float], Awaitable[None]]
    policy: SupervisorPolicy
    max_ticks: int | None = None
    on_evidence: Callable[[SupervisorEvidence], None] | None = None

    _state: SupervisorState = field(default=SupervisorState.IDLE, init=False)
    _task: asyncio.Task[MonitorSummary] | None = field(default=None, init=False)
    _restart_times: list[datetime] = field(default_factory=list, init=False)
    _initial_starts: int = field(default=0, init=False)
    _restarts: int = field(default=0, init=False)
    _cancels: int = field(default=0, init=False)
    _ticks: int = field(default=0, init=False)
    _last_action: SupervisorAction | None = field(default=None, init=False)
    _session_was_active: bool = field(default=False, init=False)
    _restart_scheduled: bool = field(default=False, init=False)
    _provider_failed: bool = field(default=False, init=False)
    _pending_monitor_restart: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.max_ticks is not None and self.max_ticks < 1:
            raise SupervisorError("max_ticks must be >= 1 when set.")

    @property
    def state(self) -> SupervisorState:
        return self._state

    @property
    def last_action(self) -> SupervisorAction | None:
        return self._last_action

    def record_transport(self, *, kind: str, at: datetime) -> None:
        self.tracker.record_transport_event(kind=kind, at=at, now=self.clock())

    def record_market(self, *, event_type: str, at: datetime) -> None:
        self.tracker.record_market_event(event_type=event_type, at=at, now=self.clock())

    async def run(self) -> SupervisorSummary:
        if self._state is not SupervisorState.IDLE:
            raise SupervisorError("supervisor.run() is not re-entrant.")
        try:
            while True:
                self._ticks += 1
                now = self.clock()
                try:
                    session = self.calendar.session_at(self.market, now)
                except MarketSessionError as exc:
                    self._provider_failed = True
                    self._state = SupervisorState.FAILED_CLOSED
                    self._last_action = SupervisorAction.FAILED_CLOSED
                    self._emit(
                        "calendar_provider_error",
                        None,
                        action=SupervisorAction.FAILED_CLOSED,
                        reason_code=str(exc)[:120],
                    )
                    return self._summary()
                except Exception:
                    self._provider_failed = True
                    self._state = SupervisorState.FAILED_CLOSED
                    self._last_action = SupervisorAction.FAILED_CLOSED
                    self._emit(
                        "calendar_provider_error",
                        None,
                        action=SupervisorAction.FAILED_CLOSED,
                        reason_code="provider_exception",
                    )
                    return self._summary()

                self._reap_finished(now)
                verdict = self.tracker.evaluate(session=session, now=now)
                action = self._decide_action(session, verdict)

                if action is SupervisorAction.FAILED_CLOSED:
                    self._state = SupervisorState.FAILED_CLOSED
                    self._last_action = action
                    await self._ensure_idle(now, session, reason_code="failed_closed")
                    self._emit_verdict(now, session, verdict, action)
                    return self._summary()

                if action is SupervisorAction.WAIT_FOR_CALENDAR:
                    self._state = SupervisorState.WAITING_CALENDAR
                    self._last_action = action
                    await self._ensure_idle(now, session, reason_code="calendar_missing")
                    self._emit_verdict(now, session, verdict, action)
                elif action in (
                    SupervisorAction.WAIT_FOR_SESSION,
                    SupervisorAction.STOP_FOR_SESSION,
                ):
                    self._state = SupervisorState.IDLE_CLOSED
                    self._last_action = action
                    await self._ensure_idle(now, session, reason_code="session_inactive")
                    self._emit_verdict(now, session, verdict, action)
                elif action is SupervisorAction.RESTART_TRANSPORT:
                    self._last_action = action
                    await self._restart_transport(now, session, verdict, action)
                elif action is SupervisorAction.HOLD_EXECUTION_ONLY:
                    self._last_action = action
                    if self._is_active(session):
                        await self._ensure_running(
                            now, session, is_restart=self._pending_monitor_restart
                        )
                    self._state = SupervisorState.RUNNING if self._task else SupervisorState.IDLE_CLOSED
                    self._emit_verdict(now, session, verdict, action)
                else:  # KEEP_RUNNING
                    self._last_action = action
                    if self._is_active(session):
                        await self._ensure_running(
                            now, session, is_restart=self._pending_monitor_restart
                        )
                    self._state = SupervisorState.RUNNING if self._task else SupervisorState.IDLE_CLOSED
                    self._emit_verdict(now, session, verdict, action)

                self._track_session_transition(session)

                if self.max_ticks is not None and self._ticks >= self.max_ticks:
                    await self._graceful_cancel(now, session, reason_code="max_ticks")
                    self._state = SupervisorState.STOPPED
                    return self._summary()
                await self.sleep(self.policy.poll_interval_seconds)
        except asyncio.CancelledError:
            await self._graceful_cancel(self.clock(), None, reason_code="supervisor_cancelled")
            self._state = SupervisorState.STOPPED
            raise
        finally:
            await self._graceful_cancel(self.clock(), None, reason_code="shutdown")

    def _track_session_transition(self, session: MarketSession) -> None:
        active = self._is_active(session)
        if self._session_was_active and not active:
            # 세션 종료 시 restart budget 초기화(새 session = 새 budget).
            self._restart_times.clear()
            self._restarts = 0
        self._session_was_active = active

    @staticmethod
    def _is_active(session: MarketSession) -> bool:
        return session.is_open

    def _decide_action(self, session: MarketSession, verdict: HealthVerdict) -> SupervisorAction:
        if self._provider_failed:
            return SupervisorAction.FAILED_CLOSED

        if session.is_calendar_missing or session.state is MarketSessionState.UNKNOWN:
            return SupervisorAction.WAIT_FOR_CALENDAR

        if session.calendar_reason is CalendarReason.PROVIDER_ERROR:
            return SupervisorAction.FAILED_CLOSED

        if session.state in (
            MarketSessionState.CLOSED,
            MarketSessionState.PRE_OPEN,
            MarketSessionState.POST_CLOSE,
        ):
            return SupervisorAction.WAIT_FOR_SESSION

        # OPEN 세션.
        transport = verdict.transport
        market = verdict.market_data

        if transport in (TransportHealthStatus.FLAPPING, TransportHealthStatus.UNHEALTHY):
            return SupervisorAction.RESTART_TRANSPORT

        if market in (MarketDataHealthStatus.STARVED, MarketDataHealthStatus.STALE):
            # market-data 결함 — transport 유지, execution만 hold.
            return SupervisorAction.HOLD_EXECUTION_ONLY

        if market is MarketDataHealthStatus.INVALID:
            return SupervisorAction.HOLD_EXECUTION_ONLY

        return SupervisorAction.KEEP_RUNNING

    async def _ensure_running(
        self, now: datetime, session: MarketSession, *, is_restart: bool
    ) -> None:
        if self._task is not None:
            self._state = SupervisorState.RUNNING
            return
        if is_restart and not self._restart_allowed(now):
            self._state = SupervisorState.FAILED_CLOSED
            self._last_action = SupervisorAction.FAILED_CLOSED
            return
        if is_restart and self.policy.restart_backoff_seconds > 0:
            self._emit(
                "restart_backoff",
                session,
                action=SupervisorAction.RESTART_TRANSPORT,
                backoff_seconds=self.policy.restart_backoff_seconds,
            )
            await self.sleep(self.policy.restart_backoff_seconds)
        try:
            monitor = self.monitor_factory()
        except Exception:
            self._state = SupervisorState.FAILED_CLOSED
            self._last_action = SupervisorAction.FAILED_CLOSED
            self._emit(
                "monitor_factory_error",
                session,
                action=SupervisorAction.FAILED_CLOSED,
                reason_code="factory_exception",
            )
            return

        if is_restart:
            self._restart_times.append(now)
            self._restarts += 1
            self._pending_monitor_restart = False
        else:
            self._initial_starts += 1

        self._task = asyncio.create_task(monitor.run())
        self._state = SupervisorState.RUNNING
        self._emit(
            "monitor_start",
            session,
            action=self._last_action or SupervisorAction.KEEP_RUNNING,
            reason_code="restart" if is_restart else "initial_start",
        )
        await asyncio.sleep(0)
        self._reap_finished(now)

    async def _restart_transport(
        self,
        now: datetime,
        session: MarketSession,
        verdict: HealthVerdict,
        action: SupervisorAction,
    ) -> None:
        if not self._restart_allowed(now):
            self._state = SupervisorState.FAILED_CLOSED
            self._last_action = SupervisorAction.FAILED_CLOSED
            self._emit_verdict(now, session, verdict, SupervisorAction.FAILED_CLOSED)
            return
        if self._task is not None:
            await self._graceful_cancel(now, session, reason_code="transport_restart")
        self._state = SupervisorState.BACKING_OFF
        await self._ensure_running(now, session, is_restart=True)
        self._pending_monitor_restart = False
        self._emit_verdict(now, session, verdict, action)

    async def _ensure_idle(
        self, now: datetime, session: MarketSession, *, reason_code: str
    ) -> None:
        if self._task is not None:
            await self._graceful_cancel(now, session, reason_code=reason_code)

    def _reap_finished(self, now: datetime) -> None:
        task = self._task
        if task is None or not task.done():
            return
        self._task = None
        if not task.cancelled():
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                self._state = SupervisorState.BACKING_OFF
                self._pending_monitor_restart = True
                self._emit(
                    "monitor_exit",
                    None,
                    action=SupervisorAction.RESTART_TRANSPORT,
                    reason_code="monitor_error",
                )
                return
        self._state = SupervisorState.BACKING_OFF
        self._pending_monitor_restart = True
        self._emit(
            "monitor_exit",
            None,
            action=SupervisorAction.RESTART_TRANSPORT,
            reason_code="monitor_stopped",
        )

    async def _graceful_cancel(
        self, now: datetime, session: MarketSession | None, *, reason_code: str
    ) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(task, return_exceptions=True)
        self._cancels += 1
        self._emit(
            "monitor_cancelled",
            session,
            action=self._last_action or SupervisorAction.STOP_FOR_SESSION,
            reason_code=reason_code,
        )

    def _restart_allowed(self, now: datetime) -> bool:
        cutoff = now - timedelta(seconds=self.policy.restart_window_seconds)
        self._restart_times = [t for t in self._restart_times if t >= cutoff]
        return self._restarts < self.policy.max_restarts_in_window

    def _emit_verdict(
        self,
        now: datetime,
        session: MarketSession,
        verdict: HealthVerdict,
        action: SupervisorAction,
    ) -> None:
        self._emit(
            "health_verdict",
            session,
            action=action,
            transport=str(verdict.transport),
            market_data=str(verdict.market_data),
            reason_code=verdict.reasons[0] if verdict.reasons else None,
            execution_ready=verdict.is_execution_ready,
        )

    def _emit(
        self,
        kind: str,
        session: MarketSession | None,
        *,
        action: SupervisorAction,
        reason_code: str | None = None,
        backoff_seconds: float | None = None,
        transport: str = "UNKNOWN",
        market_data: str = "UNKNOWN",
        execution_ready: bool = False,
    ) -> None:
        if self.on_evidence is None:
            return
        now = self.clock()
        session_state = str(session.state) if session is not None else "UNKNOWN"
        try:
            self.on_evidence(
                SupervisorEvidence(
                    timestamp=now,
                    state=self._state,
                    session_state=session_state,
                    transport=transport,
                    market_data=market_data,
                    action=str(action),
                    monitor_initial_starts=self._initial_starts,
                    monitor_restarts=self._restarts,
                    monitor_cancels=self._cancels,
                    kind=kind,
                    reason_code=reason_code,
                    backoff_seconds=backoff_seconds,
                    execution_ready=execution_ready,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if kind == "monitor_cancelled" or "cancel" in kind:
                return  # cancellation 경로: sink 오류 suppress, CancelledError 보존.
            self._state = SupervisorState.FAILED_CLOSED
            self._last_action = SupervisorAction.FAILED_CLOSED

    def _summary(self) -> SupervisorSummary:
        return SupervisorSummary(
            final_state=self._state,
            monitor_initial_starts=self._initial_starts,
            monitor_restarts=self._restarts,
            monitor_cancels=self._cancels,
            ticks=self._ticks,
            final_action=self._last_action,
        )
