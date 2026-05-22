from __future__ import annotations

from typing import Any


def normalize_required_string(value: Any, *, field_name: str) -> str:
    """식별자 문자열을 strip한 뒤 blank면 실패한다."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank.")

    return normalized
