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


def parse_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    """datetime 객체 또는 ISO-8601 timezone string을 timezone-aware datetime으로 변환한다."""
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        value = datetime.fromisoformat(normalized)
    return require_timezone_aware_datetime(value, field_name=field_name)
