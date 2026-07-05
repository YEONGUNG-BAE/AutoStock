"""Pure offline backtest engine building blocks.

The package contains a deterministic LLM-free rules-only allocator, a
single-step backtest contract, an as-of-safe snapshot builder, as-of-safe
rolling feature helpers, count-based observation spacing guards, and a
single-step rules decision composer. It does not wire ScoutInputBuilder,
implement a walk-forward loop, produce NAV, or compute benchmark-relative
performance.
"""

from __future__ import annotations

from backtest_engine.execution_prices import (
    EXECUTION_PRICE_POLICY_V1,
    BacktestExecutionPrice,
    BacktestExecutionPriceSlice,
    select_execution_prices_for_single_step_decision,
)
from backtest_engine.observation_spacing import (
    ObservationSpacingReport,
    validate_uniform_observation_spacing_for_count_based_ma,
)
from backtest_engine.rolling_features import (
    RollingLongMaAssetConfig,
    build_snapshot_configs_with_rolling_long_ma,
)
from backtest_engine.rules_allocator import RULES_ALLOCATOR_V1, allocate_rules_only_v1
from backtest_engine.snapshot_builder import (
    SnapshotAssetConfig,
    build_feature_snapshot_from_source_records,
)
from backtest_engine.single_step import (
    BacktestSingleStepDecision,
    build_single_step_rules_decision,
    make_rules_only_single_step_decision,
)
from backtest_engine.step_contract import (
    DECIMAL_WEIGHT_TOLERANCE,
    BacktestAssetFeature,
    BacktestFeatureSnapshot,
    BacktestTargetWeight,
    BacktestTargetWeights,
)

__all__ = [
    "DECIMAL_WEIGHT_TOLERANCE",
    "EXECUTION_PRICE_POLICY_V1",
    "RULES_ALLOCATOR_V1",
    "BacktestAssetFeature",
    "BacktestExecutionPrice",
    "BacktestExecutionPriceSlice",
    "BacktestFeatureSnapshot",
    "BacktestSingleStepDecision",
    "BacktestTargetWeight",
    "BacktestTargetWeights",
    "ObservationSpacingReport",
    "RollingLongMaAssetConfig",
    "SnapshotAssetConfig",
    "allocate_rules_only_v1",
    "build_feature_snapshot_from_source_records",
    "build_single_step_rules_decision",
    "build_snapshot_configs_with_rolling_long_ma",
    "make_rules_only_single_step_decision",
    "select_execution_prices_for_single_step_decision",
    "validate_uniform_observation_spacing_for_count_based_ma",
]
