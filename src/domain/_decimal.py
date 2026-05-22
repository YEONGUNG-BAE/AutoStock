from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def to_decimal(value: Any, *, field_name: str) -> Decimal:
    """숫자 입력을 문자열 기반 Decimal로 변환하고 finite 값만 허용한다."""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a numeric value, not bool.")

    if isinstance(value, Decimal):
        return _require_finite_decimal(value, field_name=field_name)

    if isinstance(value, int):
        return _require_finite_decimal(Decimal(value), field_name=field_name)

    if isinstance(value, float):
        return _parse_decimal_string(str(value), field_name=field_name)

    if isinstance(value, str):
        return _parse_decimal_string(value, field_name=field_name)

    raise ValueError(f"{field_name} must be a valid decimal.")


def to_optional_decimal(value: Any | None, *, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return to_decimal(value, field_name=field_name)


def _parse_decimal_string(value: str, *, field_name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be a valid decimal.") from exc
    return _require_finite_decimal(parsed, field_name=field_name)


def _require_finite_decimal(value: Decimal, *, field_name: str) -> Decimal:
    if not value.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal.")
    return value
