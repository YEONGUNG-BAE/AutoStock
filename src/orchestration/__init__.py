"""RTM-7c.1 — offline decision publication + refresh orchestration.

이 패키지는 검증된 의사결정 bundle을 원자적으로 게시(publish)하고, 하루 N회
slot 기반으로 갱신을 스케줄하는 **오프라인** 계층이다. broker/ledger/paper_execution/
LLM/network/data adapter를 import하지 않는다(실행 배선은 RTM-7c.2 이후 별도 레인).
"""

from __future__ import annotations

__all__ = [
    "ActiveBundle",
    "ActiveDecisionStore",
    "DecisionPublicationCandidate",
    "PublicationError",
    "PublicationResult",
    "PublicationStatus",
]


def __getattr__(name: str):  # noqa: ANN202 - lazy re-export
    if name in __all__:
        from orchestration import active_decision_store as _store

        return getattr(_store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
