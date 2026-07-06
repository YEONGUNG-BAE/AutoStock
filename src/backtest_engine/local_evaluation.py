"""Sibling local monthly real-data evaluation dry-run for Phase 2d-3.

This module orchestrates local monthly CSV dataset assembly, KOSPI-primary run
config building, walk-forward NAV, benchmark-relative metrics, and markdown report
bundle rendering entirely in memory. It does not fetch or download data, write
report files, create artifacts, or produce investment conclusions.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from backtest_engine.benchmark_adapter import (
    BacktestBenchmarkRelativeResult,
    compute_walk_forward_benchmark_relative_metrics,
)
from backtest_engine.local_dataset import (
    LocalMonthlyBenchmarkSpec,
    LocalMonthlyDatasetAssemblyResult,
    LocalMonthlyInstrumentSpec,
    assemble_local_monthly_dataset,
    default_local_monthly_benchmark_spec,
    default_local_monthly_instrument_specs_for_kospi_primary,
)
from backtest_engine.local_run_config import (
    LocalMonthlyRunConfig,
    build_kospi_primary_monthly_run_config,
)
from backtest_engine.report_bundle import (
    BacktestEvaluationReportBundle,
    render_backtest_evaluation_report_bundle,
)
from backtest_engine.walk_forward import (
    BacktestWalkForwardResult,
    run_explicit_schedule_rules_walk_forward_nav,
)
from paper_review.models import BenchmarkReturnPoint

LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1 = (
    "sibling_local_monthly_kospi_primary_evaluation_dry_run.v1"
)
LOCAL_BENCHMARK_CALENDAR_ALIGNMENT_POLICY_V1 = (
    "local_monthly_benchmark_points_aligned_to_strategy_nav_calendar.v1"
)

_RESEARCH_ONLY_WARNING = (
    "real-data dry-run result is research evidence only; "
    "it is not an investment conclusion"
)
_KOSPI_PROXY_WARNING = (
    "KOSPI primary is a KR proxy, not implementable ETF evidence"
)
_BENCHMARK_CALENDAR_ALIGNMENT_WARNING = (
    "local benchmark points are calendar-aligned to strategy NAV timestamps "
    "before metric adaptation"
)


class LocalMonthlyEvaluationDryRunResult(BaseModel):
    """Immutable in-memory local monthly real-data evaluation dry-run result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_monthly_evaluation_dry_run_policy: Literal[
        "sibling_local_monthly_kospi_primary_evaluation_dry_run.v1"
    ]
    dataset: LocalMonthlyDatasetAssemblyResult
    run_config: LocalMonthlyRunConfig
    walk_forward_result: BacktestWalkForwardResult
    benchmark_relative_result: BacktestBenchmarkRelativeResult
    report_bundle: BacktestEvaluationReportBundle
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.run_config.dataset != self.dataset:
            raise ValueError("run_config.dataset must equal dataset.")
        if (
            self.benchmark_relative_result.walk_forward_result
            != self.walk_forward_result
        ):
            raise ValueError(
                "benchmark_relative_result.walk_forward_result must equal "
                "walk_forward_result."
            )
        if (
            self.report_bundle.benchmark_relative_result
            != self.benchmark_relative_result
        ):
            raise ValueError(
                "report_bundle.benchmark_relative_result must equal "
                "benchmark_relative_result."
            )
        if len(self.walk_forward_result.steps) != len(self.run_config.period_specs):
            raise ValueError(
                "walk_forward_result.steps length must equal "
                "run_config.period_specs length."
            )
        if len(self.walk_forward_result.nav_points) != len(
            self.run_config.period_specs
        ):
            raise ValueError(
                "walk_forward_result.nav_points length must equal "
                "run_config.period_specs length."
            )
        if (
            self.walk_forward_result.initial_portfolio_state
            != self.run_config.initial_portfolio_state
        ):
            raise ValueError(
                "walk_forward_result.initial_portfolio_state must equal "
                "run_config.initial_portfolio_state."
            )
        return self


