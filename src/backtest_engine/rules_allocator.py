"""Pure rules-only allocator for Phase 2c-0."""

from __future__ import annotations

from decimal import Decimal

from backtest_engine.step_contract import (
    BacktestFeatureSnapshot,
    BacktestTargetWeight,
    BacktestTargetWeights,
)

RULES_ALLOCATOR_V1 = "rules_allocator.v1"
ONE = Decimal("1")
ZERO = Decimal("0")


def _clamp(value: Decimal, *, minimum: Decimal, maximum: Decimal) -> Decimal:
    return min(max(value, minimum), maximum)


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
