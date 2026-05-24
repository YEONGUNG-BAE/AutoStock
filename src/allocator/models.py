from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from decision.canonical_json import canonicalize_payload
from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.identifiers import DateId, DecisionId, Percent

ALLOCATOR_DECISION_SCHEMA = "allocator_decision.v1"
SUMMARY_ONE_LINER_MAX_LENGTH = 200


def _parse_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    """datetime 객체 또는 ISO-8601 문자열을 timezone-aware datetime으로 변환한다."""
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        return require_timezone_aware_datetime(parsed, field_name=field_name)
    return require_timezone_aware_datetime(value, field_name=field_name)


class AssetBucket(StrEnum):
    """Allocator target weight 자산군 bucket."""

    KR = "kr"
    US = "us"
    GOLD = "gold"


class AllocatorAction(StrEnum):
    """Allocator 배분 조정 의도. 주문 side가 아니다."""

    KEEP = "keep"
    INCREASE = "increase"
    DECREASE = "decrease"
    HOLD = "hold"
    REBALANCE = "rebalance"


class GoldPolicyMode(StrEnum):
    """gold target band 적용 모드."""

    NORMAL = "normal"
    EXCEPTION = "exception"


class AllocationRegime(StrEnum):
    """Allocator risk/allocation regime. ExecutionMode와 별개다."""

    NORMAL = "normal"
    REBALANCING = "rebalancing"
    DEFENSIVE = "defensive"
    EMERGENCY = "emergency"


class AllocatorReason(BaseModel):
    """Allocator 판단 근거. 반드시 Date-ID를 인용한다."""

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


def _normalize_reasons(value: Any) -> tuple[AllocatorReason, ...]:
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, tuple):
        return value
    raise ValueError("reasons must be a list or tuple.")


def _validate_non_empty_reasons(reasons: tuple[AllocatorReason, ...], *, field_name: str) -> None:
    if not reasons:
        raise ValueError(f"{field_name} must contain at least one reason.")


class TargetWeights(BaseModel):
    """현금 제외 운용 자산 기준 KR/US/GOLD target weights."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kr: Percent
    us: Percent
    gold: Percent


class CashPolicy(BaseModel):
    """전체 계좌 기준 cash target policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cash_target_percent: Percent
    min_cash_percent: Percent | None = None
    max_cash_percent: Percent | None = None
    rationale: str
    reasons: tuple[AllocatorReason, ...]

    @field_validator("rationale", mode="before")
    @classmethod
    def validate_rationale(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="rationale")

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[AllocatorReason, ...]:
        return _normalize_reasons(value)

    @model_validator(mode="after")
    def validate_reasons_and_bounds(self) -> Self:
        _validate_non_empty_reasons(self.reasons, field_name="cash_policy.reasons")
        target = self.cash_target_percent.value
        if self.min_cash_percent is not None and self.max_cash_percent is not None:
            if self.min_cash_percent.value > self.max_cash_percent.value:
                raise ValueError("min_cash_percent must be <= max_cash_percent.")
        if self.min_cash_percent is not None and target < self.min_cash_percent.value:
            raise ValueError("cash_target_percent must be >= min_cash_percent.")
        if self.max_cash_percent is not None and target > self.max_cash_percent.value:
            raise ValueError("cash_target_percent must be <= max_cash_percent.")
        return self


class SignalSummary(BaseModel):
    """Allocator Signal Summary view. 아직 weight를 결정하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str
    reasons: tuple[AllocatorReason, ...]

    @field_validator("summary", mode="before")
    @classmethod
    def validate_summary(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="summary")

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[AllocatorReason, ...]:
        return _normalize_reasons(value)

    @model_validator(mode="after")
    def validate_non_empty_reasons(self) -> Self:
        _validate_non_empty_reasons(self.reasons, field_name="signal_summary.reasons")
        return self


class CashManagerView(BaseModel):
    """Allocator Cash Manager view."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str
    recommended_cash_percent: Percent
    reasons: tuple[AllocatorReason, ...]

    @field_validator("summary", mode="before")
    @classmethod
    def validate_summary(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="summary")

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[AllocatorReason, ...]:
        return _normalize_reasons(value)

    @model_validator(mode="after")
    def validate_non_empty_reasons(self) -> Self:
        _validate_non_empty_reasons(self.reasons, field_name="cash_manager.reasons")
        return self


class AssetAllocatorView(BaseModel):
    """Allocator Asset Allocator view."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str
    target_weights: TargetWeights
    reasons: tuple[AllocatorReason, ...]

    @field_validator("summary", mode="before")
    @classmethod
    def validate_summary(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="summary")

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[AllocatorReason, ...]:
        return _normalize_reasons(value)

    @model_validator(mode="after")
    def validate_non_empty_reasons(self) -> Self:
        _validate_non_empty_reasons(self.reasons, field_name="asset_allocator.reasons")
        return self


class ConsistencyCheckerView(BaseModel):
    """Allocator Consistency Checker view."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    summary: str
    issues: tuple[str, ...] = ()
    reasons: tuple[AllocatorReason, ...]

    @field_validator("summary", mode="before")
    @classmethod
    def validate_summary(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="summary")

    @field_validator("issues", mode="before")
    @classmethod
    def normalize_issues(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("issues must be a list or tuple.")
        normalized: list[str] = []
        for index, item in enumerate(value):
            normalized.append(normalize_required_string(item, field_name=f"issues[{index}]"))
        return tuple(normalized)

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[AllocatorReason, ...]:
        return _normalize_reasons(value)

    @model_validator(mode="after")
    def validate_non_empty_reasons(self) -> Self:
        _validate_non_empty_reasons(self.reasons, field_name="consistency_checker.reasons")
        return self


class AllocatorDecision(BaseModel):
    """Asset allocation LLM output schema. 개별 종목 주문 판단을 포함하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: DecisionId
    created_at: datetime
    schema_name: str = ALLOCATOR_DECISION_SCHEMA
    universe: str
    summary_one_liner: str
    gold_policy_mode: GoldPolicyMode
    signal_summary: SignalSummary
    cash_manager: CashManagerView
    asset_allocator: AssetAllocatorView
    consistency_checker: ConsistencyCheckerView
    cash_policy: CashPolicy
    target_weights: TargetWeights
    reasons: tuple[AllocatorReason, ...]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("decision_id", mode="before")
    @classmethod
    def validate_decision_id(cls, value: Any) -> DecisionId:
        if isinstance(value, DecisionId):
            return value
        if isinstance(value, str):
            return DecisionId(value)
        raise ValueError("decision_id must be a string or DecisionId.")

    @field_validator("schema_name", mode="before")
    @classmethod
    def validate_schema_name(cls, value: Any) -> str:
        normalized = normalize_required_string(value, field_name="schema_name")
        if normalized != ALLOCATOR_DECISION_SCHEMA:
            raise ValueError(f"schema_name must be {ALLOCATOR_DECISION_SCHEMA!r}.")
        return normalized

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

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[AllocatorReason, ...]:
        return _normalize_reasons(value)

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
    def validate_reasons_and_metadata(self) -> Self:
        _validate_non_empty_reasons(self.reasons, field_name="reasons")
        expected = canonicalize_payload(self.metadata)
        if self.metadata != expected:
            raise ValueError("metadata must be in canonical JSON-compatible form.")
        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic replay/저장을 위한 canonical dict 표현을 반환한다."""
        return canonicalize_payload(self.model_dump(mode="json"))
