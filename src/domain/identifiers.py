from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic_core import core_schema

from domain._decimal import to_decimal

# Date-ID canonical format: YYMMDD-N (예: 260522-1)
_DATE_ID_PATTERN = re.compile(r"^(\d{6})-(\d+)$")

# DecisionId 허용 문자: letters, digits, _, -, .
_DECISION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class Percent:
    """0~100 범위의 finite Decimal 비중 값을 표현한다."""

    __slots__ = ("_value",)

    def __init__(self, value: Any) -> None:
        parsed = to_decimal(value, field_name="percent")
        if parsed < Decimal("0") or parsed > Decimal("100"):
            raise ValueError("percent must be between 0 and 100.")
        self._value = parsed

    @property
    def value(self) -> Decimal:
        return self._value

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Percent):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return f"Percent('{self._value}')"

    def __str__(self) -> str:
        return str(self._value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: Any,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate_for_pydantic,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: str(value.value),
                return_schema=core_schema.str_schema(),
            ),
        )

    @classmethod
    def _validate_for_pydantic(cls, value: Any) -> Percent:
        if isinstance(value, Percent):
            return value
        return cls(value)


class DateId:
    """LLM 판단 근거 Date-ID. canonical value는 대괄호 없는 YYMMDD-N 문자열이다."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = _parse_date_id(value, field_name="date_id")

    @property
    def value(self) -> str:
        return self._value

    @classmethod
    def from_token(cls, token: str) -> DateId:
        """[260522-1] 또는 260522-1 토큰에서 DateId를 생성한다."""
        if not isinstance(token, str):
            raise ValueError("date_id token must be a string.")

        stripped = token.strip()
        if not stripped:
            raise ValueError("date_id token must not be blank.")

        if stripped.startswith("[") and stripped.endswith("]"):
            inner = stripped[1:-1].strip()
            if not inner:
                raise ValueError("date_id token must not be blank.")
            return cls(inner)

        return cls(stripped)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DateId):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return f"DateId('{self._value}')"

    def __str__(self) -> str:
        return self._value

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: Any,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate_for_pydantic,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: value.value,
                return_schema=core_schema.str_schema(),
            ),
        )

    @classmethod
    def _validate_for_pydantic(cls, value: Any) -> DateId:
        if isinstance(value, DateId):
            return value
        if not isinstance(value, str):
            raise ValueError("date_id must be a string.")
        return cls(value)


class DecisionId:
    """DecisionSnapshot 고유 ID. letters/digits/_/-/. 만 허용한다."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = _parse_decision_id(value, field_name="decision_id")

    @property
    def value(self) -> str:
        return self._value

    @classmethod
    def from_hash(cls, schema_name: str, payload_hash: str) -> DecisionId:
        """schema_name과 payload_hash로 deterministic DecisionId를 생성한다."""
        normalized_schema = _parse_non_blank_identifier(schema_name, field_name="schema_name")
        normalized_hash = _parse_non_blank_identifier(payload_hash, field_name="payload_hash")
        return cls(f"{normalized_schema}-{normalized_hash}")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DecisionId):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return f"DecisionId('{self._value}')"

    def __str__(self) -> str:
        return self._value

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: Any,
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate_for_pydantic,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: value.value,
                return_schema=core_schema.str_schema(),
            ),
        )

    @classmethod
    def _validate_for_pydantic(cls, value: Any) -> DecisionId:
        if isinstance(value, DecisionId):
            return value
        if not isinstance(value, str):
            raise ValueError("decision_id must be a string.")
        return cls(value)


def _parse_non_blank_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")

    if value != value.strip():
        raise ValueError(f"{field_name} must not contain leading or trailing whitespace.")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank.")

    if any(ch.isspace() for ch in normalized):
        raise ValueError(f"{field_name} must not contain whitespace.")

    return normalized


def _parse_decision_id(value: str, *, field_name: str) -> str:
    normalized = _parse_non_blank_identifier(value, field_name=field_name)
    if not _DECISION_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{field_name} may contain only letters, digits, '_', '-', and '.'."
        )
    return normalized


def _parse_date_id(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank.")

    # constructor는 bracket token을 허용하지 않는다. from_token()을 사용한다.
    if normalized.startswith("[") or normalized.endswith("]"):
        raise ValueError(f"{field_name} must use canonical format YYMMDD-N without brackets.")

    match = _DATE_ID_PATTERN.fullmatch(normalized)
    if match is None:
        raise ValueError(f"{field_name} must match canonical format YYMMDD-N.")

    yymmdd, sequence_text = match.groups()
    sequence = int(sequence_text)
    if sequence < 1:
        raise ValueError(f"{field_name} sequence must be at least 1.")

    yy = int(yymmdd[0:2])
    mm = int(yymmdd[2:4])
    dd = int(yymmdd[4:6])
    year = 2000 + yy
    try:
        date(year, mm, dd)
    except ValueError as exc:
        raise ValueError(f"{field_name} must contain a valid calendar date.") from exc

    return f"{yymmdd}-{sequence}"
