from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from allocator.models import ALLOCATOR_DECISION_SCHEMA, AllocatorDecision
from analysis.models import ANALYSIS_DECISION_SCHEMA, AnalysisDecision
from decision.canonical_json import canonicalize_payload
from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.decision import DecisionSnapshot
from domain.enums import AccountRole, Currency, Market
from domain.identifiers import DecisionId
from domain.market import MarketPrice
from domain.order import Fill, OrderIntent, OrderResult
from domain.portfolio import NavSnapshot
from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity
from risk.models import RiskFilterContext
from risk.order_generation import OrderGenerationResult
from scout.models import ScoutSummary

PAPER_LOOP_SCHEMA = "paper_loop.v1"
PAPER_LOOP_VALIDATOR_VERSION = "phase11"

# --- issue codes ---
PAPER_LOOP_QUANTITY_RESOLVED = "PAPER_LOOP_QUANTITY_RESOLVED"
PAPER_LOOP_NO_EXECUTABLE_QUANTITY = "PAPER_LOOP_NO_EXECUTABLE_QUANTITY"
PAPER_LOOP_QUANTITY_CONTEXT_MISSING = "PAPER_LOOP_QUANTITY_CONTEXT_MISSING"
PAPER_LOOP_QUANTITY_CURRENCY_MISMATCH = "PAPER_LOOP_QUANTITY_CURRENCY_MISMATCH"
PAPER_LOOP_UNSUPPORTED_ORDER_TYPE = "PAPER_LOOP_UNSUPPORTED_ORDER_TYPE"
PAPER_LOOP_INVALID_TARGET_WEIGHT_INTENT = "PAPER_LOOP_INVALID_TARGET_WEIGHT_INTENT"
PAPER_LOOP_DUPLICATE_SNAPSHOT = "PAPER_LOOP_DUPLICATE_SNAPSHOT"
PAPER_LOOP_NOT_PAPER_MODE = "PAPER_LOOP_NOT_PAPER_MODE"
PAPER_LOOP_INPUT_VALIDATION_FAILED = "PAPER_LOOP_INPUT_VALIDATION_FAILED"


class QuantityResolutionStatus(StrEnum):
    """target_weight_percent → quantity 변환 결과."""

    RESOLVED = "resolved"
    NOOP = "noop"
    FAILED = "failed"


class PaperLoopStatus(StrEnum):
    """Paper E2E loop 최종 실행 상태."""

    FILLED = "filled"
    BROKER_REJECTED = "broker_rejected"
    RISK_BLOCKED = "risk_blocked"
    NOOP = "noop"
    QUANTITY_FAILED = "quantity_failed"
    VALIDATION_FAILED = "validation_failed"


class QuantityResolutionResult(BaseModel):
    """QuantityResolver 실행 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: QuantityResolutionStatus
    order_intent: OrderIntent | None = None
    validation_result: ValidationResult


class PaperLoopInput(BaseModel):
    """Phase 7~10 산출물과 paper execution context를 하나로 묶는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: DecisionId | str
    created_at: datetime
    scout_summary: ScoutSummary | None = None
    allocator_decision: AllocatorDecision
    analysis_decision: AnalysisDecision
    risk_context: RiskFilterContext
    market_price: MarketPrice
    broker_account_role: AccountRole = AccountRole.PAPER
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id", mode="before")
    @classmethod
    def validate_run_id(cls, value: Any) -> DecisionId | str:
        if isinstance(value, DecisionId):
            return value
        return normalize_required_string(value, field_name="run_id")

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="created_at")

    @field_validator("correlation_id", mode="before")
    @classmethod
    def validate_correlation_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name="correlation_id")

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
    def validate_paper_loop_input(self) -> Self:
        if self.broker_account_role != AccountRole.PAPER:
            raise ValueError("PaperLoopInput broker_account_role must be PAPER.")

        analysis = self.analysis_decision
        if self.market_price.symbol != analysis.symbol:
            raise ValueError(
                "market_price.symbol must match analysis_decision.symbol."
            )
        market_str = analysis.market.upper()
        expected_market = Market(market_str)
        if self.market_price.market != expected_market:
            raise ValueError(
                "market_price.market must match analysis_decision.market."
            )

        expected_currency = _expected_currency_for_market(self.market_price.market)
        if self.market_price.currency != expected_currency:
            raise ValueError(
                f"market_price.currency must be {expected_currency.value} "
                f"for market {self.market_price.market.value}."
            )

        expected_metadata = canonicalize_payload(self.metadata)
        if self.metadata != expected_metadata:
            raise ValueError("metadata must be in canonical JSON-compatible form.")

        if self.allocator_decision.schema_name != ALLOCATOR_DECISION_SCHEMA:
            raise ValueError(
                f"allocator_decision.schema_name must be {ALLOCATOR_DECISION_SCHEMA!r}."
            )
        if self.analysis_decision.schema_name != ANALYSIS_DECISION_SCHEMA:
            raise ValueError(
                f"analysis_decision.schema_name must be {ANALYSIS_DECISION_SCHEMA!r}."
            )

        return self

    @property
    def normalized_run_id(self) -> DecisionId:
        """run_id를 DecisionId로 정규화한다."""
        if isinstance(self.run_id, DecisionId):
            return self.run_id
        return DecisionId(self.run_id)


