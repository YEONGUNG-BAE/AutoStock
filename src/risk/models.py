from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from allocator.models import ALLOCATOR_DECISION_SCHEMA, AllocatorDecision, AssetBucket
from analysis.models import ANALYSIS_DECISION_SCHEMA, AnalysisDecision
from decision.canonical_json import canonicalize_payload
from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.enums import Currency, Market
from domain.identifiers import Percent
from domain.market import MarketPrice
from domain.money import Money


class RiskMode(StrEnum):
    """Python-owned portfolio/risk 실행 상태. ExecutionMode와 별개다."""

    NORMAL = "normal"
    REBALANCING = "rebalancing"
    EMERGENCY_TRIGGER = "emergency_trigger"
    MDD_KILLSWITCH = "mdd_killswitch"


class RiskDecision(StrEnum):
    """RiskFilter 단일 규칙 판정 결과."""

    ALLOW = "allow"
    BLOCK = "block"
    ADJUST = "adjust"


class OrderGenerationStatus(StrEnum):
    """OrderIntentGenerator 실행 결과 상태."""

    GENERATED = "generated"
    BLOCKED = "blocked"
    NOOP = "noop"


class AssetClassWeights(BaseModel):
    """운용 자산 기준 KR/US/GOLD 현재 비중."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kr: Percent
    us: Percent
    gold: Percent


class RiskFilterContext(BaseModel):
    """RiskFilter가 LLM output 외에 필요한 Python-owned portfolio/account state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    created_at: datetime
    mode: RiskMode
    total_nav: Money
    cash: Money
    invested_amount: Money
    current_symbol_market_value: Money | None = None
    current_symbol_cumulative_buy_cost: Money | None = None
    current_symbol_weight_percent: Percent | None = None
    current_asset_weights: AssetClassWeights | None = None
    allocator_tolerance_percent: Percent = Field(default_factory=lambda: Percent("5"))
    allocator_symbol_target_weight: Percent | None = None
    paper_observation_min_invested_percent: Percent | None = None
    mdd_percent: Percent | None = None
    market: Market | None = None
    currency: Currency | None = None
    asset_bucket: AssetBucket | None = None
    gold_trades_this_month: int = 0
    gold_trades_this_quarter: int = 0
    proposed_price: Money | None = None
    reference_prices: Mapping[str, MarketPrice] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="created_at")

    @field_validator("gold_trades_this_month", "gold_trades_this_quarter", mode="before")
    @classmethod
    def validate_gold_trade_counts(cls, value: Any, info) -> int:
        if not isinstance(value, int):
            raise ValueError(f"{info.field_name} must be an int.")
        if value < 0:
            raise ValueError(f"{info.field_name} must be greater than or equal to 0.")
        return value

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
    def validate_context(self) -> Self:
        if self.total_nav.amount <= 0:
            raise ValueError("total_nav.amount must be greater than 0.")

        nav_currency = self.total_nav.currency
        for field_name, money in (
            ("cash", self.cash),
            ("invested_amount", self.invested_amount),
        ):
            if money.amount < 0:
                raise ValueError(f"{field_name}.amount must be greater than or equal to 0.")
            if money.currency != nav_currency:
                raise ValueError(f"{field_name}.currency must match total_nav.currency.")

        for field_name, money in (
            ("current_symbol_market_value", self.current_symbol_market_value),
            ("current_symbol_cumulative_buy_cost", self.current_symbol_cumulative_buy_cost),
            ("proposed_price", self.proposed_price),
        ):
            if money is not None and money.currency != nav_currency:
                raise ValueError(f"{field_name}.currency must match total_nav.currency.")

        if self.paper_observation_min_invested_percent is not None:
            lower = self.paper_observation_min_invested_percent.value
            if lower < 50 or lower > 70:
                raise ValueError(
                    "paper_observation_min_invested_percent must be between 50 and 70."
                )

        expected = canonicalize_payload(self.metadata)
        if self.metadata != expected:
            raise ValueError("metadata must be in canonical JSON-compatible form.")

        return self


class RiskFilterInput(BaseModel):
    """validated AllocatorDecision + AnalysisDecision + Context를 하나로 묶는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allocator_decision: AllocatorDecision
    analysis_decision: AnalysisDecision
    context: RiskFilterContext
    correlation_id: str | None = None

    @field_validator("correlation_id", mode="before")
    @classmethod
    def validate_correlation_id(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name="correlation_id")

    @model_validator(mode="after")
    def validate_schema_names(self) -> Self:
        if self.allocator_decision.schema_name != ALLOCATOR_DECISION_SCHEMA:
            raise ValueError(
                f"allocator_decision.schema_name must be {ALLOCATOR_DECISION_SCHEMA!r}."
            )
        if self.analysis_decision.schema_name != ANALYSIS_DECISION_SCHEMA:
            raise ValueError(
                f"analysis_decision.schema_name must be {ANALYSIS_DECISION_SCHEMA!r}."
            )
        return self
