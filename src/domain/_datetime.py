from __future__ import annotations

from datetime import datetime
from typing import Any


def require_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    """timezone-aware datetime만 허용한다."""

    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")

    return value
