from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from decision.canonical_json import canonicalize_payload
from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.identifiers import DateId, DecisionId, Percent

ANALYSIS_DECISION_SCHEMA = "analysis_decision.v1"
SUMMARY_ONE_LINER_MAX_LENGTH = 200


def _parse_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    """datetime 객체 또는 ISO-8601 문자열을 timezone-aware datetime으로 변환한다."""
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
        return require_timezone_aware_datetime(parsed, field_name=field_name)
    return require_timezone_aware_datetime(value, field_name=field_name)


class AnalysisAction(StrEnum):
    """종목 분석 최종 매매 의도. 주문 side가 아니다."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class AnalysisRole(StrEnum):
    """Analysis 4역할 식별자. nested view 이름과 대응한다."""

    BEAR = "bear"
    BULL = "bull"
    RISK_MANAGER = "risk_manager"
    FUND_MANAGER = "fund_manager"


class ConvictionLevel(StrEnum):
    """선택적 진단용 conviction level. hard filter로 사용하지 않는다."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AnalysisReason(BaseModel):
    """Analysis 판단 근거. 반드시 Date-ID를 인용한다."""

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


def _normalize_reasons(value: Any) -> tuple[AnalysisReason, ...]:
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, tuple):
        return value
    raise ValueError("reasons must be a list or tuple.")


def _validate_non_empty_reasons(reasons: tuple[AnalysisReason, ...], *, field_name: str) -> None:
    if not reasons:
        raise ValueError(f"{field_name} must contain at least one reason.")


def _normalize_string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field_name} must be a list or tuple.")
    normalized: list[str] = []
    for index, item in enumerate(value):
        normalized.append(normalize_required_string(item, field_name=f"{field_name}[{index}]"))
    return tuple(normalized)


def _validate_non_empty_string_tuple(items: tuple[str, ...], *, field_name: str) -> None:
    if not items:
        raise ValueError(f"{field_name} must contain at least one item.")


class BearPerspective(BaseModel):
    """Bear(약세) 관점. action을 직접 내지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str
    risks: tuple[str, ...]
    reasons: tuple[AnalysisReason, ...]

    @field_validator("summary", mode="before")
    @classmethod
    def validate_summary(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="summary")

    @field_validator("risks", mode="before")
    @classmethod
    def normalize_risks(cls, value: Any) -> tuple[str, ...]:
        return _normalize_string_tuple(value, field_name="risks")

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[AnalysisReason, ...]:
        return _normalize_reasons(value)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        _validate_non_empty_string_tuple(self.risks, field_name="bear.risks")
        _validate_non_empty_reasons(self.reasons, field_name="bear.reasons")
        return self


class BullPerspective(BaseModel):
    """Bull(강세) 관점. action을 직접 내지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str
    catalysts: tuple[str, ...]
    reasons: tuple[AnalysisReason, ...]

    @field_validator("summary", mode="before")
    @classmethod
    def validate_summary(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="summary")

    @field_validator("catalysts", mode="before")
    @classmethod
    def normalize_catalysts(cls, value: Any) -> tuple[str, ...]:
        return _normalize_string_tuple(value, field_name="catalysts")

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[AnalysisReason, ...]:
        return _normalize_reasons(value)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        _validate_non_empty_string_tuple(self.catalysts, field_name="bull.catalysts")
        _validate_non_empty_reasons(self.reasons, field_name="bull.reasons")
        return self


class RiskManagerEvaluation(BaseModel):
    """Risk Manager 평가 view. Phase 10 RiskFilter가 아니다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str
    risk_flags: tuple[str, ...] = ()
    max_weight_percent: Percent | None = None
    reasons: tuple[AnalysisReason, ...]

    @field_validator("summary", mode="before")
    @classmethod
    def validate_summary(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="summary")

    @field_validator("risk_flags", mode="before")
    @classmethod
    def normalize_risk_flags(cls, value: Any) -> tuple[str, ...]:
        return _normalize_string_tuple(value, field_name="risk_flags")

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[AnalysisReason, ...]:
        return _normalize_reasons(value)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        _validate_non_empty_reasons(self.reasons, field_name="risk_manager.reasons")
        return self


class FundManagerDecision(BaseModel):
    """Fund Manager 최종 종목 의사결정. OrderIntent가 아니다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: AnalysisAction
    target_weight_percent: Percent
    rationale: str
    reasons: tuple[AnalysisReason, ...]

    @field_validator("rationale", mode="before")
    @classmethod
    def validate_rationale(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="rationale")

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value: Any) -> tuple[AnalysisReason, ...]:
        return _normalize_reasons(value)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        _validate_non_empty_reasons(self.reasons, field_name="fund_manager.reasons")
        return self


class AnalysisDecision(BaseModel):
    """종목 분석 4역할 LLM output schema. 주문 필드를 포함하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: DecisionId
    created_at: datetime
    schema_name: str = ANALYSIS_DECISION_SCHEMA
    universe: str
    symbol: str
    market: str
    summary_one_liner: str
    bear: BearPerspective
    bull: BullPerspective
    risk_manager: RiskManagerEvaluation
    fund_manager: FundManagerDecision
    reasons: tuple[AnalysisReason, ...]
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
        if normalized != ANALYSIS_DECISION_SCHEMA:
            raise ValueError(f"schema_name must be {ANALYSIS_DECISION_SCHEMA!r}.")
        return normalized

    @field_validator("universe", "symbol", "market", "summary_one_liner", mode="before")
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
    def normalize_reasons(cls, value: Any) -> tuple[AnalysisReason, ...]:
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
