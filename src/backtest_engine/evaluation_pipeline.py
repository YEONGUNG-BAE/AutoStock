"""Explicit synthetic backtest evaluation pipeline composer for Phase 2c-10.

이 모듈은 이미 동결된 walk-forward NAV, benchmark-relative adapter, report bundle
renderer를 하나의 합성 end-to-end evaluation pipeline으로 조합한다. 실데이터를
로드하거나 스케줄을 생성하거나 benchmark를 조회하지 않으며, 보고서 파일을 쓰거나
프로젝트 수준의 투자 결론을 추가하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from backtest_data import InMemoryDateIdSourceReader
from backtest_engine.benchmark_adapter import (
    BacktestBenchmarkRelativeResult,
    compute_walk_forward_benchmark_relative_metrics,
)
from backtest_engine.rebalance import BacktestCostModel, BacktestPortfolioState
from backtest_engine.report_bundle import (
    BacktestEvaluationReportBundle,
    render_backtest_evaluation_report_bundle,
)
from backtest_engine.rolling_features import RollingLongMaAssetConfig
from backtest_engine.walk_forward import (
    BacktestPeriodSpec,
    BacktestWalkForwardResult,
    run_explicit_schedule_rules_walk_forward_nav,
)
from domain import DateIdSourceRecord
from paper_review.models import BenchmarkReturnPoint

BACKTEST_EVALUATION_PIPELINE_POLICY_V1 = (
    "explicit_synthetic_walk_forward_benchmark_report_pipeline.v1"
)


class BacktestEvaluationPipelineResult(BaseModel):
    """explicit synthetic 입력으로 조합된 불변 backtest evaluation pipeline 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_pipeline_policy: Literal[
        "explicit_synthetic_walk_forward_benchmark_report_pipeline.v1"
    ]
    walk_forward_result: BacktestWalkForwardResult
    benchmark_relative_result: BacktestBenchmarkRelativeResult
    report_bundle: BacktestEvaluationReportBundle

    @model_validator(mode="after")
    def validate_nested_linkage(self) -> Self:
        if self.benchmark_relative_result.walk_forward_result != self.walk_forward_result:
            raise ValueError(
                "benchmark_relative_result.walk_forward_result must equal walk_forward_result."
            )
        if self.report_bundle.benchmark_relative_result != self.benchmark_relative_result:
            raise ValueError(
                "report_bundle.benchmark_relative_result must equal benchmark_relative_result."
            )
        return self


def run_explicit_synthetic_backtest_evaluation_pipeline(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    period_specs: Iterable[BacktestPeriodSpec],
    rolling_asset_configs: Iterable[RollingLongMaAssetConfig],
    initial_portfolio_state: BacktestPortfolioState,
    cost_model: BacktestCostModel,
    cash_asset_id: str,
    cash_min_weight: Decimal,
    benchmark_points: Iterable[BenchmarkReturnPoint],
) -> BacktestEvaluationPipelineResult:
    """explicit synthetic 입력만 받아 walk-forward → benchmark → report bundle을 조합한다."""
    step_source: InMemoryDateIdSourceReader | tuple[DateIdSourceRecord, ...]
    if isinstance(source, InMemoryDateIdSourceReader):
        step_source = source
    else:
        step_source = tuple(source)

    materialized_period_specs = tuple(period_specs)
    materialized_configs = tuple(rolling_asset_configs)
    materialized_benchmark_points = tuple(benchmark_points)

    walk_forward_result = run_explicit_schedule_rules_walk_forward_nav(
        step_source,
        period_specs=materialized_period_specs,
        rolling_asset_configs=materialized_configs,
        initial_portfolio_state=initial_portfolio_state,
        cost_model=cost_model,
        cash_asset_id=cash_asset_id,
        cash_min_weight=cash_min_weight,
    )
    benchmark_relative_result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward_result,
        benchmark_points=materialized_benchmark_points,
    )
    report_bundle = render_backtest_evaluation_report_bundle(
        benchmark_relative_result=benchmark_relative_result,
    )
    return BacktestEvaluationPipelineResult(
        evaluation_pipeline_policy=BACKTEST_EVALUATION_PIPELINE_POLICY_V1,
        walk_forward_result=walk_forward_result,
        benchmark_relative_result=benchmark_relative_result,
        report_bundle=report_bundle,
    )
