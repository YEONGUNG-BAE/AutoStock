"""Pure offline backtest engine building blocks.

The package contains a deterministic LLM-free rules-only allocator, a
single-step backtest contract, an as-of-safe snapshot builder, as-of-safe
rolling feature helpers, count-based observation spacing guards, a single-step
rules decision composer, explicit-schedule walk-forward NAV, and a
benchmark-relative metrics adapter, a benchmark-relative markdown report
bundle renderer, and a synthetic end-to-end evaluation pipeline composer. It
does not load real data, fetch data, run live or paper trading, or produce
project-level investment conclusions.
"""

from __future__ import annotations

from backtest_engine.benchmark_adapter import (
    BENCHMARK_ADAPTER_POLICY_V1,
    BacktestBenchmarkRelativeResult,
    compute_walk_forward_benchmark_relative_metrics,
)
from backtest_engine.evaluation_pipeline import (
    BACKTEST_EVALUATION_PIPELINE_POLICY_V1,
    BacktestEvaluationPipelineResult,
    run_explicit_synthetic_backtest_evaluation_pipeline,
)
from backtest_engine.local_data_preflight import (
    LOCAL_DATA_PREFLIGHT_POLICY_V1,
    LocalDataFilePreflightResult,
    LocalDataFileSpec,
    LocalDataPreflightResult,
    default_monthly_local_data_file_specs,
    run_local_data_preflight,
)
from backtest_engine.report_bundle import (
    BACKTEST_REPORT_BUNDLE_POLICY_V1,
    BacktestEvaluationReportBundle,
    render_backtest_evaluation_report_bundle,
)
from backtest_engine.execution_prices import (
    EXECUTION_PRICE_POLICY_V1,
    BacktestExecutionPrice,
    BacktestExecutionPriceSlice,
    select_execution_prices_for_single_step_decision,
)
from backtest_engine.rebalance import (
    COST_MODEL_V1,
    REBALANCE_ACCOUNTING_POLICY_V1,
    BacktestCostModel,
    BacktestHolding,
    BacktestPortfolioState,
    BacktestRebalanceResult,
    BacktestTrade,
    apply_single_rebalance_accounting,
)
from backtest_engine.period_step import (
    PERIOD_STEP_POLICY_V1,
    BacktestSinglePeriodStepResult,
    run_single_period_rules_rebalance_step,
)
from backtest_engine.walk_forward import (
    WALK_FORWARD_POLICY_V1,
    BacktestNavPoint,
    BacktestPeriodSpec,
    BacktestWalkForwardResult,
    run_explicit_schedule_rules_walk_forward_nav,
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
    "BENCHMARK_ADAPTER_POLICY_V1",
    "DECIMAL_WEIGHT_TOLERANCE",
    "COST_MODEL_V1",
    "EXECUTION_PRICE_POLICY_V1",
    "PERIOD_STEP_POLICY_V1",
    "REBALANCE_ACCOUNTING_POLICY_V1",
    "RULES_ALLOCATOR_V1",
    "WALK_FORWARD_POLICY_V1",
    "BACKTEST_EVALUATION_PIPELINE_POLICY_V1",
    "BACKTEST_REPORT_BUNDLE_POLICY_V1",
    "LOCAL_DATA_PREFLIGHT_POLICY_V1",
    "BacktestBenchmarkRelativeResult",
    "BacktestEvaluationPipelineResult",
    "BacktestEvaluationReportBundle",
    "LocalDataFilePreflightResult",
    "LocalDataFileSpec",
    "LocalDataPreflightResult",
    "BacktestAssetFeature",
    "BacktestCostModel",
    "BacktestExecutionPrice",
    "BacktestExecutionPriceSlice",
    "BacktestFeatureSnapshot",
    "BacktestHolding",
    "BacktestNavPoint",
    "BacktestPeriodSpec",
    "BacktestPortfolioState",
    "BacktestRebalanceResult",
    "BacktestSinglePeriodStepResult",
    "BacktestSingleStepDecision",
    "BacktestTargetWeight",
    "BacktestTargetWeights",
    "BacktestTrade",
    "BacktestWalkForwardResult",
    "ObservationSpacingReport",
    "RollingLongMaAssetConfig",
    "SnapshotAssetConfig",
    "compute_walk_forward_benchmark_relative_metrics",
    "default_monthly_local_data_file_specs",
    "render_backtest_evaluation_report_bundle",
    "run_explicit_synthetic_backtest_evaluation_pipeline",
    "run_local_data_preflight",
    "allocate_rules_only_v1",
    "apply_single_rebalance_accounting",
    "build_feature_snapshot_from_source_records",
    "build_single_step_rules_decision",
    "build_snapshot_configs_with_rolling_long_ma",
    "make_rules_only_single_step_decision",
    "run_explicit_schedule_rules_walk_forward_nav",
    "run_single_period_rules_rebalance_step",
    "select_execution_prices_for_single_step_decision",
    "validate_uniform_observation_spacing_for_count_based_ma",
]
