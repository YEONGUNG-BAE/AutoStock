"""Pure offline backtest engine building blocks.

The package contains a deterministic LLM-free rules-only allocator, a
single-step backtest contract, an as-of-safe snapshot builder, as-of-safe
rolling feature helpers, count-based observation spacing guards, and a
single-step rules decision composer. It does not wire ScoutInputBuilder,
implement a walk-forward loop, produce NAV, or compute benchmark-relative
performance.
"""

from __future__ import annotations

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
    "RULES_ALLOCATOR_V1",
    "BacktestAssetFeature",
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
    "validate_uniform_observation_spacing_for_count_based_ma",
]
