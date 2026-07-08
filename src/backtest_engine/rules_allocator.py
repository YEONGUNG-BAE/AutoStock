"""Pure rules-only allocator for Phase 2c-0."""

from __future__ import annotations

from typing import Literal
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_engine.step_contract import (
    DECIMAL_WEIGHT_TOLERANCE,
    BacktestFeatureSnapshot,
    BacktestTargetWeight,
    BacktestTargetWeights,
)

RULES_ALLOCATOR_V1 = "rules_allocator.v1"
RULES_ALLOCATOR_V2_POLICY = (
    "local_monthly_rules_allocator_v2_contract.sp_core_relative_recovery.v1"
)
ONE = Decimal("1")
ZERO = Decimal("0")
RULES_ALLOCATOR_V2_NORMAL_TARGET_WEIGHTS = (
    ("asset_us", Decimal("0.70")),
    ("asset_kr", Decimal("0.15")),
    ("asset_gold", Decimal("0.10")),
    ("cash", Decimal("0.05")),
)
RULES_ALLOCATOR_V2_DEFENSIVE_TARGET_WEIGHTS = (
    ("asset_us", Decimal("0.50")),
    ("asset_kr", Decimal("0.10")),
    ("asset_gold", Decimal("0.25")),
    ("cash", Decimal("0.15")),
)
RULES_ALLOCATOR_V2_MIN_US_WEIGHT_NORMAL = Decimal("0.65")
RULES_ALLOCATOR_V2_MAX_CASH_GOLD_WEIGHT_NORMAL = Decimal("0.20")
RULES_ALLOCATOR_V2_MAX_CASH_GOLD_WEIGHT_DEFENSIVE = Decimal("0.40")


class RulesAllocatorV2StateInput(BaseModel):
    """Pure state flags for the contract-only V2 target-weight function."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trend_risk_off: bool = False
    relative_drawdown_guard_active: bool = False
    relative_recovery_active: bool = False
    extended_defense_guard_active: bool = False


class RulesAllocatorV2TargetWeights(BaseModel):
    """Frozen target weights emitted by the pure V2 allocator surface."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allocator_version: str
    weights: tuple[BacktestTargetWeight, ...]

    @field_validator("allocator_version", mode="before")
    @classmethod
    def validate_allocator_version(cls, value: str) -> str:
        if value != RULES_ALLOCATOR_V2_POLICY:
            raise ValueError(f"allocator_version must be {RULES_ALLOCATOR_V2_POLICY}.")
        return value

    @model_validator(mode="after")
    def validate_target_weights(self) -> "RulesAllocatorV2TargetWeights":
        asset_ids = tuple(weight.asset_id for weight in self.weights)
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("asset_id values must be unique.")

        total = sum((weight.weight for weight in self.weights), ZERO)
        if abs(total - ONE) > DECIMAL_WEIGHT_TOLERANCE:
            raise ValueError(
                "total weight must equal 1 within Decimal tolerance "
                f"{DECIMAL_WEIGHT_TOLERANCE}."
            )
        return self


def _clamp(value: Decimal, *, minimum: Decimal, maximum: Decimal) -> Decimal:
    return min(max(value, minimum), maximum)


def resolve_rules_allocator_v2_state(
    state: RulesAllocatorV2StateInput,
) -> Literal["normal", "defensive"]:
    """Resolve V2 state flags without data access, time access, or mutation."""

    if state.trend_risk_off:
        return "defensive"
    if state.extended_defense_guard_active:
        return "normal"
    if state.relative_recovery_active:
        return "normal"
    if state.relative_drawdown_guard_active:
        return "defensive"
    return "normal"


def allocate_rules_v2_target_weights(
    *,
    state: RulesAllocatorV2StateInput | None = None,
    cash_asset_id: str = "cash",
) -> RulesAllocatorV2TargetWeights:
    """Return pure V2 target weights without integration or data access."""

    resolved_state = resolve_rules_allocator_v2_state(
        state or RulesAllocatorV2StateInput()
    )
    target_weights = (
        RULES_ALLOCATOR_V2_DEFENSIVE_TARGET_WEIGHTS
        if resolved_state == "defensive"
        else RULES_ALLOCATOR_V2_NORMAL_TARGET_WEIGHTS
    )
    return RulesAllocatorV2TargetWeights(
        allocator_version=RULES_ALLOCATOR_V2_POLICY,
        weights=tuple(
            BacktestTargetWeight(
                asset_id=cash_asset_id if asset_id == "cash" else asset_id,
                weight=weight,
            )
            for asset_id, weight in target_weights
        ),
    )


def allocate_rules_only_v1(snapshot: BacktestFeatureSnapshot) -> BacktestTargetWeights:
    """Return target weights for an already-built BacktestFeatureSnapshot."""

    non_cash_weights: list[BacktestTargetWeight] = []
    for asset in snapshot.assets:
        preliminary_weight = (
            asset.risk_on_weight if asset.current_price >= asset.long_ma else asset.risk_off_weight
        )
        bounded_weight = _clamp(
            preliminary_weight,
            minimum=asset.min_weight,
            maximum=asset.max_weight,
        )
        non_cash_weights.append(
            BacktestTargetWeight(asset_id=asset.asset_id, weight=bounded_weight)
        )

    non_cash_total = sum((target.weight for target in non_cash_weights), ZERO)
    cash_weight = ONE - non_cash_total
    if cash_weight < snapshot.cash_min_weight:
        raise ValueError("cash_weight must be >= cash_min_weight.")

    return BacktestTargetWeights(
        decision_time=snapshot.decision_time,
        allocator_version=RULES_ALLOCATOR_V1,
        weights=(
            *non_cash_weights,
            BacktestTargetWeight(asset_id=snapshot.cash_asset_id, weight=cash_weight),
        ),
    )