def align_local_monthly_benchmark_points_to_nav_calendar(
    *,
    run_config: LocalMonthlyRunConfig,
    walk_forward_result: BacktestWalkForwardResult,
    benchmark_points: Iterable[BenchmarkReturnPoint],
) -> tuple[BenchmarkReturnPoint, ...]:
    """Align local monthly benchmark points onto strategy NAV calendar dates."""
    materialized_benchmark_points = tuple(benchmark_points)
    nav_points = walk_forward_result.nav_points
    common_periods = run_config.dataset.common_periods
    rolling_lookback_count = run_config.rolling_lookback_count

    if len(nav_points) != len(run_config.period_specs):
        raise ValueError(
            "walk_forward_result.nav_points length must equal "
            "run_config.period_specs length."
        )
    if len(nav_points) < 2:
        raise ValueError("at least 2 walk-forward NAV points are required.")
    if not materialized_benchmark_points:
        raise ValueError("benchmark_points must not be empty.")

    fx_points = run_config.dataset.fx_points
    if len(materialized_benchmark_points) != len(fx_points):
        raise ValueError(
            "benchmark_points length must equal dataset.fx_points length."
        )

    seen_periods: set[str] = set()
    period_to_benchmark: dict[str, BenchmarkReturnPoint] = {}
    for fx_point, benchmark_point in zip(fx_points, materialized_benchmark_points, strict=True):
        if fx_point.period_key in seen_periods:
            raise ValueError(f"duplicate benchmark period key: {fx_point.period_key}")
        seen_periods.add(fx_point.period_key)
        period_to_benchmark[fx_point.period_key] = benchmark_point

    if len(period_to_benchmark) != len(fx_points):
        raise ValueError("duplicate benchmark period keys detected.")

    aligned_points: list[BenchmarkReturnPoint] = []
    for index, nav_point in enumerate(nav_points):
        execution_period = common_periods[rolling_lookback_count + index]
        benchmark_point = period_to_benchmark.get(execution_period)
        if benchmark_point is None:
            raise ValueError(
                f"missing benchmark point for execution period: {execution_period}"
            )
        aligned_points.append(
            BenchmarkReturnPoint(
                as_of=nav_point.as_of,
                total_return_index_value=benchmark_point.total_return_index_value,
            )
        )

    if len(aligned_points) != len(nav_points):
        raise ValueError(
            "aligned benchmark point count must equal walk-forward NAV point count."
        )

    aligned_dates = tuple(point.as_of.date() for point in aligned_points)
    nav_dates = tuple(point.as_of.date() for point in nav_points)
    if aligned_dates != nav_dates:
        raise ValueError(
            "aligned benchmark as_of.date() sequence must equal NAV as_of.date() sequence."
        )

    for previous, current in zip(aligned_dates, aligned_dates[1:], strict=False):
        if previous >= current:
            raise ValueError(
                "aligned benchmark calendar dates must be strictly increasing."
            )

    return tuple(aligned_points)


def run_local_monthly_evaluation_dry_run(
    *,
    repo_root: Path,
    data_root: Path | None = None,
    instrument_specs: Iterable[LocalMonthlyInstrumentSpec] | None = None,
    benchmark_spec: LocalMonthlyBenchmarkSpec | None = None,
    initial_cash_krw: Decimal = Decimal("100000000"),
    cash_asset_id: str = "cash",
    cash_min_weight: Decimal = Decimal("0.05"),
    rolling_lookback_count: int = 3,
    fee_bps: Decimal = Decimal("10"),
    kr_sell_tax_bps: Decimal = Decimal("23"),
    fx_spread_bps: Decimal = Decimal("15"),
) -> LocalMonthlyEvaluationDryRunResult:
    """Run an in-memory local monthly real-data evaluation dry-run."""
    resolved_repo_root = repo_root.resolve()

    materialized_instrument_specs = (
        default_local_monthly_instrument_specs_for_kospi_primary()
        if instrument_specs is None
        else tuple(instrument_specs)
    )
    materialized_benchmark_spec = (
        default_local_monthly_benchmark_spec()
        if benchmark_spec is None
        else benchmark_spec
    )

    dataset = assemble_local_monthly_dataset(
        repo_root=resolved_repo_root,
        data_root=data_root,
        instrument_specs=materialized_instrument_specs,
        benchmark_spec=materialized_benchmark_spec,
    )

    run_config = build_kospi_primary_monthly_run_config(
        dataset=dataset,
        initial_cash_krw=initial_cash_krw,
        cash_asset_id=cash_asset_id,
        cash_min_weight=cash_min_weight,
        rolling_lookback_count=rolling_lookback_count,
        fee_bps=fee_bps,
        kr_sell_tax_bps=kr_sell_tax_bps,
        fx_spread_bps=fx_spread_bps,
    )

    walk_forward_result = run_explicit_schedule_rules_walk_forward_nav(
        dataset.source_records,
        period_specs=run_config.period_specs,
        rolling_asset_configs=run_config.rolling_asset_configs,
        initial_portfolio_state=run_config.initial_portfolio_state,
        cost_model=run_config.cost_model,
        cash_asset_id=run_config.cash_asset_id,
        cash_min_weight=run_config.cash_min_weight,
    )

    aligned_benchmark_points = align_local_monthly_benchmark_points_to_nav_calendar(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_points=dataset.benchmark_points,
    )

    benchmark_relative_result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward_result,
        benchmark_points=aligned_benchmark_points,
    )

    report_bundle = render_backtest_evaluation_report_bundle(
        benchmark_relative_result=benchmark_relative_result,
    )

    warnings = _collect_warnings(
        dataset_warnings=dataset.warnings,
        run_config_warnings=run_config.warnings,
    )

    return LocalMonthlyEvaluationDryRunResult(
        local_monthly_evaluation_dry_run_policy=(
            LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1
        ),
        dataset=dataset,
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_relative_result=benchmark_relative_result,
        report_bundle=report_bundle,
        warnings=warnings,
    )


def _collect_warnings(
    *,
    dataset_warnings: tuple[str, ...],
    run_config_warnings: tuple[str, ...],
) -> tuple[str, ...]:
    combined = list(dataset_warnings)
    combined.extend(run_config_warnings)
    combined.append(_RESEARCH_ONLY_WARNING)
    combined.append(_KOSPI_PROXY_WARNING)
    combined.append(_BENCHMARK_CALENDAR_ALIGNMENT_WARNING)
    return tuple(combined)
