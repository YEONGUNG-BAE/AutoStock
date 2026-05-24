from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from decision.canonical_json import canonicalize_payload
from domain._datetime import require_timezone_aware_datetime


def _parse_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    """datetime 객체 또는 ISO-8601 문자열을 timezone-aware datetime으로 변환한다."""
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        return require_timezone_aware_datetime(parsed, field_name=field_name)
    return require_timezone_aware_datetime(value, field_name=field_name)
from domain._strings import normalize_required_string
from domain.identifiers import DateId, DecisionId, Percent
from domain.source import FactType

# summary_one_liner 최대 길이 정책 (고정)
SUMMARY_ONE_LINER_MAX_LENGTH = 200


class ScoutReason(BaseModel):
    """Scout factor 내 단일 근거. 반드시 Date-ID를 인용한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    date_id: DateId
    source_name: str | None = None
    quote: str | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def validate_reason(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="reason")

    @field_validator("source_name", "quote", mode="before")
    @classmethod
    def validate_optional_strings(cls, value: Any, info) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name=info.field_name)


class ScoutFactor(BaseModel):
    """ScoutSummary 내 positive/negative/neutral 근거 묶음. 매매 action을 포함하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    summary: str
    reasons: tuple[ScoutReason, ...]
    strength: Percent | None = None

    @field_validator("name", "summary", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[ScoutReason, ...]:
        if isinstance(value, list):
            return tuple(value)
        if isinstance(value, tuple):
            return value
        raise ValueError("reasons must be a list or tuple.")

    @model_validator(mode="after")
    def validate_non_empty_reasons(self) -> Self:
        if not self.reasons:
            raise ValueError("ScoutFactor must contain at least one reason.")
        return self


class ScoutSummary(BaseModel):
    """Scout LLM/pipeline 출력 JSON schema. 투자 판단이 아닌 근거 요약만 담는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary_id: DecisionId
    created_at: datetime
    universe: str
    summary_one_liner: str
    positive_factors: tuple[ScoutFactor, ...] = ()
    negative_factors: tuple[ScoutFactor, ...] = ()
    neutral_factors: tuple[ScoutFactor, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("summary_id", mode="before")
    @classmethod
    def validate_summary_id(cls, value: Any) -> DecisionId:
        if isinstance(value, DecisionId):
            return value
        if isinstance(value, str):
            return DecisionId(value)
        raise ValueError("summary_id must be a string or DecisionId.")

    @field_validator("universe", "summary_one_liner", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return _parse_timezone_aware_datetime(value, field_name="created_at")

    @field_validator("summary_one_liner", mode="after")
    @classmethod
    def validate_summary_one_liner_length(cls, value: str) -> str:
        if len(value) > SUMMARY_ONE_LINER_MAX_LENGTH:
            raise ValueError(
                f"summary_one_liner must be at most {SUMMARY_ONE_LINER_MAX_LENGTH} characters."
            )
        return value

    @field_validator("positive_factors", "negative_factors", "neutral_factors", mode="before")
    @classmethod
    def normalize_factor_tuple(cls, value: Any) -> tuple[ScoutFactor, ...]:
        if value is None:
            return ()
        if isinstance(value, list):
            return tuple(value)
        if isinstance(value, tuple):
            return value
        raise ValueError("factors must be a list or tuple.")

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value

    @model_validator(mode="after")
    def validate_at_least_one_factor(self) -> Self:
        total = (
            len(self.positive_factors)
            + len(self.negative_factors)
            + len(self.neutral_factors)
        )
        if total < 1:
            raise ValueError(
                "ScoutSummary must contain at least one factor across all groups."
            )
        return self

    @model_validator(mode="after")
    def validate_canonical_metadata(self) -> Self:
        expected = canonicalize_payload(self.metadata)
        if self.metadata != expected:
            raise ValueError("metadata must be in canonical JSON-compatible form.")
        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic replay/저장을 위한 canonical dict 표현을 반환한다."""
        return canonicalize_payload(self.model_dump(mode="json"))


class ScoutInputRecord(BaseModel):
    """DateIdSourceRecord를 Scout 입력 payload 항목으로 표현한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date_id: DateId
    fact_type: FactType
    source_name: str
    source_timestamp: datetime
    summary: str
    symbol: str | None = None
    market: str | None = None
    source_url: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

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

    @field_validator("source_timestamp", mode="before")
    @classmethod
    def validate_source_timestamp(cls, value: Any) -> datetime:
        return _parse_timezone_aware_datetime(value, field_name="source_timestamp")

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value

    @model_validator(mode="after")
    def validate_canonical_payload(self) -> Self:
        expected = canonicalize_payload(self.payload)
        if self.payload != expected:
            raise ValueError("payload must be in canonical JSON-compatible form.")
        return self


class ScoutInput(BaseModel):
    """Scout LLM/downstream validation에 전달되는 deterministic 입력 payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    created_at: datetime
    universe: str
    records: tuple[ScoutInputRecord, ...]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("universe", mode="before")
    @classmethod
    def validate_universe(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="universe")

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return _parse_timezone_aware_datetime(value, field_name="created_at")

    @field_validator("records", mode="before")
    @classmethod
    def normalize_records(cls, value: Any) -> tuple[ScoutInputRecord, ...]:
        if isinstance(value, list):
            return tuple(value)
        if isinstance(value, tuple):
            return value
        raise ValueError("records must be a list or tuple.")

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value

    @model_validator(mode="after")
    def validate_canonical_metadata(self) -> Self:
        expected = canonicalize_payload(self.metadata)
        if self.metadata != expected:
            raise ValueError("metadata must be in canonical JSON-compatible form.")
        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic replay/저장을 위한 canonical dict 표현을 반환한다."""
        return canonicalize_payload(self.model_dump(mode="json"))
