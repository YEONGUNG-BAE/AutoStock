"""RTM-7b.3 — calendar-gated bounded monitor supervisor (순수 asyncio; broker/ledger 미연결)."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
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
from market_data.monitor import MonitorExhaustedError, MonitorSummary

from domain.enums import Market

__all__ = [
    "MonitorExitReason",
    "SupervisorAction",
    "SupervisorInternalError",
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


class MonitorExitReason(StrEnum):
    NONE = "none"
    TRANSPORT_EXIT = "transport_exit"
    TRANSPORT_EXHAUSTED = "transport_exhausted"
    INTERNAL_FAILURE = "internal_failure"
    SESSION_CLOSED = "session_closed"
    CANCELLED = "cancelled"
    NORMAL_EOF = "normal_eof"


class SupervisorError(Exception):
    """supervisor 설정/정책 위반."""


class SupervisorInternalError(SupervisorError):
    """factory/evidence 등 내부 결함. sticky FAILED_CLOSED를 유발한다."""


@dataclass(frozen=True)
class SupervisorPolicy:
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
    async def run(self) -> MonitorSummary:  # pragma: no cover
        raise NotImplementedError

    @property
    def state(self) -> object:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True)
class SupervisorEvidence:
    timestamp: datetime
    state: SupervisorState
    session_state: str
    transport: str
    market_data: str
    action: str
    monitor_initial_starts: int
    monitor_restarts: int
    monitor_cancels: int
    restarts_in_current_window: int
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
    restarts_in_current_window: int
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
    _restart_times: deque[datetime] = field(default_factory=deque, init=False)
    _initial_starts: int = field(default=0, init=False)
    _total_restarts: int = field(default=0, init=False)
    _cancels: int = field(default=0, init=False)
    _ticks: int = field(default=0, init=False)
    _last_action: SupervisorAction | None = field(default=None, init=False)
    _session_was_active: bool = field(default=False, init=False)
    _terminal_failed: bool = field(default=False, init=False)
    _pending_exit_reason: MonitorExitReason = field(default=MonitorExitReason.NONE, init=False)
    _transport_restart_armed: bool = field(default=False, init=False)

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
        if kind == "connected":
            self._transport_restart_armed = False
        self.tracker.record_transport_event(kind=kind, at=at, now=self.clock())

    def record_market(self, *, event_type: str, at: datetime) -> None:
        self.tracker.record_market_event(event_type=event_type, at=at, now=self.clock())

    async def run(self) -> SupervisorSummary:
        if self._state is not SupervisorState.IDLE:
            raise SupervisorError("supervisor.run() is not re-entrant.")
        try:
            while True:
                if self._terminal_failed:
                    self._state = SupervisorState.FAILED_CLOSED
                    return self._summary()
                self._ticks += 1
                now = self.clock()
                try:
                    session = self.calendar.session_at(self.market, now)
                except MarketSessionError as exc:
                    self._enter_terminal_failed(reason_code=str(exc)[:120])
                    return self._summary()
                except Exception:
                    self._enter_terminal_failed(reason_code="provider_exception")
                    return self._summary()

                self._reap_finished(now)
                verdict = self.tracker.evaluate(session=session, now=now)
                action = self._decide_action(session, verdict)
                await self._apply_action(now, session, verdict, action)
                self._track_session_transition(session)

                if self._terminal_failed or self._state is SupervisorState.FAILED_CLOSED:
                    return self._summary()
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
            # terminal failure에서도 살아있는 monitor task는 반드시 정리한다(누수·fail-closed 모순 방지).
            # _graceful_cancel은 suppress_sink_errors=True로 emit하고, terminal 상태에서는
            # _emit이 조기 return하므로 sink 억제와 CancelledError 전파는 유지된다.
            await self._graceful_cancel(self.clock(), None, reason_code="shutdown")

    def _enter_terminal_failed(self, *, reason_code: str) -> None:
        if self._terminal_failed:
            return
        self._terminal_failed = True
        self._state = SupervisorState.FAILED_CLOSED
        self._last_action = SupervisorAction.FAILED_CLOSED
        if self.on_evidence is None:
            return
        now = self.clock()
        try:
            self.on_evidence(
                SupervisorEvidence(
                    timestamp=now,
                    state=self._state,
                    session_state="UNKNOWN",
                    transport="UNKNOWN",
                    market_data="UNKNOWN",
                    action=str(SupervisorAction.FAILED_CLOSED),
                    monitor_initial_starts=self._initial_starts,
                    monitor_restarts=self._total_restarts,
                    monitor_cancels=self._cancels,
                    restarts_in_current_window=self._restarts_in_current_window(now),
                    kind="internal_failure",
                    reason_code=reason_code,
                )
            )
        except Exception:
            pass  # terminal 경로: sink 오류를 재귀적으로 삼키지 않는다.

    async def _apply_action(
        self,
        now: datetime,
        session: MarketSession,
        verdict: HealthVerdict,
        action: SupervisorAction,
    ) -> None:
        if action is SupervisorAction.FAILED_CLOSED:
            self._state = SupervisorState.FAILED_CLOSED
            self._last_action = action
            await self._ensure_idle(now, session, reason_code="failed_closed")
            self._emit_verdict(now, session, verdict, action)
            return

        if action is SupervisorAction.WAIT_FOR_CALENDAR:
            self._state = SupervisorState.WAITING_CALENDAR
            self._last_action = action
            await self._ensure_idle(now, session, reason_code="calendar_missing")
            self._emit_verdict(now, session, verdict, action)
            return

        if action in (SupervisorAction.WAIT_FOR_SESSION, SupervisorAction.STOP_FOR_SESSION):
            self._state = SupervisorState.IDLE_CLOSED
            self._last_action = action
            await self._ensure_idle(now, session, reason_code="session_inactive")
            self._emit_verdict(now, session, verdict, action)
            return

        if action is SupervisorAction.RESTART_TRANSPORT:
            self._last_action = action
            await self._restart_transport(now, session, verdict, action)
            return

        # KEEP_RUNNING / HOLD_EXECUTION_ONLY — starvation은 monitor를 유지한다.
        self._last_action = action
        if self._terminal_failed:
            return
        if self._is_active(session):
            need_restart = self._pending_exit_reason in (
                MonitorExitReason.TRANSPORT_EXIT,
                MonitorExitReason.TRANSPORT_EXHAUSTED,
            )
            if need_restart and self._task is None:
                await self._ensure_running(now, session, is_restart=True)
            elif self._task is None and action is SupervisorAction.KEEP_RUNNING:
                await self._ensure_running(now, session, is_restart=False)
            elif self._task is None and action is SupervisorAction.HOLD_EXECUTION_ONLY:
                # transport session absent이지만 market starvation — restart 금지.
                if self._pending_exit_reason in (
                    MonitorExitReason.TRANSPORT_EXIT,
                    MonitorExitReason.TRANSPORT_EXHAUSTED,
                ):
                    await self._ensure_running(now, session, is_restart=True)
                else:
                    await self._ensure_running(now, session, is_restart=False)
            else:
                self._pending_exit_reason = MonitorExitReason.NONE
        if self._terminal_failed:
            return
        self._state = SupervisorState.RUNNING if self._task else SupervisorState.IDLE_CLOSED
        self._emit_verdict(now, session, verdict, action)

    def _track_session_transition(self, session: MarketSession) -> None:
        active = self._is_active(session)
        if self._session_was_active and not active:
            # 다음 OPEN epoch에서 restart window만 초기화(총 restart 수는 유지).
            self._restart_times.clear()
        self._session_was_active = active

    @staticmethod
    def _is_active(session: MarketSession) -> bool:
        return session.is_open

    def _decide_action(self, session: MarketSession, verdict: HealthVerdict) -> SupervisorAction:
        if self._terminal_failed:
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

        transport = verdict.transport
        market = verdict.market_data

        if transport in (TransportHealthStatus.FLAPPING, TransportHealthStatus.UNHEALTHY):
            if self._pending_exit_reason in (
                MonitorExitReason.TRANSPORT_EXIT,
                MonitorExitReason.TRANSPORT_EXHAUSTED,
            ):
                return SupervisorAction.RESTART_TRANSPORT
            if self._transport_restart_armed:
                if market in (
                    MarketDataHealthStatus.STARVED,
                    MarketDataHealthStatus.STALE,
                    MarketDataHealthStatus.INVALID,
                ):
                    return SupervisorAction.HOLD_EXECUTION_ONLY
                return SupervisorAction.KEEP_RUNNING
            return SupervisorAction.RESTART_TRANSPORT
        if market in (MarketDataHealthStatus.STARVED, MarketDataHealthStatus.STALE, MarketDataHealthStatus.INVALID):
            return SupervisorAction.HOLD_EXECUTION_ONLY
        return SupervisorAction.KEEP_RUNNING

    async def _ensure_running(
        self, now: datetime, session: MarketSession, *, is_restart: bool
    ) -> None:
        if self._task is not None:
            self._state = SupervisorState.RUNNING
            return
        if is_restart and not self._restart_allowed(now):
            self._enter_terminal_failed(reason_code="restart_budget_exhausted")
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
            self._enter_terminal_failed(reason_code="factory_exception")
            return

        if is_restart:
            self._total_restarts += 1
            self._restart_times.append(now)
            self._pending_exit_reason = MonitorExitReason.NONE
        else:
            self._initial_starts += 1
            self._pending_exit_reason = MonitorExitReason.NONE

        self._task = asyncio.create_task(monitor.run())
        self._state = SupervisorState.RUNNING
        self._emit(
            "monitor_start",
            session,
            action=self._last_action or SupervisorAction.KEEP_RUNNING,
            reason_code="transport_session_absent" if is_restart else "initial_start",
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
            self._enter_terminal_failed(reason_code="restart_budget_exhausted")
            self._emit_verdict(now, session, verdict, SupervisorAction.FAILED_CLOSED)
            return
        if self._task is not None:
            await self._graceful_cancel(now, session, reason_code="transport_restart")
        self._state = SupervisorState.BACKING_OFF
        self._transport_restart_armed = True
        await self._ensure_running(now, session, is_restart=True)
        if not self._terminal_failed:
            self._emit_verdict(now, session, verdict, action)

    async def _ensure_idle(
        self, now: datetime, session: MarketSession, *, reason_code: str
    ) -> None:
        if self._task is not None:
            await self._graceful_cancel(now, session, reason_code=reason_code)

    def _classify_exit(self, task: asyncio.Task[MonitorSummary]) -> MonitorExitReason:
        if task.cancelled():
            return MonitorExitReason.CANCELLED
        exc = task.exception()
        if exc is not None:
            if isinstance(exc, MonitorExhaustedError):
                return MonitorExitReason.TRANSPORT_EXHAUSTED
            return MonitorExitReason.INTERNAL_FAILURE
        return MonitorExitReason.TRANSPORT_EXIT

    def _reap_finished(self, now: datetime) -> None:
        task = self._task
        if task is None or not task.done():
            return
        self._task = None
        reason = self._classify_exit(task)
        if reason is MonitorExitReason.INTERNAL_FAILURE:
            self._pending_exit_reason = MonitorExitReason.NONE
            self._enter_terminal_failed(reason_code="monitor_internal_failure")
            return
        if reason is MonitorExitReason.CANCELLED:
            self._pending_exit_reason = MonitorExitReason.NONE
            return
        self._pending_exit_reason = reason
        self._state = SupervisorState.BACKING_OFF
        self._emit(
            "monitor_exit",
            None,
            action=SupervisorAction.RESTART_TRANSPORT,
            reason_code="transport_session_absent",
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
        self._pending_exit_reason = MonitorExitReason.NONE
        self._emit(
            "monitor_cancelled",
            session,
            action=self._last_action or SupervisorAction.STOP_FOR_SESSION,
            reason_code=reason_code,
            suppress_sink_errors=True,
        )

    def _restarts_in_current_window(self, now: datetime) -> int:
        cutoff = now - timedelta(seconds=self.policy.restart_window_seconds)
        while self._restart_times and self._restart_times[0] < cutoff:
            self._restart_times.popleft()
        return len(self._restart_times)

    def _restart_allowed(self, now: datetime) -> bool:
        return self._restarts_in_current_window(now) < self.policy.max_restarts_in_window

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
        suppress_sink_errors: bool = False,
    ) -> None:
        if self.on_evidence is None or self._terminal_failed:
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
                    monitor_restarts=self._total_restarts,
                    monitor_cancels=self._cancels,
                    restarts_in_current_window=self._restarts_in_current_window(now),
                    kind=kind,
                    reason_code=reason_code,
                    backoff_seconds=backoff_seconds,
                    execution_ready=execution_ready,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if suppress_sink_errors:
                return
            self._terminal_failed = True
            self._state = SupervisorState.FAILED_CLOSED
            self._last_action = SupervisorAction.FAILED_CLOSED

    def _summary(self) -> SupervisorSummary:
        now = self.clock()
        final_state = (
            SupervisorState.FAILED_CLOSED if self._terminal_failed else self._state
        )
        return SupervisorSummary(
            final_state=final_state,
            monitor_initial_starts=self._initial_starts,
            monitor_restarts=self._total_restarts,
            monitor_cancels=self._cancels,
            restarts_in_current_window=self._restarts_in_current_window(now),
            ticks=self._ticks,
            final_action=self._last_action,
        )