class PaperLoopResult(BaseModel):
    """Paper E2E loop 실행 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    status: PaperLoopStatus
    validation_result: ValidationResult
    risk_result: ValidationResult
    order_generation_result: OrderGenerationResult
    quantity_resolution_result: QuantityResolutionResult | None = None
    generated_order_intent: OrderIntent | None = None
    executable_order_intent: OrderIntent | None = None
    broker_order_result: OrderResult | None = None
    fill: Fill | None = None
    nav_snapshot: NavSnapshot | None = None
    decision_snapshot_ids: tuple[DecisionId, ...] = ()
    correlation_id: str | None = None


def _expected_currency_for_market(market: Market) -> Currency:
    if market == Market.KR:
        return Currency.KRW
    if market == Market.US:
        return Currency.USD
    raise ValueError(f"unsupported market for currency mapping: {market.value}")


def passed_validation_result(*, schema_name: str, validator_version: str) -> ValidationResult:
    """사전 검증된 decision bundle용 passed ValidationResult."""
    return ValidationResult(
        passed=True,
        issues=(),
        schema_name=schema_name,
        validator_version=validator_version,
    )


def failed_validation_result(
    *,
    code: str,
    message: str,
    path: str | None = None,
) -> ValidationResult:
    """Paper loop 입력/저장 실패용 ValidationResult."""
    return ValidationResult(
        passed=False,
        issues=(
            ValidationIssue(
                code=code,
                message=message,
                severity=ValidationSeverity.ERROR,
                path=path,
            ),
        ),
        schema_name=PAPER_LOOP_SCHEMA,
        validator_version=PAPER_LOOP_VALIDATOR_VERSION,
    )


def build_paper_loop_snapshot(
    *,
    loop_input: PaperLoopInput,
    result: PaperLoopResult,
) -> DecisionSnapshot:
    """paper_loop.v1 DecisionSnapshot을 생성한다."""
    raw_payload: dict[str, Any] = {
        "run_id": loop_input.normalized_run_id.value,
        "status": result.status.value,
        "allocator_decision_id": loop_input.allocator_decision.decision_id.value,
        "analysis_decision_id": loop_input.analysis_decision.decision_id.value,
        "generated_order_intent_id": (
            result.generated_order_intent.order_id
            if result.generated_order_intent is not None
            else None
        ),
        "executable_order_intent_id": (
            result.executable_order_intent.order_id
            if result.executable_order_intent is not None
            else None
        ),
        "broker_status": (
            result.broker_order_result.status.value
            if result.broker_order_result is not None
            else None
        ),
    }
    if loop_input.scout_summary is not None:
        raw_payload["scout_summary_id"] = loop_input.scout_summary.summary_id.value

    order_intent_ids: list[str] = []
    if result.generated_order_intent is not None:
        order_intent_ids.append(result.generated_order_intent.order_id)
    if (
        result.executable_order_intent is not None
        and result.executable_order_intent.order_id not in order_intent_ids
    ):
        order_intent_ids.append(result.executable_order_intent.order_id)

    failed_statuses = {
        PaperLoopStatus.VALIDATION_FAILED,
        PaperLoopStatus.RISK_BLOCKED,
        PaperLoopStatus.QUANTITY_FAILED,
    }
    issues = result.validation_result.issues
    passed = result.status not in failed_statuses
    if not passed and not issues:
        issues = (
            ValidationIssue(
                code=PAPER_LOOP_INPUT_VALIDATION_FAILED,
                message=f"Paper loop ended with status {result.status.value}.",
                severity=ValidationSeverity.ERROR,
            ),
        )
    validation = ValidationResult(
        passed=passed,
        issues=issues,
        schema_name=PAPER_LOOP_SCHEMA,
        validator_version=PAPER_LOOP_VALIDATOR_VERSION,
    )

    return DecisionSnapshot.create(
        decision_id=loop_input.normalized_run_id,
        created_at=loop_input.created_at,
        schema_name=PAPER_LOOP_SCHEMA,
        raw_payload=raw_payload,
        validation_result=validation,
        order_intent_ids=tuple(order_intent_ids),
        replay_metadata={
            "correlation_id": result.correlation_id,
            "risk_passed": result.risk_result.passed,
            "order_generation_status": result.order_generation_result.status.value,
        },
    )
