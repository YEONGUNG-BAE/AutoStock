"""execution: 발화(fire)→주문(order) 경계의 영속 상태를 관리하는 패키지.

F1a 범위에서는 trigger fire journal(영속 발화 저널)만 구현한다.
F1b 에서 TriggerOrderBridge(발화→주문 경계)를 추가한다. bridge 는 journal/broker/ledger/
risk/paper_loop 를 *호출*만 하며 수정하지 않는다. RTM-5 real broker write-path 는 아직 미연결.
journal 모듈(trigger_journal/sqlite_trigger_journal)은 순수 SQLite 영속화이며
broker/ledger/network 를 import 하지 않는다(bridge 만 그것들을 호출한다).
"""

from __future__ import annotations

from execution.trigger_journal import (
    IdentityCollisionError,
    IllegalTransitionError,
    JournalResultStatus,
    JournalState,
    NonMonotonicTimestampError,
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
from execution.trigger_order_bridge import (
    BridgeCoherenceError,
    BridgeDependencyError,
    BridgeError,
    BridgeOutcome,
    BridgePreflightError,
    BridgeResult,
    FireBroker,
    FireBundle,
    FireDecision,
    FireGenerator,
    FireLedger,
    FirePlan,
    FireResolver,
    FireRiskInput,
    TriggerOrderBridge,
)
from execution.paper_execution_coordinator import (
    CoordinatorResult,
    CoordinatorStatus,
    PaperExecutionCoordinator,
)
from execution.paper_portfolio_context import (
    PaperPortfolioContextError,
    PaperPortfolioContextService,
    PaperPortfolioLedgerSource,
    PaperPortfolioPolicy,
    PaperPortfolioValuation,
    PortfolioMarketStateSource,
)

__all__ = [
    "BridgeCoherenceError",
    "BridgeDependencyError",
    "BridgeError",
    "BridgeOutcome",
    "BridgePreflightError",
    "BridgeResult",
    "CoordinatorResult",
    "CoordinatorStatus",
    "PaperExecutionCoordinator",
    "PaperPortfolioContextError",
    "PaperPortfolioContextService",
    "PaperPortfolioLedgerSource",
    "PaperPortfolioPolicy",
    "PaperPortfolioValuation",
    "PortfolioMarketStateSource",
    "FireBroker",
    "FireBundle",
    "FireDecision",
    "FireGenerator",
    "FireLedger",
    "FirePlan",
    "FireResolver",
    "FireRiskInput",
    "TriggerOrderBridge",
    "IdentityCollisionError",
    "IllegalTransitionError",
    "JournalResultStatus",
    "JournalState",
    "NonMonotonicTimestampError",
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
