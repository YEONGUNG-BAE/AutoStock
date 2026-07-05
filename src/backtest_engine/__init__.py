"""Phase 2c-0 provides a pure, deterministic, LLM-free rules-only allocator and a single-step backtest contract. It does not build snapshots from source records, does not wire loaders or ScoutInputBuilder, does not implement a walk-forward loop, does not produce NAV, and does not compute benchmark-relative performance."""

from __future__ import annotations

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
    "SnapshotAssetConfig",
    "allocate_rules_only_v1",
    "build_feature_snapshot_from_source_records",
]
