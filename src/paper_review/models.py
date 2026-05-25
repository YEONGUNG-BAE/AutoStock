from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from decision.canonical_json import canonicalize_payload, payload_sha256
from domain._datetime import require_timezone_aware_datetime
from domain._decimal import to_decimal, to_optional_decimal
from domain._strings import normalize_required_string
from domain.order import Fill, OrderIntent
from domain.portfolio import NavSnapshot
from emergency.models import EmergencyTriggerEvent
from logs.models import DailySummary
from postmortem.models import PostmortemRecord


class SampleSufficiency(StrEnum):
    """리뷰 기간 샘플 충분성."""

    INSUFFICIENT = "INSUFFICIENT"
    PARTIAL = "PARTIAL"
    SUFFICIENT = "SUFFICIENT"


class RecommendationType(StrEnum):
    """파라미터 변경 후보 유형."""

    KEEP = "KEEP"
    TIGHTEN = "TIGHTEN"
    LOOSEN = "LOOSEN"
    INVESTIGATE = "INVESTIGATE"
    OBSERVE_MORE = "OBSERVE_MORE"


class RecommendationActionability(StrEnum):
    """추천의 실행 가능성."""

    NOT_ACTIONABLE = "NOT_ACTIONABLE"
    OBSERVE_MORE = "OBSERVE_MORE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class ReviewConfidence(StrEnum):
    """추천 신뢰도."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def compute_sample_sufficiency(calendar_days: int) -> SampleSufficiency:
    """calendar_days 기준 deterministic sample sufficiency를 반환한다."""
    if calendar_days < 90:
        return SampleSufficiency.INSUFFICIENT
    if calendar_days < 180:
        return SampleSufficiency.PARTIAL
    return SampleSufficiency.SUFFICIENT


def compute_calendar_days(start_date: date, end_date: date) -> int:
    """start_date~end_date inclusive calendar day count를 반환한다."""
    return (end_date - start_date).days + 1


class ReviewPeriod(BaseModel):
    """paper review 대상 기간."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_date: date
    end_date: date
    trading_days: int | None = None
    calendar_days: int
    sample_sufficiency: SampleSufficiency

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def validate_dates(cls, value: Any, info) -> date:
        if isinstance(value, str):
            return date.fromisoformat(value)
        if isinstance(value, date):
            return value
        raise ValueError(f"{info.field_name} must be a date.")

    @field_validator("trading_days", mode="after")
    @classmethod
    def validate_trading_days(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("trading_days must be >= 0.")
        return value

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be <= end_date.")

        expected_calendar_days = compute_calendar_days(self.start_date, self.end_date)
        if self.calendar_days != expected_calendar_days:
            raise ValueError(
                f"calendar_days must equal inclusive range length: expected {expected_calendar_days}."
            )

        expected_sufficiency = compute_sample_sufficiency(self.calendar_days)
        if self.sample_sufficiency != expected_sufficiency:
            raise ValueError(
                f"sample_sufficiency must be {expected_sufficiency.value} for "
                f"{self.calendar_days} calendar days."
            )

        return self

    @classmethod
    def from_dates(
        cls,
        *,
        start_date: date,
        end_date: date,
        trading_days: int | None = None,
    ) -> ReviewPeriod:
        """start/end date에서 calendar_days와 sample_sufficiency를 deterministic하게 생성한다."""
        calendar_days = compute_calendar_days(start_date, end_date)
        return cls(
            start_date=start_date,
            end_date=end_date,
            trading_days=trading_days,
            calendar_days=calendar_days,
            sample_sufficiency=compute_sample_sufficiency(calendar_days),
        )


class ParameterRecommendation(BaseModel):
    """사람 검토용 파라미터 변경 후보. Phase 16에서는 auto_apply 금지."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recommendation_id: str
    parameter_name: str
    current_value: str
    candidate_value: str | None
    recommendation_type: RecommendationType
    actionability: RecommendationActionability
    evidence: tuple[str, ...]
    confidence_level: ReviewConfidence
    risk_of_change: str
    requires_human_approval: bool = True
    auto_apply: bool = False

    @field_validator("recommendation_id", "parameter_name", "current_value", "risk_of_change", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("candidate_value", mode="before")
    @classmethod
    def validate_candidate_value(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name="candidate_value")

    @field_validator("evidence", mode="before")
    @classmethod
    def validate_evidence(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("evidence must be a sequence of strings.")

        normalized: list[str] = []
        for index, item in enumerate(value):
            normalized.append(normalize_required_string(item, field_name=f"evidence[{index}]"))
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_recommendation_policy(self) -> Self:
        if self.auto_apply:
            raise ValueError("auto_apply must be False in Phase 16.")

        if not self.requires_human_approval:
            raise ValueError("requires_human_approval must be True in Phase 16.")

        if self.recommendation_type != RecommendationType.OBSERVE_MORE and not self.evidence:
            raise ValueError("evidence must be non-empty unless recommendation_type is OBSERVE_MORE.")

        if self.recommendation_type in {
            RecommendationType.KEEP,
            RecommendationType.INVESTIGATE,
            RecommendationType.OBSERVE_MORE,
        } and self.candidate_value is not None:
            # candidate_value may be None for these types — non-None is allowed but not required
            pass

        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic canonical dict 표현을 반환한다."""
        payload: dict[str, Any] = {
            "recommendation_id": self.recommendation_id,
            "parameter_name": self.parameter_name,
            "current_value": self.current_value,
            "candidate_value": self.candidate_value,
            "recommendation_type": self.recommendation_type.value,
            "actionability": self.actionability.value,
            "evidence": list(self.evidence),
            "confidence_level": self.confidence_level.value,
            "risk_of_change": self.risk_of_change,
            "requires_human_approval": self.requires_human_approval,
            "auto_apply": self.auto_apply,
        }
        return canonicalize_payload(payload)


class PaperPerformanceMetrics(BaseModel):
    """NAV 기반 paper 성과 지표."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_nav_krw: Decimal
    end_nav_krw: Decimal
    total_return_percent: Decimal
    annualized_return_percent: Decimal | None
    max_drawdown_percent: Decimal
    worst_daily_return_percent: Decimal | None
    best_daily_return_percent: Decimal | None
    volatility_daily_percent: Decimal | None
    cash_average_percent: Decimal | None
    invested_average_percent: Decimal | None
    nav_snapshot_count: int

    @field_validator(
        "start_nav_krw",
        "end_nav_krw",
        "total_return_percent",
        "max_drawdown_percent",
        mode="before",
    )
    @classmethod
    def validate_required_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="performance_decimal")

    @field_validator(
        "annualized_return_percent",
        "worst_daily_return_percent",
        "best_daily_return_percent",
        "volatility_daily_percent",
        "cash_average_percent",
        "invested_average_percent",
        mode="before",
    )
    @classmethod
    def validate_optional_decimals(cls, value: Any) -> Decimal | None:
        return to_optional_decimal(value, field_name="optional_performance_decimal")

    @field_validator("nav_snapshot_count", mode="after")
    @classmethod
    def validate_nav_snapshot_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("nav_snapshot_count must be >= 0.")
        return value

    @model_validator(mode="after")
    def validate_drawdown(self) -> Self:
        if self.max_drawdown_percent > Decimal("0"):
            raise ValueError("max_drawdown_percent must be <= 0.")
        return self


class ExecutionReviewMetrics(BaseModel):
    """paper execution 품질 지표."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_intent_count: int
    fill_count: int
    rejected_count: int
    manual_count: int
    emergency_count: int
    mdd_killswitch_count: int
    avg_fill_notional_krw: Decimal | None
    sell_count: int
    buy_count: int
    paper_fill_consistency_warnings: tuple[str, ...] = ()

    @field_validator("avg_fill_notional_krw", mode="before")
    @classmethod
    def validate_avg_fill_notional(cls, value: Any) -> Decimal | None:
        return to_optional_decimal(value, field_name="avg_fill_notional_krw")

    @field_validator(
        "order_intent_count",
        "fill_count",
        "rejected_count",
        "manual_count",
        "emergency_count",
        "mdd_killswitch_count",
        "sell_count",
        "buy_count",
        mode="after",
    )
    @classmethod
    def validate_non_negative_counts(cls, value: int, info) -> int:
        if value < 0:
            raise ValueError(f"{info.field_name} must be >= 0.")
        return value

    @field_validator("paper_fill_consistency_warnings", mode="before")
    @classmethod
    def validate_warnings(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("paper_fill_consistency_warnings must be a sequence of strings.")
        return tuple(str(item) for item in value)


class MddThresholdReview(BaseModel):
    """MDD threshold 검토 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_max_drawdown_percent: Decimal
    mdd_level_1_count: int
    mdd_level_2_count: int
    mdd_level_3_count: int
    false_positive_suspected_count: int = 0
    missed_risk_suspected_count: int = 0
    recommendations: tuple[ParameterRecommendation, ...] = ()

    @field_validator("observed_max_drawdown_percent", mode="before")
    @classmethod
    def validate_observed_drawdown(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="observed_max_drawdown_percent")

    @field_validator(
        "mdd_level_1_count",
        "mdd_level_2_count",
        "mdd_level_3_count",
        "false_positive_suspected_count",
        "missed_risk_suspected_count",
        mode="after",
    )
    @classmethod
    def validate_non_negative_counts(cls, value: int, info) -> int:
        if value < 0:
            raise ValueError(f"{info.field_name} must be >= 0.")
        return value


class AssetBandReview(BaseModel):
    """자산군 band 검토 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kr_band_breach_count: int
    us_band_breach_count: int
    gold_band_breach_count: int
    cash_band_breach_count: int
    time_in_band_percent: Decimal | None
    recovery_review_count: int
    recommendations: tuple[ParameterRecommendation, ...] = ()

    @field_validator("time_in_band_percent", mode="before")
    @classmethod
    def validate_time_in_band(cls, value: Any) -> Decimal | None:
        return to_optional_decimal(value, field_name="time_in_band_percent")

    @field_validator(
        "kr_band_breach_count",
        "us_band_breach_count",
        "gold_band_breach_count",
        "cash_band_breach_count",
        "recovery_review_count",
        mode="after",
    )
    @classmethod
    def validate_non_negative_counts(cls, value: int, info) -> int:
        if value < 0:
            raise ValueError(f"{info.field_name} must be >= 0.")
        return value


class AllocatorToleranceReview(BaseModel):
    """Allocator tolerance 검토 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allocator_fallback_count: int
    target_sum_invalid_count: int
    cash_target_out_of_range_count: int
    gold_target_out_of_range_count: int
    tolerance_breach_count: int
    recommendations: tuple[ParameterRecommendation, ...] = ()

    @field_validator(
        "allocator_fallback_count",
        "target_sum_invalid_count",
        "cash_target_out_of_range_count",
        "gold_target_out_of_range_count",
        "tolerance_breach_count",
        mode="after",
    )
    @classmethod
    def validate_non_negative_counts(cls, value: int, info) -> int:
        if value < 0:
            raise ValueError(f"{info.field_name} must be >= 0.")
        return value


class PaperReviewInput(BaseModel):
    """paper review 입력 bundle. store integration 없이 explicit tuple/list로 전달한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    created_at: datetime
    period: ReviewPeriod
    nav_snapshots: tuple[NavSnapshot, ...]
    daily_summaries: tuple[DailySummary, ...] = ()
    postmortem_records: tuple[PostmortemRecord, ...] = ()
    emergency_events: tuple[EmergencyTriggerEvent, ...] = ()
    order_intents: tuple[OrderIntent, ...] = ()
    fills: tuple[Fill, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("review_id", mode="before")
    @classmethod
    def validate_review_id(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="review_id")

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return require_timezone_aware_datetime(value, field_name="created_at")

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be a JSON-compatible object.")
        return canonicalize_payload(value)

    @model_validator(mode="before")
    @classmethod
    def canonicalize_nav_snapshots(cls, data: Any) -> Any:
        """nav_snapshots를 as_of 기준 deterministic sort한다."""
        if not isinstance(data, dict):
            return data

        nav_snapshots = data.get("nav_snapshots")
        if nav_snapshots is None:
            return data

        if isinstance(nav_snapshots, (list, tuple)):
            sorted_items = sorted(
                nav_snapshots,
                key=lambda item: (
                    item.as_of.isoformat()
                    if hasattr(item, "as_of")
                    else NavSnapshot.model_validate(item).as_of.isoformat()
                ),
            )
            data["nav_snapshots"] = sorted_items

        return data

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        if self.metadata != canonicalize_payload(self.metadata):
            raise ValueError("metadata must be in canonical JSON-compatible form.")

        snapshot_ids: set[str] = set()
        for snapshot in self.nav_snapshots:
            if snapshot.snapshot_id in snapshot_ids:
                raise ValueError(f"duplicate nav snapshot_id: {snapshot.snapshot_id}")
            snapshot_ids.add(snapshot.snapshot_id)

            snapshot_date = snapshot.as_of.date()
            if not (self.period.start_date <= snapshot_date <= self.period.end_date):
                raise ValueError(
                    f"nav snapshot {snapshot.snapshot_id} as_of {snapshot_date.isoformat()} "
                    f"falls outside review period."
                )

        for summary in self.daily_summaries:
            if not (self.period.start_date <= summary.trading_date <= self.period.end_date):
                raise ValueError(
                    f"daily summary {summary.summary_id} trading_date "
                    f"{summary.trading_date.isoformat()} falls outside review period."
                )

        for record in self.postmortem_records:
            if record.evaluated_end_date < self.period.start_date:
                raise ValueError(
                    f"postmortem {record.postmortem_id} evaluated_end_date "
                    f"falls before review period start."
                )
            if record.evaluated_start_date > self.period.end_date:
                raise ValueError(
                    f"postmortem {record.postmortem_id} evaluated_start_date "
                    f"falls after review period end."
                )

        for event in self.emergency_events:
            event_date = event.created_at.date()
            if not (self.period.start_date <= event_date <= self.period.end_date):
                raise ValueError(
                    f"emergency event {event.event_id} created_at "
                    f"{event_date.isoformat()} falls outside review period."
                )

        return self


class PaperReviewReport(BaseModel):
    """deterministic paper review report. config 변경/자동 적용 없음."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: str
    created_at: datetime
    period: ReviewPeriod
    sample_sufficiency: SampleSufficiency
    performance_metrics: PaperPerformanceMetrics
    execution_metrics: ExecutionReviewMetrics
    mdd_threshold_review: MddThresholdReview
    asset_band_review: AssetBandReview
    allocator_tolerance_review: AllocatorToleranceReview
    postmortem_top_error_tags: tuple[str, ...] = ()
    emergency_trigger_counts: dict[str, int] = Field(default_factory=dict)
    data_quality_warnings: tuple[str, ...] = ()
    recommendations: tuple[ParameterRecommendation, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("review_id", mode="before")
    @classmethod
    def validate_review_id(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="review_id")

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return require_timezone_aware_datetime(value, field_name="created_at")

    @field_validator("emergency_trigger_counts", mode="before")
    @classmethod
    def validate_emergency_counts(cls, value: Any) -> dict[str, int]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("emergency_trigger_counts must be a mapping.")
        normalized: dict[str, int] = {}
        for key, count in value.items():
            if not isinstance(key, str):
                raise ValueError("emergency_trigger_counts keys must be strings.")
            if not isinstance(count, int) or count < 0:
                raise ValueError(f"emergency_trigger_counts[{key!r}] must be a non-negative int.")
            normalized[key] = count
        return {key: normalized[key] for key in sorted(normalized)}

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be a JSON-compatible object.")
        return canonicalize_payload(value)

    @field_validator("data_quality_warnings", mode="before")
    @classmethod
    def validate_warnings(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("data_quality_warnings must be a sequence of strings.")
        return tuple(str(item) for item in value)

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        expected_metadata = canonicalize_payload(self.metadata)
        if self.metadata != expected_metadata:
            raise ValueError("metadata must be in canonical JSON-compatible form.")

        if self.sample_sufficiency != self.period.sample_sufficiency:
            raise ValueError("sample_sufficiency must match period.sample_sufficiency.")

        for recommendation in self.recommendations:
            if recommendation.auto_apply:
                raise ValueError("report recommendations must have auto_apply=False.")
            if not recommendation.requires_human_approval:
                raise ValueError("report recommendations must require human approval.")

        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic canonical dict 표현을 반환한다."""
        payload: dict[str, Any] = {
            "review_id": self.review_id,
            "created_at": self.created_at.isoformat(),
            "period": {
                "start_date": self.period.start_date.isoformat(),
                "end_date": self.period.end_date.isoformat(),
                "trading_days": self.period.trading_days,
                "calendar_days": self.period.calendar_days,
                "sample_sufficiency": self.period.sample_sufficiency.value,
            },
            "sample_sufficiency": self.sample_sufficiency.value,
            "performance_metrics": {
                "start_nav_krw": str(self.performance_metrics.start_nav_krw),
                "end_nav_krw": str(self.performance_metrics.end_nav_krw),
                "total_return_percent": str(self.performance_metrics.total_return_percent),
                "annualized_return_percent": (
                    str(self.performance_metrics.annualized_return_percent)
                    if self.performance_metrics.annualized_return_percent is not None
                    else None
                ),
                "max_drawdown_percent": str(self.performance_metrics.max_drawdown_percent),
                "worst_daily_return_percent": (
                    str(self.performance_metrics.worst_daily_return_percent)
                    if self.performance_metrics.worst_daily_return_percent is not None
                    else None
                ),
                "best_daily_return_percent": (
                    str(self.performance_metrics.best_daily_return_percent)
                    if self.performance_metrics.best_daily_return_percent is not None
                    else None
                ),
                "volatility_daily_percent": (
                    str(self.performance_metrics.volatility_daily_percent)
                    if self.performance_metrics.volatility_daily_percent is not None
                    else None
                ),
                "cash_average_percent": (
                    str(self.performance_metrics.cash_average_percent)
                    if self.performance_metrics.cash_average_percent is not None
                    else None
                ),
                "invested_average_percent": (
                    str(self.performance_metrics.invested_average_percent)
                    if self.performance_metrics.invested_average_percent is not None
                    else None
                ),
                "nav_snapshot_count": self.performance_metrics.nav_snapshot_count,
            },
            "execution_metrics": {
                "order_intent_count": self.execution_metrics.order_intent_count,
                "fill_count": self.execution_metrics.fill_count,
                "rejected_count": self.execution_metrics.rejected_count,
                "manual_count": self.execution_metrics.manual_count,
                "emergency_count": self.execution_metrics.emergency_count,
                "mdd_killswitch_count": self.execution_metrics.mdd_killswitch_count,
                "avg_fill_notional_krw": (
                    str(self.execution_metrics.avg_fill_notional_krw)
                    if self.execution_metrics.avg_fill_notional_krw is not None
                    else None
                ),
                "sell_count": self.execution_metrics.sell_count,
                "buy_count": self.execution_metrics.buy_count,
                "paper_fill_consistency_warnings": list(
                    self.execution_metrics.paper_fill_consistency_warnings
                ),
            },
            "mdd_threshold_review": {
                "observed_max_drawdown_percent": str(
                    self.mdd_threshold_review.observed_max_drawdown_percent
                ),
                "mdd_level_1_count": self.mdd_threshold_review.mdd_level_1_count,
                "mdd_level_2_count": self.mdd_threshold_review.mdd_level_2_count,
                "mdd_level_3_count": self.mdd_threshold_review.mdd_level_3_count,
                "false_positive_suspected_count": (
                    self.mdd_threshold_review.false_positive_suspected_count
                ),
                "missed_risk_suspected_count": (
                    self.mdd_threshold_review.missed_risk_suspected_count
                ),
                "recommendations": [
                    item.to_canonical_dict()
                    for item in self.mdd_threshold_review.recommendations
                ],
            },
            "asset_band_review": {
                "kr_band_breach_count": self.asset_band_review.kr_band_breach_count,
                "us_band_breach_count": self.asset_band_review.us_band_breach_count,
                "gold_band_breach_count": self.asset_band_review.gold_band_breach_count,
                "cash_band_breach_count": self.asset_band_review.cash_band_breach_count,
                "time_in_band_percent": (
                    str(self.asset_band_review.time_in_band_percent)
                    if self.asset_band_review.time_in_band_percent is not None
                    else None
                ),
                "recovery_review_count": self.asset_band_review.recovery_review_count,
                "recommendations": [
                    item.to_canonical_dict() for item in self.asset_band_review.recommendations
                ],
            },
            "allocator_tolerance_review": {
                "allocator_fallback_count": (
                    self.allocator_tolerance_review.allocator_fallback_count
                ),
                "target_sum_invalid_count": (
                    self.allocator_tolerance_review.target_sum_invalid_count
                ),
                "cash_target_out_of_range_count": (
                    self.allocator_tolerance_review.cash_target_out_of_range_count
                ),
                "gold_target_out_of_range_count": (
                    self.allocator_tolerance_review.gold_target_out_of_range_count
                ),
                "tolerance_breach_count": (
                    self.allocator_tolerance_review.tolerance_breach_count
                ),
                "recommendations": [
                    item.to_canonical_dict()
                    for item in self.allocator_tolerance_review.recommendations
                ],
            },
            "postmortem_top_error_tags": list(self.postmortem_top_error_tags),
            "emergency_trigger_counts": dict(self.emergency_trigger_counts),
            "data_quality_warnings": list(self.data_quality_warnings),
            "recommendations": [item.to_canonical_dict() for item in self.recommendations],
        }

        if self.metadata:
            payload["metadata"] = canonicalize_payload(self.metadata)

        return canonicalize_payload(payload)

    def payload_hash(self) -> str:
        """canonical payload sha256 hex digest를 반환한다."""
        return payload_sha256(self.to_canonical_dict())
