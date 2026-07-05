"""Pure offline backtest engine building blocks.

The package contains a deterministic LLM-free rules-only allocator, a
single-step backtest contract, an as-of-safe snapshot builder, and as-of-safe
rolling feature helpers. It does not wire ScoutInputBuilder, implement a
walk-forward loop, produce NAV, or compute benchmark-relative performance.
"""

from __future__ import annotations

from backtest_engine.rolling_features import (
    RollingLongMaAssetConfig,
    build_snapshot_configs_with_rolling_long_ma,
)
from backtest_engine.rules_allocator import RULES_ALLOCATOR_V1, allocate_rules_only_v1
from backtest_engine.snapshot_builder import (
    SnapshotAssetConfig,
    build_feature_snapshot_from_source_records,
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
    "BacktestTargetWeight",
    "BacktestTargetWeights",
    "RollingLongMaAssetConfig",
    "SnapshotAssetConfig",
    "allocate_rules_only_v1",
    "build_feature_snapshot_from_source_records",
    "build_snapshot_configs_with_rolling_long_ma",
]
