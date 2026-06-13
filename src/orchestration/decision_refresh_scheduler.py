"""RTM-7c.1 — offline calendar-gated decision refresh scheduler (foundation).

하루 N개의 **외부 설정 slot** 시각에 맞춰 `DecisionRefreshRunner`를 호출하고, 그 결과
후보를 `ActiveDecisionStore`에 원자적으로 게시한다. slot 시각은 repo 계약에 없는
숨은 기본값을 두지 않고 **반드시 caller가 주입**한다(4회라는 값도 하드코딩하지 않는다).

slot 1회 실행 보장은 **durable journal**(`ActiveDecisionStore.decision_refresh_slots`)에
위임한다(프로세스 재시작 후에도 유지). in-memory set이 아니므로 재시작이 같은 slot을
다른 결정으로 재실행하지 못한다. runner 호출 직전 durable 예약(reserve)하고, 종료 시
durable 종료 기록(finalize)한다.

정책(fail-closed):
  - 각 slot은 하루 1회만 실행한다(durable journal로 중복 실행 금지).
  - 시작 시점 이전에 지나간 과거 slot들은 MISSED 처리한다(catch-up burst 금지).
  - 가장 최근 due slot이 grace 안이면 1회만 실행하고, 그보다 오래되면 MISSED.
  - OPEN이 아닌 세션(CLOSED/PRE_OPEN/POST_CLOSE/UNKNOWN)에서는 갱신하지 않는다.
  - 거래일인데 세션이 종료(POST_CLOSE/CLOSED)된 뒤 남은 due slot은
    MISSED_SESSION_CLOSED로 기록한다(의도된 비실행, 다음 거래일 catch-up 없음).
  - 재시작 시 잔존 RESERVED slot은 자동 재실행하지 않고 UNCERTAIN으로 reconcile한다.
  - calendar provider 예외는 terminal FAILED_CLOSED로 표면화한다.
  - runner 실패/게시 실패는 해당 slot을 종료 처리하되(무한 재시도 금지) 직전 활성
    bundle을 유지하며, 성공으로 위장하지 않는다. evidence에는 원시 예외 문자열을 넣지
    않고 reason 코드만 남긴다(payload/URL/credential 누출 방지).
  - evidence sink 예외는 sanitized terminal FAILED_CLOSED로 표면화하되, 취소는 보존한다.

network/broker/ledger/paper_execution/LLM 접근이 없다. runner는 Protocol로 주입한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from domain._datetime import require_timezone_aware_datetime
from domain.enums import Market
from market_data.market_session import (
    MarketCalendarProvider,
    MarketSessionState,
)
from orchestration.active_decision_store import (
    ActiveDecisionStore,
    DecisionPublicationCandidate,
    PublicationStatus,
    SlotReservationStatus,
    SlotState,
)

__all__ = [
    "DecisionPublicationCandidate",
    "DecisionRefreshRunner",
    "DecisionRefreshScheduler",
    "RefreshSlotOutcome",
    "RefreshSummary",
    "SchedulerError",
    "SchedulerEvidence",
    "SchedulerState",
    "SlotConfig",
]

# evidence에 남기는 reason 코드(원시 예외 문자열 금지: payload/URL/credential 누출 방지).
_RUNNER_DEPENDENCY_ERROR = "runner_dependency_error"
_PUBLICATION_DEPENDENCY_ERROR = "publication_dependency_error"
_DANGLING_RESERVED = "dangling_reserved_reconciled"
_SESSION_CLOSED = "session_closed"


class SchedulerError(Exception):
    """스케줄러 설정/계약 위반(빈 slot, naive 시각 등)."""


class _EvidenceSinkError(SchedulerError):
    """evidence sink 콜백 실패. run()이 sanitized terminal FAILED_CLOSED로 처리한다."""


class DecisionRefreshRunner(Protocol):
    """slot 시점에 갱신 후보를 생성하는 주입식 계약. 스케줄러는 후보만 받아 게시한다.

    구현체가 network/LLM/broker를 쓰든 말든 스케줄러는 알지 못한다(오프라인 레인에서는
    fake runner를 주입한다). 반환은 게시 가능한 `DecisionPublicationCandidate`다.
    """

    async def refresh(
        self,
        *,
        market: Market,
        session_date: date,
        slot_id: str,
        scheduled_at: datetime,
    ) -> DecisionPublicationCandidate: ...


@dataclass(frozen=True)
class SlotConfig:
    """하루 1개 갱신 slot의 식별자와 로컬 시각."""

    slot_id: str
    at: time


class RefreshSlotOutcome(StrEnum):
    RAN = "ran"  # runner 호출 + 게시 시도 완료(게시 상태는 evidence.publication_status)
    MISSED = "missed"  # catch-up 금지로 건너뛴 과거 slot
    MISSED_SESSION_CLOSED = "missed_session_closed"  # 세션 종료 후 남은 slot(의도된 비실행)
    RUNNER_FAILED = "runner_failed"  # runner.refresh 예외 — slot 종료, 직전 bundle 유지
    PUBLISH_FAILED = "publish_failed"  # 게시 거부/예외 — slot 종료, 직전 bundle 유지
    RECONCILED_UNCERTAIN = "reconciled_uncertain"  # 재시작 잔존 RESERVED → fail-closed


class SchedulerState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED_CLOSED = "failed_closed"


@dataclass(frozen=True)
class SchedulerEvidence:
    at: datetime
    session_date: date
    slot_id: str
    scheduled_at: datetime
    outcome: RefreshSlotOutcome
    publication_status: PublicationStatus | None = None
    reason: str | None = None


@dataclass
class RefreshSummary:
    ticks: int = 0
    slots_run: int = 0
    slots_missed: int = 0
    slots_missed_session_closed: int = 0
    slots_published: int = 0
    slots_reconciled: int = 0
    runner_failures: int = 0
    publish_failures: int = 0
    final_state: SchedulerState = SchedulerState.STOPPED
    evidence: list[SchedulerEvidence] = field(default_factory=list)


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class DecisionRefreshScheduler:
    """calendar-gated N-slot(설정 가능) 갱신 스케줄러. runner를 inline await로 호출해
    leaked background task가 생기지 않는다(취소는 깔끔히 전파된다). slot 1회 실행은
    durable journal로 보장하므로 프로세스 재시작에도 안전하다."""

    def __init__(
        self,
        *,
        market: Market,
        calendar: MarketCalendarProvider,
        runner: DecisionRefreshRunner,
        store: ActiveDecisionStore,
        slots: Sequence[SlotConfig],
        timezone: ZoneInfo,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = _default_sleep,
        poll_interval_seconds: float = 1.0,
        slot_grace_seconds: float = 300.0,
        max_ticks: int | None = None,
        on_evidence: Callable[[SchedulerEvidence], None] | None = None,
    ) -> None:
        if not slots:
            raise SchedulerError("at least one slot must be configured (no hidden default).")
        slot_ids = [s.slot_id for s in slots]
        if len(set(slot_ids)) != len(slot_ids):
            raise SchedulerError("slot ids must be unique.")
        if poll_interval_seconds <= 0:
            raise SchedulerError("poll_interval_seconds must be > 0.")
        if slot_grace_seconds < 0:
            raise SchedulerError("slot_grace_seconds must be >= 0.")
        self._market = market
        self._calendar = calendar
        self._runner = runner
        self._store = store
        self._slots = tuple(sorted(slots, key=lambda s: s.at))
        self._slot_by_id = {s.slot_id: s for s in self._slots}
        self._tz = timezone
        self._clock = clock
        self._sleep = sleep
        self._poll = poll_interval_seconds
        self._grace = slot_grace_seconds
        self._max_ticks = max_ticks
        self._on_evidence = on_evidence
        self._state = SchedulerState.RUNNING
        self.last_outcome: RefreshSlotOutcome | None = None

    @property
    def state(self) -> SchedulerState:
        return self._state

    async def run(self) -> RefreshSummary:
        summary = RefreshSummary(final_state=SchedulerState.RUNNING)
        try:
            while self._max_ticks is None or summary.ticks < self._max_ticks:
                summary.ticks += 1
                now = require_timezone_aware_datetime(self._clock(), field_name="now")
                try:
                    terminal = await self._tick(now, summary)
                except _EvidenceSinkError:
                    # evidence sink 실패는 sanitized terminal(원시 예외 문자열 누출 없음).
                    summary.final_state = SchedulerState.FAILED_CLOSED
                    self._state = SchedulerState.FAILED_CLOSED
                    return summary
                if terminal:
                    summary.final_state = SchedulerState.FAILED_CLOSED
                    self._state = SchedulerState.FAILED_CLOSED
                    return summary
                await self._sleep(self._poll)
            summary.final_state = SchedulerState.STOPPED
            self._state = SchedulerState.STOPPED
            return summary
        except asyncio.CancelledError:
            # 취소는 보존한다(sink 실패보다 우선). inline await이므로 leaked task 없음.
            self._state = SchedulerState.STOPPED
            raise

    async def _tick(self, now: datetime, summary: RefreshSummary) -> bool:
        """한 tick 처리. terminal(FAILED_CLOSED)이면 True를 반환한다."""
        try:
            session = self._calendar.session_at(self._market, now)
        except Exception:  # noqa: BLE001 - calendar provider 예외는 terminal fail-closed
            return True

        session_date = now.astimezone(self._tz).date()

        if session.state is MarketSessionState.OPEN:
            await self._tick_open(session_date, now, summary)
            return False

        if session.state in (MarketSessionState.POST_CLOSE, MarketSessionState.CLOSED):
            # 거래일인데 세션이 종료된 뒤 남은 due slot → MISSED_SESSION_CLOSED.
            # UNKNOWN(calendar missing)/PRE_OPEN/비거래일은 표시하지 않는다.
            try:
                trading = self._calendar.is_trading_day(self._market, session_date)
            except Exception:  # noqa: BLE001 - calendar provider 예외는 terminal fail-closed
                return True
            if trading:
                self._mark_session_closed(session_date, now, summary)
        return False

    async def _tick_open(
        self, session_date: date, now: datetime, summary: RefreshSummary
    ) -> None:
        states = self._reconcile_dangling(session_date, now, summary)
        due = self._due_slots(session_date, now, states)
        if not due:
            return

        # 가장 최근 due slot만 실행 후보. 나머지는 catch-up 금지로 MISSED.
        most_recent_at, most_recent = due[-1]
        for scheduled_at, slot in due[:-1]:
            self._mark_missed(session_date, slot, scheduled_at, now, summary)

        if (now - most_recent_at).total_seconds() > self._grace:
            # 가장 최근 slot조차 grace를 넘겨 stale → 실행하지 않고 MISSED.
            self._mark_missed(session_date, most_recent, most_recent_at, now, summary)
            return

        await self._run_slot(session_date, most_recent, most_recent_at, now, summary)

    def _reconcile_dangling(
        self, session_date: date, now: datetime, summary: RefreshSummary
    ) -> dict[str, SlotState]:
        """재시작 후 잔존 RESERVED slot을 UNCERTAIN으로 fail-closed reconcile한다.

        자동 재실행하지 않는다(같은 slot을 다른 결정으로 재실행하면 안 되므로). reconcile
        후 갱신된 slot 상태 맵을 반환한다."""
        states = self._store.slot_states(self._market, session_date)
        dangling = [sid for sid, st in states.items() if st is SlotState.RESERVED]
        if not dangling:
            return states
        for slot_id in dangling:
            scheduled_at = self._scheduled_at(session_date, slot_id, now)
            self._store.finalize_slot(
                market=self._market,
                session_date=session_date,
                slot_id=slot_id,
                scheduled_at=scheduled_at,
                state=SlotState.UNCERTAIN,
                now=now,
                outcome=_DANGLING_RESERVED,
            )
            summary.slots_reconciled += 1
            self.last_outcome = RefreshSlotOutcome.RECONCILED_UNCERTAIN
            self._record(
                summary,
                SchedulerEvidence(
                    at=now,
                    session_date=session_date,
                    slot_id=slot_id,
                    scheduled_at=scheduled_at,
                    outcome=RefreshSlotOutcome.RECONCILED_UNCERTAIN,
                    reason=_DANGLING_RESERVED,
                ),
            )
        return self._store.slot_states(self._market, session_date)

    def _scheduled_at(self, session_date: date, slot_id: str, fallback: datetime) -> datetime:
        slot = self._slot_by_id.get(slot_id)
        if slot is None:
            return fallback
        return datetime.combine(session_date, slot.at, tzinfo=self._tz)

    def _due_slots(
        self, session_date: date, now: datetime, states: dict[str, SlotState]
    ) -> list[tuple[datetime, SlotConfig]]:
        due: list[tuple[datetime, SlotConfig]] = []
        for slot in self._slots:
            # journal에 기록된 slot(종료/예약)은 소비된 것으로 본다(durable exactly-once).
            if slot.slot_id in states:
                continue
            scheduled_at = datetime.combine(session_date, slot.at, tzinfo=self._tz)
            if scheduled_at <= now:
                due.append((scheduled_at, slot))
        due.sort(key=lambda item: item[0])
        return due

    def _mark_missed(
        self,
        session_date: date,
        slot: SlotConfig,
        scheduled_at: datetime,
        now: datetime,
        summary: RefreshSummary,
    ) -> None:
        self._store.finalize_slot(
            market=self._market,
            session_date=session_date,
            slot_id=slot.slot_id,
            scheduled_at=scheduled_at,
            state=SlotState.MISSED,
            now=now,
            outcome=RefreshSlotOutcome.MISSED.value,
        )
        summary.slots_missed += 1
        self.last_outcome = RefreshSlotOutcome.MISSED
        self._record(
            summary,
            SchedulerEvidence(
                at=now,
                session_date=session_date,
                slot_id=slot.slot_id,
                scheduled_at=scheduled_at,
                outcome=RefreshSlotOutcome.MISSED,
            ),
        )

    def _mark_session_closed(
        self, session_date: date, now: datetime, summary: RefreshSummary
    ) -> None:
        """세션 종료(POST_CLOSE/CLOSED) 후 남은 due slot을 MISSED_SESSION_CLOSED로 기록."""
        states = self._reconcile_dangling(session_date, now, summary)
        for slot in self._slots:
            if slot.slot_id in states:
                continue
            scheduled_at = datetime.combine(session_date, slot.at, tzinfo=self._tz)
            if scheduled_at > now:
                continue  # 아직 도래하지 않은 slot은 표시하지 않는다.
            self._store.finalize_slot(
                market=self._market,
                session_date=session_date,
                slot_id=slot.slot_id,
                scheduled_at=scheduled_at,
                state=SlotState.MISSED_SESSION_CLOSED,
                now=now,
                outcome=_SESSION_CLOSED,
            )
            summary.slots_missed_session_closed += 1
            self.last_outcome = RefreshSlotOutcome.MISSED_SESSION_CLOSED
            self._record(
                summary,
                SchedulerEvidence(
                    at=now,
                    session_date=session_date,
                    slot_id=slot.slot_id,
                    scheduled_at=scheduled_at,
                    outcome=RefreshSlotOutcome.MISSED_SESSION_CLOSED,
                    reason=_SESSION_CLOSED,
                ),
            )

    async def _run_slot(
        self,
        session_date: date,
        slot: SlotConfig,
        scheduled_at: datetime,
        now: datetime,
        summary: RefreshSummary,
    ) -> None:
        # runner 호출 전에 durable 예약. 이미 종료/잔존-예약이면 재실행하지 않는다.
        reservation = self._store.reserve_slot(
            market=self._market,
            session_date=session_date,
            slot_id=slot.slot_id,
            scheduled_at=scheduled_at,
            now=now,
        )
        if reservation.status is SlotReservationStatus.ALREADY_TERMINAL:
            return
        if reservation.status is SlotReservationStatus.DANGLING_RESERVED:
            # 다른 프로세스가 예약만 남기고 죽음 → fail-closed reconcile, 재실행 금지.
            self._store.finalize_slot(
                market=self._market,
                session_date=session_date,
                slot_id=slot.slot_id,
                scheduled_at=scheduled_at,
                state=SlotState.UNCERTAIN,
                now=now,
                outcome=_DANGLING_RESERVED,
            )
            summary.slots_reconciled += 1
            self.last_outcome = RefreshSlotOutcome.RECONCILED_UNCERTAIN
            self._record(
                summary,
                SchedulerEvidence(
                    at=now,
                    session_date=session_date,
                    slot_id=slot.slot_id,
                    scheduled_at=scheduled_at,
                    outcome=RefreshSlotOutcome.RECONCILED_UNCERTAIN,
                    reason=_DANGLING_RESERVED,
                ),
            )
            return

        # RESERVED: 실행 진행. 취소 시 RESERVED 잔존 → 다음 시작에서 reconcile(fail-closed).
        try:
            candidate = await self._runner.refresh(
                market=self._market,
                session_date=session_date,
                slot_id=slot.slot_id,
                scheduled_at=scheduled_at,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - runner 실패는 slot 종료 + 직전 bundle 유지
            self._finalize_failure(
                session_date, slot, scheduled_at, now, summary,
                outcome=RefreshSlotOutcome.RUNNER_FAILED,
                reason=_RUNNER_DEPENDENCY_ERROR,
            )
            summary.runner_failures += 1
            return

        try:
            result = self._store.publish(candidate, now=now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 게시 예외는 slot 종료 + 직전 bundle 유지
            self._finalize_failure(
                session_date, slot, scheduled_at, now, summary,
                outcome=RefreshSlotOutcome.PUBLISH_FAILED,
                reason=_PUBLICATION_DEPENDENCY_ERROR,
            )
            summary.publish_failures += 1
            return

        published_ok = result.status in (
            PublicationStatus.PUBLISHED,
            PublicationStatus.IDEMPOTENT,
        )
        if published_ok:
            summary.slots_run += 1
            if result.status is PublicationStatus.PUBLISHED:
                summary.slots_published += 1
            self.last_outcome = RefreshSlotOutcome.RAN
            outcome = RefreshSlotOutcome.RAN
            self._store.finalize_slot(
                market=self._market,
                session_date=session_date,
                slot_id=slot.slot_id,
                scheduled_at=scheduled_at,
                state=SlotState.PUBLISHED,
                now=now,
                outcome=result.status.value,
                publication_id=result.publication_id,
            )
        else:
            # 게시 거부(conflict/older/expired/invalid)는 성공으로 위장하지 않는다.
            summary.publish_failures += 1
            self.last_outcome = RefreshSlotOutcome.PUBLISH_FAILED
            outcome = RefreshSlotOutcome.PUBLISH_FAILED
            self._store.finalize_slot(
                market=self._market,
                session_date=session_date,
                slot_id=slot.slot_id,
                scheduled_at=scheduled_at,
                state=SlotState.FAILED,
                now=now,
                outcome=result.status.value,
            )
        self._record(
            summary,
            SchedulerEvidence(
                at=now,
                session_date=session_date,
                slot_id=slot.slot_id,
                scheduled_at=scheduled_at,
                outcome=outcome,
                publication_status=result.status,
                # result.reason은 store가 만든 typed 메시지(원시 예외/payload 아님).
                reason=result.reason,
            ),
        )

    def _finalize_failure(
        self,
        session_date: date,
        slot: SlotConfig,
        scheduled_at: datetime,
        now: datetime,
        summary: RefreshSummary,
        *,
        outcome: RefreshSlotOutcome,
        reason: str,
    ) -> None:
        self.last_outcome = outcome
        self._store.finalize_slot(
            market=self._market,
            session_date=session_date,
            slot_id=slot.slot_id,
            scheduled_at=scheduled_at,
            state=SlotState.FAILED,
            now=now,
            outcome=reason,
        )
        self._record(
            summary,
            SchedulerEvidence(
                at=now,
                session_date=session_date,
                slot_id=slot.slot_id,
                scheduled_at=scheduled_at,
                outcome=outcome,
                reason=reason,
            ),
        )

    def _record(self, summary: RefreshSummary, evidence: SchedulerEvidence) -> None:
        summary.evidence.append(evidence)
        if self._on_evidence is None:
            return
        try:
            self._on_evidence(evidence)
        except Exception as exc:  # noqa: BLE001 - sink 실패는 sanitized terminal로 표면화
            # CancelledError는 BaseException이라 여기서 잡히지 않고 그대로 전파된다(취소 보존).
            raise _EvidenceSinkError("evidence sink callback failed") from exc
