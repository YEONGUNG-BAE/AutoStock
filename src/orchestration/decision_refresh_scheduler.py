"""RTM-7c.1 — offline calendar-gated decision refresh scheduler (foundation).

하루 N개의 **외부 설정 slot** 시각에 맞춰 `DecisionRefreshRunner`를 호출하고, 그 결과
후보를 `ActiveDecisionStore`에 원자적으로 게시한다. slot 시각은 repo 계약에 없는
숨은 기본값을 두지 않고 **반드시 caller가 주입**한다(4회라는 값도 하드코딩하지 않는다).

정책(fail-closed):
  - 각 slot은 하루 1회만 실행한다(중복 실행 금지).
  - 시작 시점 이전에 지나간 과거 slot들은 MISSED 처리한다(catch-up burst 금지).
  - 가장 최근 due slot이 grace 안이면 1회만 실행하고, 그보다 오래되면 MISSED.
  - OPEN이 아닌 세션(CLOSED/PRE_OPEN/POST_CLOSE/UNKNOWN)에서는 갱신하지 않는다.
  - calendar provider 예외는 terminal FAILED_CLOSED로 표면화한다.
  - runner 실패/게시 실패는 해당 slot을 소비 처리하되(무한 재시도 금지) 직전 활성
    bundle을 유지하며, 성공으로 위장하지 않는다.
  - 다음 거래일이 되면 slot 실행 상태는 자연히 초기화된다(키가 (날짜, slot)).

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


class SchedulerError(Exception):
    """스케줄러 설정/계약 위반(빈 slot, naive 시각 등)."""


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
    RUNNER_FAILED = "runner_failed"  # runner.refresh 예외 — slot 소비, 직전 bundle 유지
    PUBLISH_FAILED = "publish_failed"  # 게시 거부/예외 — slot 소비, 직전 bundle 유지


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
    slots_published: int = 0
    runner_failures: int = 0
    publish_failures: int = 0
    final_state: SchedulerState = SchedulerState.STOPPED
    evidence: list[SchedulerEvidence] = field(default_factory=list)


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class DecisionRefreshScheduler:
    """calendar-gated 4-slot(설정 가능) 갱신 스케줄러. runner를 inline await로 호출해
    leaked background task가 생기지 않는다(취소는 깔끔히 전파된다)."""

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
        self._tz = timezone
        self._clock = clock
        self._sleep = sleep
        self._poll = poll_interval_seconds
        self._grace = slot_grace_seconds
        self._max_ticks = max_ticks
        self._on_evidence = on_evidence
        # (session_date, slot_id) → 이미 소비된 slot. 다음 날이 되면 키가 달라 자연 초기화.
        self._consumed: set[tuple[date, str]] = set()
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
                terminal = await self._tick(now, summary)
                if terminal:
                    summary.final_state = SchedulerState.FAILED_CLOSED
                    self._state = SchedulerState.FAILED_CLOSED
                    return summary
                await self._sleep(self._poll)
            summary.final_state = SchedulerState.STOPPED
            self._state = SchedulerState.STOPPED
            return summary
        except asyncio.CancelledError:
            self._state = SchedulerState.STOPPED
            raise

    async def _tick(self, now: datetime, summary: RefreshSummary) -> bool:
        """한 tick 처리. terminal(FAILED_CLOSED)이면 True를 반환한다."""
        try:
            session = self._calendar.session_at(self._market, now)
        except Exception:  # noqa: BLE001 - calendar provider 예외는 terminal fail-closed
            return True

        if session.state is not MarketSessionState.OPEN:
            # CLOSED/PRE_OPEN/POST_CLOSE/UNKNOWN(calendar missing 포함) → 갱신하지 않는다.
            return False

        session_date = now.astimezone(self._tz).date()
        due = self._due_slots(session_date, now)
        if not due:
            return False

        # 가장 최근 due slot만 실행 후보. 나머지는 catch-up 금지로 MISSED.
        most_recent_at, most_recent = due[-1]
        for scheduled_at, slot in due[:-1]:
            self._mark_missed(session_date, slot, scheduled_at, now, summary)

        if (now - most_recent_at).total_seconds() > self._grace:
            # 가장 최근 slot조차 grace를 넘겨 stale → 실행하지 않고 MISSED.
            self._mark_missed(session_date, most_recent, most_recent_at, now, summary)
            return False

        await self._run_slot(session_date, most_recent, most_recent_at, now, summary)
        return False

    def _due_slots(
        self, session_date: date, now: datetime
    ) -> list[tuple[datetime, SlotConfig]]:
        due: list[tuple[datetime, SlotConfig]] = []
        for slot in self._slots:
            if (session_date, slot.slot_id) in self._consumed:
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
        self._consumed.add((session_date, slot.slot_id))
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
        summary.slots_missed += 1
        self.last_outcome = RefreshSlotOutcome.MISSED

    async def _run_slot(
        self,
        session_date: date,
        slot: SlotConfig,
        scheduled_at: datetime,
        now: datetime,
        summary: RefreshSummary,
    ) -> None:
        # slot은 실행을 시도하는 순간 소비된다(성공/실패 무관, 무한 재시도 금지).
        self._consumed.add((session_date, slot.slot_id))
        try:
            candidate = await self._runner.refresh(
                market=self._market,
                session_date=session_date,
                slot_id=slot.slot_id,
                scheduled_at=scheduled_at,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - runner 실패는 slot 소비 + 직전 bundle 유지
            summary.runner_failures += 1
            self.last_outcome = RefreshSlotOutcome.RUNNER_FAILED
            self._record(
                summary,
                SchedulerEvidence(
                    at=now,
                    session_date=session_date,
                    slot_id=slot.slot_id,
                    scheduled_at=scheduled_at,
                    outcome=RefreshSlotOutcome.RUNNER_FAILED,
                    reason=str(exc),
                ),
            )
            return

        try:
            result = self._store.publish(candidate, now=now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 게시 예외는 slot 소비 + 직전 bundle 유지
            summary.publish_failures += 1
            self.last_outcome = RefreshSlotOutcome.PUBLISH_FAILED
            self._record(
                summary,
                SchedulerEvidence(
                    at=now,
                    session_date=session_date,
                    slot_id=slot.slot_id,
                    scheduled_at=scheduled_at,
                    outcome=RefreshSlotOutcome.PUBLISH_FAILED,
                    reason=str(exc),
                ),
            )
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
        else:
            # 게시 거부(conflict/older/expired/invalid)는 성공으로 위장하지 않는다.
            summary.publish_failures += 1
            self.last_outcome = RefreshSlotOutcome.PUBLISH_FAILED
            outcome = RefreshSlotOutcome.PUBLISH_FAILED
        self._record(
            summary,
            SchedulerEvidence(
                at=now,
                session_date=session_date,
                slot_id=slot.slot_id,
                scheduled_at=scheduled_at,
                outcome=outcome,
                publication_status=result.status,
                reason=result.reason,
            ),
        )

    def _record(self, summary: RefreshSummary, evidence: SchedulerEvidence) -> None:
        summary.evidence.append(evidence)
        if self._on_evidence is not None:
            self._on_evidence(evidence)
