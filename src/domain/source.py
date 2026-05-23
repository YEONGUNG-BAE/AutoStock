from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.identifiers import DateId


class FactType(StrEnum):
    """Date-ID가 나타내는 사실 종류. stale validation의 기본 단위다."""

    PRICE = "price"
    FLOW = "flow"
    FX = "fx"
    NEWS = "news"
    DISCLOSURE = "disclosure"
    MACRO = "macro"
    MANUAL = "manual"


def parse_fact_type(value: str) -> FactType:
    """저장된 fact_type 문자열을 FactType enum으로 복원한다."""
    normalized = normalize_required_string(value, field_name="fact_type")
    try:
        return FactType(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid fact_type: {normalized!r}") from exc


class DateIdSourceRecord(BaseModel):
    """하나의 Date-ID가 어떤 원천 사실/근거를 나타내는지 저장한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_id: DateId
    fact_type: FactType
    source_name: str
    source_timestamp: datetime
    created_at: datetime
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    symbol: str | None = None
    market: str | None = None
    source_url: str | None = None

    @field_validator("source_name", "summary", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("symbol", "market", "source_url", mode="before")
    @classmethod
    def validate_optional_strings(cls, value: Any, info) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("source_timestamp", "created_at", mode="before")
    @classmethod
    def validate_timezone_aware_datetimes(cls, value: Any, info) -> datetime:
        return require_timezone_aware_datetime(value, field_name=info.field_name)

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any) -> dict[str, Any]:
        from decision.canonical_json import canonicalize_payload

        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value

    @model_validator(mode="after")
    def validate_canonical_payload(self) -> Self:
        from decision.canonical_json import canonicalize_payload

        expected = canonicalize_payload(self.payload)
        if self.payload != expected:
            raise ValueError("payload must be in canonical JSON-compatible form.")
        return self
