"""RTM-7c.1 + RTM-7c.2 — offline orchestration (decision publication + fast-loop execution).

RTM-7c.1: 검증된 decision bundle 원자적 게시, slot 기반 refresh 스케줄.
RTM-7c.2: MarketMonitor post-apply hook → fast-loop paper execution orchestration.

offline library only — runtime activation NO-GO. network/KIS transport import 없음,
live broker 없음. broker/ledger/paper_execution/LLM/data adapter eager import 없음.
"""

from __future__ import annotations

__all__ = [
    # RTM-7c.1 — active decision store
    "ActiveBundle",
    "ActiveDecisionStore",
    "DecisionPublicationCandidate",
    "PublicationError",
    "PublicationResult",
    "PublicationStatus",
    # RTM-7c.2 — execution gate
    "ExecutionGateSnapshot",
    "ExecutionGateProvider",
    "SessionHealthExecutionGate",
    # RTM-7c.2 — fast-loop orchestration
    "ExecutionInputs",
    "ExecutionInputsProvider",
    "StaticExecutionInputsProvider",
    "FastLoopExecutionEvidence",
    "FastLoopExecutionOrchestrator",
    "FastLoopExecutionResult",
    "FastLoopExecutionStatus",
]

_STORE_EXPORTS = frozenset(
    {
        "ActiveBundle",
        "ActiveDecisionStore",
        "DecisionPublicationCandidate",
        "PublicationError",
        "PublicationResult",
        "PublicationStatus",
    }
)

_GATE_EXPORTS = frozenset(
    {
        "ExecutionGateSnapshot",
        "ExecutionGateProvider",
        "SessionHealthExecutionGate",
    }
)

_FAST_LOOP_EXPORTS = frozenset(
    {
        "ExecutionInputs",
        "ExecutionInputsProvider",
        "StaticExecutionInputsProvider",
        "FastLoopExecutionEvidence",
        "FastLoopExecutionOrchestrator",
        "FastLoopExecutionResult",
        "FastLoopExecutionStatus",
    }
)


def __getattr__(name: str):  # noqa: ANN202 - lazy re-export
    if name in _STORE_EXPORTS:
        from orchestration import active_decision_store as _store

        return getattr(_store, name)
    if name in _GATE_EXPORTS:
        from orchestration import execution_gate as _gate

        return getattr(_gate, name)
    if name in _FAST_LOOP_EXPORTS:
        from orchestration import fast_loop_execution as _fast

        return getattr(_fast, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
