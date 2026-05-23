from __future__ import annotations

from typing import Any

__all__ = [
    "DuplicateDecisionIdError",
    "SQLiteDecisionStore",
]


def __getattr__(name: str) -> Any:
    if name == "DuplicateDecisionIdError":
        from decision.sqlite_decision_store import DuplicateDecisionIdError

        return DuplicateDecisionIdError
    if name == "SQLiteDecisionStore":
        from decision.sqlite_decision_store import SQLiteDecisionStore

        return SQLiteDecisionStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
