"""execution: 발화(fire)→주문(order) 경계의 영속 상태를 관리하는 패키지.

F1a 범위에서는 trigger fire journal(영속 발화 저널)만 구현한다.
broker bridge / RTM-5 write-path는 이 패키지에 아직 연결하지 않는다.
journal은 순수 SQLite 영속화이며 broker/ledger/network를 import 하지 않는다.
"""

from __future__ import annotations

from execution.trigger_journal import (
    IdentityCollisionError,
    IllegalTransitionError,
    JournalState,
    OrderIdConflictError,
    RecordNotFoundError,
    ReserveOutcome,
    ReserveResult,
    TERMINAL_STATES,
    TriggerFireSignal,
    TriggerJournal,
    TriggerJournalError,
    TriggerJournalRecord,
)
from execution.sqlite_trigger_journal import SqliteTriggerJournal

__all__ = [
    "IdentityCollisionError",
    "IllegalTransitionError",
    "JournalState",
    "OrderIdConflictError",
    "RecordNotFoundError",
    "ReserveOutcome",
    "ReserveResult",
    "SqliteTriggerJournal",
    "TERMINAL_STATES",
    "TriggerFireSignal",
    "TriggerJournal",
    "TriggerJournalError",
    "TriggerJournalRecord",
]
