from __future__ import annotations

from typing import Any

__all__ = [
    "DateIdValidator",
    "DuplicateDateIdError",
    "SQLiteDateIdSourceStore",
    "extract_date_ids_from_reasons",
]


def __getattr__(name: str) -> Any:
    if name == "DuplicateDateIdError":
        from data.date_id_store import DuplicateDateIdError

        return DuplicateDateIdError
    if name == "SQLiteDateIdSourceStore":
        from data.date_id_store import SQLiteDateIdSourceStore

        return SQLiteDateIdSourceStore
    if name == "DateIdValidator":
        from data.date_id_validator import DateIdValidator

        return DateIdValidator
    if name == "extract_date_ids_from_reasons":
        from data.date_id_validator import extract_date_ids_from_reasons

        return extract_date_ids_from_reasons
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
