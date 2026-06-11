"""Persistent trigger fire journal — 추상 계약(상태/에러/레코드/Protocol).

F1(영속 발화 저널) 문제: TriggerEngine의 발화 상태(_fires/_activation_epoch/
_cooldown_until/idempotency)는 in-memory per-process라 재시작 시 사라진다. broker write가
연결되면 같은 activation이 재발화하여 동일 주문을 이중 실행할 수 있다. journal은 발화→주문
경계에서 idempotency_key 단위로 상태를 영속화해 이 이중 실행을 막는다.

F1a 범위:
- journal repository + 상태 전이만 구현한다.
- broker/ledger/network를 호출하지 않는다(영속 SQLite 계산만).
- activation_epoch 복원은 v1에서 미지원(TriggerSignal이 노출하지 않음). 대신 bridge 경계에서
  max_fires_per_decision == 1을 강제한다는 가정 위에서 idempotency_key 단위로 단발 발화를 보장한다.

상태 기계: RESERVED → DISPATCHING → COMMITTED
                    └→ ABORTED (RESERVED에서 안전 취소)
                    DISPATCHING └→ UNCERTAIN (재시작/오류로 주문 결과 불명확)
COMMITTED / ABORTED / UNCERTAIN 은 terminal(자동 실행 기준). UNCERTAIN 은 자동 재시도 금지이며
operator reconciliation 으로만 mutate 한다(F1a에서는 reconcile API 미구현).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class JournalState(StrEnum):
    """발화 저널 상태."""

    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    COMMITTED = "committed"
    ABORTED = "aborted"
    UNCERTAIN = "uncertain"


TERMINAL_STATES: frozenset[JournalState] = frozenset(
    {JournalState.COMMITTED, JournalState.ABORTED, JournalState.UNCERTAIN}
)
NONTERMINAL_STATES: frozenset[JournalState] = frozenset(
    {JournalState.RESERVED, JournalState.DISPATCHING}
)


class ReserveOutcome(StrEnum):
    """reserve() 결과 분류."""

    RESERVED_NEW = "reserved_new"
    EXISTING_TERMINAL = "existing_terminal"
    EXISTING_PENDING = "existing_pending"


class TriggerJournalError(Exception):
    """trigger journal 공통 에러 base."""


class IllegalTransitionError(TriggerJournalError):
    """허용되지 않은 상태 전이."""


class RecordNotFoundError(TriggerJournalError):
    """존재하지 않는 idempotency_key 에 대한 전이 요청."""


class IdentityCollisionError(TriggerJournalError):
    """같은 idempotency_key 인데 identity field 가 다른 충돌(해시 충돌/논리 오류)."""


class OrderIdConflictError(TriggerJournalError):
    """이미 다른 fire 가 점유한 order_id 를 재사용하려는 시도."""


@dataclass(frozen=True, slots=True)
class TriggerJournalRecord:
    """저널 한 행의 immutable 뷰."""

    idempotency_key: str
    trigger_id: str
    decision_id: str
    plan_id: str
    market: str
    symbol: str
    action: str
    state: JournalState
    order_id: str | None
    result_status: str | None
    reason_code: str | None
    triggered_at: datetime
    reserved_at: datetime
    dispatching_at: datetime | None
    finalized_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReserveResult:
    """reserve() 반환값."""

    outcome: ReserveOutcome
    record: TriggerJournalRecord


@runtime_checkable
class TriggerFireSignal(Protocol):
    """reserve() 가 읽는 발화 신호의 최소 구조.

    market_data.TriggerSignal 이 구조적으로 이 Protocol 을 만족한다. journal 은 market_data 를
    import 하지 않으며 identity field 만 읽는다(quantity/side/credential/price 는 읽지 않는다).
    """

    idempotency_key: str
    trigger_id: str
    decision_id: object  # str 또는 .value(str) 를 가진 식별자(DecisionId 등)
    plan_id: str
    market: object  # Market enum 또는 str
    symbol: str
    action: object  # AnalysisAction enum 또는 str
    triggered_at: datetime


class TriggerJournal(Protocol):
    """발화 저널 repository 계약."""

    def reserve(self, signal: TriggerFireSignal, now: datetime) -> ReserveResult:
        """idempotency_key 로 RESERVED 행을 선점한다.

        - 신규: ReserveOutcome.RESERVED_NEW
        - 기존이 terminal: EXISTING_TERMINAL (재처리 skip 신호)
        - 기존이 nonterminal: EXISTING_PENDING (resume/abort 대상)
        identity field 불일치 시 IdentityCollisionError.
        """

    def mark_dispatching(
        self, idempotency_key: str, order_id: str, now: datetime
    ) -> TriggerJournalRecord:
        """RESERVED → DISPATCHING. order_id 는 전역 UNIQUE."""

    def mark_committed(
        self, idempotency_key: str, result_status: str, now: datetime
    ) -> TriggerJournalRecord:
        """DISPATCHING → COMMITTED."""

    def mark_aborted(
        self, idempotency_key: str, reason_code: str, now: datetime
    ) -> TriggerJournalRecord:
        """RESERVED → ABORTED (주문 미전송 안전 취소)."""

    def mark_uncertain(
        self, idempotency_key: str, reason_code: str, now: datetime
    ) -> TriggerJournalRecord:
        """DISPATCHING → UNCERTAIN (주문 결과 불명확, 자동 재시도 금지)."""

    def get(self, idempotency_key: str) -> TriggerJournalRecord | None:
        """단건 조회."""

    def list_nonterminal(self) -> tuple[TriggerJournalRecord, ...]:
        """RESERVED/DISPATCHING 행을 reserved_at 순으로 반환(재시작 복원용)."""
