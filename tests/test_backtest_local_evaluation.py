from __future__ import annotations

import ast
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine.local_dataset import (  # noqa: E402
    LocalMonthlyDatasetAssemblyResult,
    assemble_local_monthly_dataset,
    default_local_monthly_benchmark_spec,
    default_local_monthly_instrument_specs_for_kospi_primary,
)
from backtest_engine.local_evaluation import (  # noqa: E402
    LOCAL_BENCHMARK_CALENDAR_ALIGNMENT_POLICY_V1,
    LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1,
    LOCAL_NAV_SANITY_DIAGNOSTIC_POLICY_V1,
    LOCAL_NAV_SANITY_POLICY_V1,
    LocalMonthlyEvaluationDryRunResult,
    LocalNavSanityStepDiagnostic,
    align_local_monthly_benchmark_points_to_nav_calendar,
    build_local_nav_sanity_step_diagnostic,
    run_local_monthly_evaluation_dry_run,
    validate_local_monthly_walk_forward_nav_sanity,
    _holding_value_krw_for_sanity,
)
from backtest_engine.local_run_config import (  # noqa: E402
    LocalMonthlyRunConfig,
    build_kospi_primary_monthly_run_config,
)
from backtest_engine.rebalance import (  # noqa: E402
    BacktestHolding,
    BacktestTrade,
    _canonical_total_cost_krw,
)
from backtest_engine.walk_forward import (  # noqa: E402
    BacktestNavPoint,
    BacktestWalkForwardResult,
)
from paper_review.models import BenchmarkReturnPoint  # noqa: E402

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "backtest_engine"
    / "local_evaluation.py"
)

REQUIRED_COLUMNS = (
    "date",
    "as_of",
    "symbol",
    "market",
    "close_adjusted",
    "source_name",
)
HEADER = ",".join(REQUIRED_COLUMNS)

FOCUSED_TEST_FILES = (
    "tests/test_backtest_local_evaluation.py",
    "tests/test_backtest_local_run_config.py",
    "tests/test_backtest_local_dataset.py",
    "tests/test_backtest_local_data_preflight.py",
    "tests/test_backtest_evaluation_pipeline.py",
    "tests/test_backtest_report_bundle.py",
    "tests/test_backtest_benchmark_adapter.py",
    "tests/test_backtest_walk_forward.py",
    "tests/test_backtest_period_step.py",
    "tests/test_backtest_rebalance.py",
    "tests/test_backtest_execution_prices.py",
    "tests/test_backtest_single_step_decision.py",
    "tests/test_backtest_observation_spacing.py",
    "tests/test_backtest_rolling_features.py",
    "tests/test_backtest_snapshot_builder.py",
    "tests/test_backtest_step_contract.py",
    "tests/test_rules_allocator.py",
    "tests/test_paper_review_benchmark_relative_metrics.py",
    "tests/test_paper_review_benchmark_relative_report.py",
    "tests/test_backtest_data_loader.py",
    "tests/test_backtest_asof_guard.py",
    "tests/test_backtest_source_record_conversion.py",
    "tests/test_scout_input_builder.py",
    "tests/test_backtest_design_freeze_docs.py",
)


def _write_csv(path: Path, rows: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "AutoStock"
    data_root = tmp_path / "autostock-data"
    repo_root.mkdir()
    data_root.mkdir()
    return repo_root, data_root


def _write_default_kospi_primary_csvs(
    data_root: Path,
    *,
    periods: tuple[str, ...] = (
        "2020-01",
        "2020-02",
        "2020-03",
        "2020-04",
        "2020-05",
    ),
    close_by_symbol: dict[str, tuple[Decimal, ...]] | None = None,
) -> None:
    specs = default_local_monthly_instrument_specs_for_kospi_primary()
    benchmark = default_local_monthly_benchmark_spec()

    for spec in specs:
        rows: list[str] = []
        for index, period in enumerate(periods):
            year, month = period.split("-")
            day = "31" if month in {"01", "03", "05", "07", "08", "10", "12"} else "29"
            if close_by_symbol is not None and spec.symbol in close_by_symbol:
                close = close_by_symbol[spec.symbol][index]
            else:
                close = Decimal("100") + Decimal(index)
            as_of_month = int(month) + 1
            as_of_year = int(year)
            if as_of_month > 12:
                as_of_month = 1
                as_of_year += 1
            rows.append(
                f"{year}-{month}-{day},{as_of_year:04d}-{as_of_month:02d}-01T00:00:00+00:00,"
                f"{spec.symbol},{spec.market},{close},synthetic"
            )
        _write_csv(data_root / spec.relative_path, tuple(rows))

    sp_rows: list[str] = []
    fx_rows: list[str] = []
    for index, period in enumerate(periods):
        year, month = period.split("-")
        day = "31" if month in {"01", "03", "05", "07", "08", "10", "12"} else "29"
        as_of_month = int(month) + 1
        as_of_year = int(year)
        if as_of_month > 12:
            as_of_month = 1
            as_of_year += 1
        if close_by_symbol is not None and "SP500TR" in close_by_symbol:
            sp_close = close_by_symbol["SP500TR"][index]
        else:
            sp_close = Decimal("100") + Decimal(index)
        if close_by_symbol is not None and "USDKRW" in close_by_symbol:
            fx_close = close_by_symbol["USDKRW"][index]
        else:
            fx_close = Decimal("1300") + Decimal(index)
        sp_rows.append(
            f"{year}-{month}-{day},{as_of_year:04d}-{as_of_month:02d}-01T00:00:00+00:00,"
            f"SP500TR,US,{sp_close},synthetic"
        )
        fx_rows.append(
            f"{year}-{month}-{day},{as_of_year:04d}-{as_of_month:02d}-01T00:00:00+00:00,"
            f"USDKRW,FX,{fx_close},synthetic"
        )
    _write_csv(data_root / benchmark.sp500tr_relative_path, tuple(sp_rows))
    _write_csv(data_root / benchmark.usdkrw_relative_path, tuple(fx_rows))


def _prepare_default_layout(
    tmp_path: Path,
    *,
    periods: tuple[str, ...] | None = None,
) -> tuple[Path, Path]:
    repo_root, data_root = _layout(tmp_path)
    if periods is None:
        _write_default_kospi_primary_csvs(data_root)
    else:
        _write_default_kospi_primary_csvs(data_root, periods=periods)
    return repo_root, data_root


def _run_dry_run(tmp_path: Path) -> LocalMonthlyEvaluationDryRunResult:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    return run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )


def test_builds_local_monthly_evaluation_dry_run_result_from_synthetic_csvs(
    tmp_path: Path,
) -> None:
    result = _run_dry_run(tmp_path)
    assert (
        result.local_monthly_evaluation_dry_run_policy
        == LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1
    )
    assert result.dataset.source_records
    assert result.run_config.period_specs
    assert result.walk_forward_result.steps
    assert result.benchmark_relative_result.metrics
    assert result.report_bundle.markdown_report.strip()


def test_calls_assemble_local_monthly_dataset(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.assemble_local_monthly_dataset",
        wraps=assemble_local_monthly_dataset,
    ) as mocked:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )
    mocked.assert_called_once()


def test_calls_build_kospi_primary_monthly_run_config(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.build_kospi_primary_monthly_run_config"
    ) as mocked:
        mocked.side_effect = lambda **kwargs: __import__(
            "backtest_engine.local_run_config",
            fromlist=["build_kospi_primary_monthly_run_config"],
        ).build_kospi_primary_monthly_run_config(**kwargs)
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )
    mocked.assert_called_once()


def test_calls_run_explicit_schedule_rules_walk_forward_nav(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.run_explicit_schedule_rules_walk_forward_nav",
        wraps=__import__(
            "backtest_engine.walk_forward",
            fromlist=["run_explicit_schedule_rules_walk_forward_nav"],
        ).run_explicit_schedule_rules_walk_forward_nav,
    ) as mocked:
        result = run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )
        mocked.assert_called_once()
        assert mocked.call_args.args[0] == result.dataset.source_records


def test_calls_compute_walk_forward_benchmark_relative_metrics(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.compute_walk_forward_benchmark_relative_metrics"
    ) as mocked:
        mocked.side_effect = lambda **kwargs: __import__(
            "backtest_engine.benchmark_adapter",
            fromlist=["compute_walk_forward_benchmark_relative_metrics"],
        ).compute_walk_forward_benchmark_relative_metrics(**kwargs)
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )
    mocked.assert_called_once()


def test_run_local_monthly_evaluation_dry_run_calls_alignment_helper_before_adapter(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.align_local_monthly_benchmark_points_to_nav_calendar",
        wraps=align_local_monthly_benchmark_points_to_nav_calendar,
    ) as align_mock:
        with patch(
            "backtest_engine.local_evaluation.compute_walk_forward_benchmark_relative_metrics",
            wraps=__import__(
                "backtest_engine.benchmark_adapter",
                fromlist=["compute_walk_forward_benchmark_relative_metrics"],
            ).compute_walk_forward_benchmark_relative_metrics,
        ) as adapter_mock:
            run_local_monthly_evaluation_dry_run(
                repo_root=repo_root,
                data_root=data_root,
            )
    align_mock.assert_called_once()
    adapter_mock.assert_called_once()
    expected_aligned = align_local_monthly_benchmark_points_to_nav_calendar(
        **align_mock.call_args.kwargs
    )
    assert adapter_mock.call_args.kwargs["benchmark_points"] == expected_aligned


def test_calls_render_backtest_evaluation_report_bundle(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.render_backtest_evaluation_report_bundle"
    ) as mocked:
        mocked.side_effect = lambda **kwargs: __import__(
            "backtest_engine.report_bundle",
            fromlist=["render_backtest_evaluation_report_bundle"],
        ).render_backtest_evaluation_report_bundle(**kwargs)
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )
    mocked.assert_called_once()


def test_does_not_call_synthetic_pipeline_wrapper(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.evaluation_pipeline.run_explicit_synthetic_backtest_evaluation_pipeline"
    ) as mocked:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )
    mocked.assert_not_called()


def test_default_kospi_primary_dry_run_succeeds_with_one_row_per_period_csvs(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=None,
        benchmark_spec=None,
    )
    assert result.walk_forward_result.steps
    assert result.dataset.instrument_specs == (
        default_local_monthly_instrument_specs_for_kospi_primary()
    )
    assert result.dataset.benchmark_spec == default_local_monthly_benchmark_spec()
    assert not any(
        "sp500tr_asset_monthly.csv" in spec.relative_path
        for spec in result.dataset.instrument_specs
    )


def _write_staggered_kospi_primary_csvs(
    data_root: Path,
    *,
    periods: tuple[str, ...] = (
        "2020-01",
        "2020-02",
        "2020-03",
        "2020-04",
        "2020-05",
        "2020-06",
        "2020-07",
    ),
) -> None:
    specs = default_local_monthly_instrument_specs_for_kospi_primary()
    benchmark = default_local_monthly_benchmark_spec()
    staggered_as_of_by_symbol = {
        "KOSPI": ("01T00:00:00+00:00", "01T06:00:00+00:00", "01T12:00:00+00:00"),
        "GLD": ("01T06:00:00+00:00", "01T12:00:00+00:00", "01T18:00:00+00:00"),
        "SP500TR": ("01T12:00:00+00:00", "01T18:00:00+00:00", "02T00:00:00+00:00"),
    }

    for spec in specs:
        rows: list[str] = []
        for index, period in enumerate(periods):
            year, month = period.split("-")
            day = "31" if month in {"01", "03", "05", "07", "08", "10", "12"} else "29"
            close = Decimal("100") + Decimal(index)
            as_of_month = int(month) + 1
            as_of_year = int(year)
            if as_of_month > 12:
                as_of_month = 1
                as_of_year += 1
            as_of_suffix = staggered_as_of_by_symbol[spec.symbol][index % 3]
            rows.append(
                f"{year}-{month}-{day},{as_of_year:04d}-{as_of_month:02d}-{as_of_suffix},"
                f"{spec.symbol},{spec.market},{close},synthetic"
            )
        _write_csv(data_root / spec.relative_path, tuple(rows))

    sp_rows: list[str] = []
    fx_rows: list[str] = []
    for index, period in enumerate(periods):
        year, month = period.split("-")
        day = "31" if month in {"01", "03", "05", "07", "08", "10", "12"} else "29"
        as_of_month = int(month) + 1
        as_of_year = int(year)
        if as_of_month > 12:
            as_of_month = 1
            as_of_year += 1
        sp_as_of = staggered_as_of_by_symbol["SP500TR"][index % 3]
        fx_as_of = "01T23:58:00+00:00"
        sp_rows.append(
            f"{year}-{month}-{day},{as_of_year:04d}-{as_of_month:02d}-{sp_as_of},"
            f"SP500TR,US,{100 + index},synthetic"
        )
        fx_rows.append(
            f"{year}-{month}-{day},{as_of_year:04d}-{as_of_month:02d}-{fx_as_of},"
            f"USDKRW,FX,{1300 + index},synthetic"
        )
    _write_csv(data_root / benchmark.sp500tr_relative_path, tuple(sp_rows))
    _write_csv(data_root / benchmark.usdkrw_relative_path, tuple(fx_rows))


def test_staggered_timestamp_synthetic_dry_run_completes_without_execution_price_blocker(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _layout(tmp_path)
    _write_staggered_kospi_primary_csvs(data_root)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    assert result.walk_forward_result.steps
    assert result.run_config.local_monthly_run_config_policy == (
        "kospi_primary_monthly_rules_config.v3"
    )


def test_local_benchmark_calendar_alignment_policy_constant_exists() -> None:
    assert LOCAL_BENCHMARK_CALENDAR_ALIGNMENT_POLICY_V1 == (
        "local_monthly_benchmark_points_aligned_to_strategy_nav_calendar.v1"
    )


def test_align_local_monthly_benchmark_points_helper_exists() -> None:
    assert callable(align_local_monthly_benchmark_points_to_nav_calendar)


def _dry_run_alignment_inputs(
    tmp_path: Path,
) -> tuple[
    LocalMonthlyRunConfig,
    BacktestWalkForwardResult,
    tuple[BenchmarkReturnPoint, ...],
]:
    result = _run_dry_run(tmp_path)
    return (
        result.run_config,
        result.walk_forward_result,
        result.dataset.benchmark_points,
    )


def test_align_returns_one_aligned_benchmark_point_per_nav_point(tmp_path: Path) -> None:
    run_config, walk_forward_result, benchmark_points = _dry_run_alignment_inputs(
        tmp_path
    )
    aligned = align_local_monthly_benchmark_points_to_nav_calendar(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_points=benchmark_points,
    )
    assert len(aligned) == len(walk_forward_result.nav_points)


def test_align_sets_benchmark_as_of_to_corresponding_nav_as_of(tmp_path: Path) -> None:
    run_config, walk_forward_result, benchmark_points = _dry_run_alignment_inputs(
        tmp_path
    )
    aligned = align_local_monthly_benchmark_points_to_nav_calendar(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_points=benchmark_points,
    )
    for nav_point, aligned_point in zip(
        walk_forward_result.nav_points,
        aligned,
        strict=True,
    ):
        assert aligned_point.as_of == nav_point.as_of


def test_align_preserves_benchmark_values_except_as_of(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    _write_staggered_kospi_primary_csvs(data_root)
    dataset = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=default_local_monthly_instrument_specs_for_kospi_primary(),
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )
    run_config = build_kospi_primary_monthly_run_config(dataset=dataset)
    walk_forward_result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    ).walk_forward_result
    benchmark_points = dataset.benchmark_points
    aligned = align_local_monthly_benchmark_points_to_nav_calendar(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_points=benchmark_points,
    )
    common_periods = run_config.dataset.common_periods
    rolling_lookback_count = run_config.rolling_lookback_count
    period_to_benchmark = {
        fx_point.period_key: benchmark_points[index]
        for index, fx_point in enumerate(run_config.dataset.fx_points)
    }
    for index, aligned_point in enumerate(aligned):
        execution_period = common_periods[rolling_lookback_count + index]
        source = period_to_benchmark[execution_period]
        assert aligned_point.total_return_index_value == source.total_return_index_value
        if aligned_point.as_of == source.as_of:
            pytest.fail("expected aligned benchmark as_of to differ from source")


def test_align_derives_execution_periods_from_common_periods_and_lookback(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result, benchmark_points = _dry_run_alignment_inputs(
        tmp_path
    )
    aligned = align_local_monthly_benchmark_points_to_nav_calendar(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_points=benchmark_points,
    )
    common_periods = run_config.dataset.common_periods
    rolling_lookback_count = run_config.rolling_lookback_count
    period_to_benchmark = {
        fx_point.period_key: benchmark_points[index]
        for index, fx_point in enumerate(run_config.dataset.fx_points)
    }
    for index, aligned_point in enumerate(aligned):
        execution_period = common_periods[rolling_lookback_count + index]
        assert (
            aligned_point.total_return_index_value
            == period_to_benchmark[execution_period].total_return_index_value
        )


def test_align_rejects_missing_benchmark_execution_period(tmp_path: Path) -> None:
    run_config, walk_forward_result, benchmark_points = _dry_run_alignment_inputs(
        tmp_path
    )
    missing_execution_period = run_config.dataset.common_periods[
        run_config.rolling_lookback_count
    ]
    fx_points = tuple(
        fx_point
        for fx_point in run_config.dataset.fx_points
        if fx_point.period_key != missing_execution_period
    )
    trimmed_benchmark = tuple(
        benchmark_points[index]
        for index, fx_point in enumerate(run_config.dataset.fx_points)
        if fx_point.period_key != missing_execution_period
    )
    trimmed_dataset = run_config.dataset.model_copy(
        update={"fx_points": fx_points, "benchmark_points": trimmed_benchmark}
    )
    trimmed_run_config = run_config.model_copy(update={"dataset": trimmed_dataset})
    with pytest.raises(
        ValueError,
        match=f"missing benchmark point for execution period: {missing_execution_period}",
    ):
        align_local_monthly_benchmark_points_to_nav_calendar(
            run_config=trimmed_run_config,
            walk_forward_result=walk_forward_result,
            benchmark_points=trimmed_benchmark,
        )


def test_align_rejects_duplicate_benchmark_period_keys_if_detectable(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result, benchmark_points = _dry_run_alignment_inputs(
        tmp_path
    )
    duplicated_fx_points = (
        run_config.dataset.fx_points[0],
        run_config.dataset.fx_points[0],
        *run_config.dataset.fx_points[2:],
    )
    dataset_dump = run_config.dataset.model_dump()
    dataset_dump["fx_points"] = duplicated_fx_points
    duplicated_dataset = LocalMonthlyDatasetAssemblyResult.model_construct(
        **dataset_dump
    )
    run_config_dump = run_config.model_dump()
    run_config_dump["dataset"] = duplicated_dataset
    duplicated_run_config = LocalMonthlyRunConfig.model_construct(**run_config_dump)
    with pytest.raises(ValueError, match="duplicate benchmark period key"):
        align_local_monthly_benchmark_points_to_nav_calendar(
            run_config=duplicated_run_config,
            walk_forward_result=walk_forward_result,
            benchmark_points=benchmark_points,
        )


def test_align_rejects_mismatched_benchmark_fx_period_lengths(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result, benchmark_points = _dry_run_alignment_inputs(
        tmp_path
    )
    with pytest.raises(ValueError, match="benchmark_points length must equal"):
        align_local_monthly_benchmark_points_to_nav_calendar(
            run_config=run_config,
            walk_forward_result=walk_forward_result,
            benchmark_points=benchmark_points + benchmark_points[:1],
        )


def test_align_rejects_fewer_than_two_nav_points(tmp_path: Path) -> None:
    run_config, walk_forward_result, benchmark_points = _dry_run_alignment_inputs(
        tmp_path
    )
    single_nav_walk_forward = walk_forward_result.model_copy(
        update={
            "nav_points": walk_forward_result.nav_points[:1],
            "steps": walk_forward_result.steps[:1],
        }
    )
    single_period_run_config = run_config.model_copy(
        update={"period_specs": run_config.period_specs[:1]}
    )
    with pytest.raises(ValueError, match="at least 2 walk-forward NAV points"):
        align_local_monthly_benchmark_points_to_nav_calendar(
            run_config=single_period_run_config,
            walk_forward_result=single_nav_walk_forward,
            benchmark_points=benchmark_points,
        )


def test_align_does_not_mutate_original_benchmark_points(tmp_path: Path) -> None:
    run_config, walk_forward_result, benchmark_points = _dry_run_alignment_inputs(
        tmp_path
    )
    before = tuple(
        BenchmarkReturnPoint(
            as_of=point.as_of,
            total_return_index_value=point.total_return_index_value,
        )
        for point in benchmark_points
    )
    align_local_monthly_benchmark_points_to_nav_calendar(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_points=benchmark_points,
    )
    assert benchmark_points == before


def test_align_does_not_mutate_walk_forward_result(tmp_path: Path) -> None:
    run_config, walk_forward_result, benchmark_points = _dry_run_alignment_inputs(
        tmp_path
    )
    before_nav_points = walk_forward_result.nav_points
    align_local_monthly_benchmark_points_to_nav_calendar(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_points=benchmark_points,
    )
    assert walk_forward_result.nav_points == before_nav_points


def test_staggered_timestamp_synthetic_dry_run_completes_through_benchmark_metrics(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _layout(tmp_path)
    _write_staggered_kospi_primary_csvs(data_root)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    assert result.benchmark_relative_result.metrics
    assert len(result.benchmark_relative_result.common_dates) >= 2


def test_staggered_timestamp_dry_run_has_at_least_two_common_dates_after_alignment(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _layout(tmp_path)
    _write_staggered_kospi_primary_csvs(data_root)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    assert len(result.benchmark_relative_result.common_dates) >= 2


def test_benchmark_relative_common_dates_equal_aligned_strategy_nav_dates(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _layout(tmp_path)
    _write_staggered_kospi_primary_csvs(data_root)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    nav_dates = tuple(
        nav_point.as_of.date() for nav_point in result.walk_forward_result.nav_points
    )
    assert result.benchmark_relative_result.common_dates == nav_dates


def test_warnings_include_benchmark_calendar_alignment_notice(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert any(
        "local benchmark points are calendar-aligned to strategy NAV timestamps"
        in warning
        for warning in result.warnings
    )


def test_benchmark_adapter_module_not_modified_in_this_phase() -> None:
    benchmark_adapter_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "backtest_engine"
        / "benchmark_adapter.py"
    )
    text = benchmark_adapter_path.read_text(encoding="utf-8")
    assert "LOCAL_BENCHMARK_CALENDAR_ALIGNMENT_POLICY_V1" not in text
    assert "align_local_monthly_benchmark_points_to_nav_calendar" not in text


def test_mixed_regime_us_kr_risk_on_gold_risk_off_dry_run_succeeds(
    tmp_path: Path,
) -> None:
    """US/KR risk-on with GLD risk-off must not violate the cash floor under v3 weights."""
    repo_root, data_root = _layout(tmp_path)
    periods = ("2020-01", "2020-02", "2020-03", "2020-04", "2020-05")
    rising = tuple(Decimal("100") + Decimal(index * 10) for index in range(len(periods)))
    gld_mixed = (
        Decimal("100"),
        Decimal("120"),
        Decimal("90"),
        Decimal("95"),
        Decimal("100"),
    )
    _write_default_kospi_primary_csvs(
        data_root,
        periods=periods,
        close_by_symbol={
            "SP500TR": rising,
            "KOSPI": rising,
            "GLD": gld_mixed,
        },
    )
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    assert result.walk_forward_result.steps
    assert result.run_config.local_monthly_run_config_policy.endswith(".v3")


def test_long_decimal_synthetic_csv_dry_run_preserves_aggregate_cost_invariants(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _layout(tmp_path)
    periods = ("2020-01", "2020-02", "2020-03", "2020-04", "2020-05")
    long_kospi = tuple(
        Decimal("98765.432109876543210987654321987654321") + Decimal(index)
        for index in range(len(periods))
    )
    long_gld = tuple(
        Decimal("2345.678901234567890123456789012345678901") + Decimal(index)
        for index in range(len(periods))
    )
    long_sp = tuple(
        Decimal("123456.789012345678901234567890123456789") + Decimal(index)
        for index in range(len(periods))
    )
    long_fx = tuple(
        Decimal("1345.67890123456789012345678901234567890123456789012")
        + Decimal(index)
        for index in range(len(periods))
    )
    _write_default_kospi_primary_csvs(
        data_root,
        periods=periods,
        close_by_symbol={
            "KOSPI": long_kospi,
            "GLD": long_gld,
            "SP500TR": long_sp,
            "USDKRW": long_fx,
        },
    )
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    for step in result.walk_forward_result.steps:
        rebalance = step.rebalance_result
        assert rebalance.total_cost_krw == _canonical_total_cost_krw(
            rebalance.total_fee_krw,
            rebalance.total_tax_krw,
            rebalance.total_fx_spread_krw,
        )


def test_uses_default_kospi_primary_instrument_specs_when_none_supplied(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=None,
    )
    assert result.dataset.instrument_specs == (
        default_local_monthly_instrument_specs_for_kospi_primary()
    )


def test_uses_default_benchmark_spec_when_none_supplied(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.assemble_local_monthly_dataset",
        wraps=assemble_local_monthly_dataset,
    ) as mocked_assemble:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            benchmark_spec=None,
        )
    assert mocked_assemble.call_args.kwargs["benchmark_spec"] == (
        default_local_monthly_benchmark_spec()
    )


def test_preserves_dataset(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    dataset = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=default_local_monthly_instrument_specs_for_kospi_primary(),
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    assert result.dataset == dataset


def test_preserves_run_config(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert result.run_config.dataset == result.dataset
    assert result.run_config.period_specs


def test_preserves_walk_forward_result(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert result.walk_forward_result.initial_portfolio_state == (
        result.run_config.initial_portfolio_state
    )
    assert len(result.walk_forward_result.steps) == len(result.run_config.period_specs)


def test_preserves_benchmark_relative_result(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert (
        result.benchmark_relative_result.walk_forward_result
        == result.walk_forward_result
    )


def test_preserves_report_bundle(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert (
        result.report_bundle.benchmark_relative_result
        == result.benchmark_relative_result
    )


def test_produces_non_empty_markdown_report_in_memory(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert result.report_bundle.markdown_report.strip()


def test_does_not_write_markdown_report_file(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch("pathlib.Path.write_text") as mocked_write_text:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )
    mocked_write_text.assert_not_called()


def test_does_not_write_json_artifact(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch("json.dump") as mocked_json_dump:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )
    mocked_json_dump.assert_not_called()


def test_does_not_call_open_for_write(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch("builtins.open") as mocked_open:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )
    for call in mocked_open.call_args_list:
        mode = call.args[1] if len(call.args) > 1 else call.kwargs.get("mode", "r")
        assert "w" not in str(mode)


def test_does_not_fetch_or_download_data(tmp_path: Path) -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "yfinance",
        "fred",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "websocket",
        "websockets",
        "aiohttp",
        "subprocess",
        "os.system",
        "uv run",
    )
    for token in forbidden:
        assert token not in text, f"forbidden token present: {token}"


def test_does_not_import_yfinance_fred_or_network_libraries() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)
    forbidden_roots = {
        "yfinance",
        "fred",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "websocket",
        "websockets",
        "aiohttp",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_roots


def test_does_not_write_sqlite_or_call_save_record() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "SQLiteDateIdSourceStore" not in text
    assert ".save_record(" not in text


def test_does_not_import_runtime_packages() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)
    forbidden_import_roots = {
        "scout",
        "allocator",
        "risk",
        "broker",
        "orders",
        "emergency",
        "composition",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots


def test_result_model_is_frozen_and_forbids_extra_fields(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)

    with pytest.raises(ValidationError):
        result.local_monthly_evaluation_dry_run_policy = "other"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        LocalMonthlyEvaluationDryRunResult(
            local_monthly_evaluation_dry_run_policy=(
                LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1
            ),
            dataset=result.dataset,
            run_config=result.run_config,
            walk_forward_result=result.walk_forward_result,
            benchmark_relative_result=result.benchmark_relative_result,
            report_bundle=result.report_bundle,
            warnings=result.warnings,
            artifact_path="/tmp/forbidden",  # type: ignore[call-arg]
        )


def test_result_has_no_forbidden_fields() -> None:
    fields = set(LocalMonthlyEvaluationDryRunResult.model_fields)
    forbidden = {
        "output_path",
        "artifact_path",
        "markdown_report_path",
        "persisted_report_path",
        "report_path",
        "recommendation",
        "recommendations",
        "investment_advice",
        "conclusion",
        "project_conclusion",
    }
    assert fields.isdisjoint(forbidden)


def test_warnings_include_research_only_no_investment_conclusion(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert any(
        "research evidence only" in warning and "investment conclusion" in warning
        for warning in result.warnings
    )


def test_warnings_include_kospi_proxy_caveat(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert any("KOSPI primary is a KR proxy" in warning for warning in result.warnings)


def test_propagates_dataset_assembly_failure(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    with pytest.raises(ValueError, match="CSV file not found"):
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )


def test_propagates_run_config_failure(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(
        tmp_path,
        periods=("2020-01", "2020-02", "2020-03"),
    )
    with pytest.raises(ValueError, match="rolling_lookback_count \\+ 1"):
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            rolling_lookback_count=3,
        )


def test_propagates_walk_forward_failure(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.run_explicit_schedule_rules_walk_forward_nav",
        side_effect=ValueError("walk-forward failed"),
    ):
        with pytest.raises(ValueError, match="walk-forward failed"):
            run_local_monthly_evaluation_dry_run(
                repo_root=repo_root,
                data_root=data_root,
            )


def test_propagates_benchmark_adapter_failure(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.compute_walk_forward_benchmark_relative_metrics",
        side_effect=ValueError("benchmark adapter failed"),
    ):
        with pytest.raises(ValueError, match="benchmark adapter failed"):
            run_local_monthly_evaluation_dry_run(
                repo_root=repo_root,
                data_root=data_root,
            )


def test_propagates_report_bundle_failure(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.render_backtest_evaluation_report_bundle",
        side_effect=ValueError("report bundle failed"),
    ):
        with pytest.raises(ValueError, match="report bundle failed"):
            run_local_monthly_evaluation_dry_run(
                repo_root=repo_root,
                data_root=data_root,
            )


def test_module_does_not_import_forbidden_runtime_packages() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)
    forbidden_import_roots = {
        "scout",
        "allocator",
        "risk",
        "broker",
        "orders",
        "emergency",
        "composition",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots


def test_module_has_no_forbidden_runtime_or_imports() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)

    forbidden_import_roots = {
        "scout",
        "allocator",
        "risk",
        "broker",
        "orders",
        "emergency",
        "composition",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots

    forbidden_text = (
        "pandas",
        "read_csv",
        "to_csv",
        "yfinance",
        "fred",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "websocket",
        "websockets",
        "aiohttp",
        "SQLiteDateIdSourceStore",
        ".save_record(",
        "uv run",
        "subprocess",
        "os.system",
        "ScoutInputBuilder",
        "AllocatorDecision",
        "AllocationRegime",
        "open(",
        ".write(",
        "Path.write_text",
        "json.dump",
        "markdown_report_path",
        "artifact_path",
        "recommendation",
        "investment advice",
        "beats S&P",
        "beat S&P",
        "run_explicit_synthetic_backtest_evaluation_pipeline",
    )
    for token in forbidden_text:
        assert token not in text, f"forbidden token present: {token}"

    allowed_calls = (
        "assemble_local_monthly_dataset",
        "build_kospi_primary_monthly_run_config",
        "run_explicit_schedule_rules_walk_forward_nav",
        "validate_local_monthly_walk_forward_nav_sanity",
        "align_local_monthly_benchmark_points_to_nav_calendar",
        "compute_walk_forward_benchmark_relative_metrics",
        "render_backtest_evaluation_report_bundle",
    )
    for call in allowed_calls:
        assert call in text, f"expected allowed call present: {call}"


def _sanity_inputs(
    tmp_path: Path,
) -> tuple[LocalMonthlyRunConfig, BacktestWalkForwardResult]:
    result = _run_dry_run(tmp_path)
    return result.run_config, result.walk_forward_result


def test_local_nav_sanity_policy_constant_exists() -> None:
    assert LOCAL_NAV_SANITY_POLICY_V1 == (
        "local_monthly_walk_forward_nav_sanity.v1"
    )


def test_validate_local_monthly_walk_forward_nav_sanity_helper_exists() -> None:
    assert callable(validate_local_monthly_walk_forward_nav_sanity)


def test_sane_synthetic_dry_run_passes_nav_sanity(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    warnings = validate_local_monthly_walk_forward_nav_sanity(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
    )
    assert "local monthly walk-forward NAV passed deterministic sanity checks" in warnings


def test_nav_sanity_rejects_non_positive_nav(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    corrupted_nav = walk_forward_result.nav_points[0].model_copy(
        update={"portfolio_value_krw": Decimal("0")}
    )
    corrupted = walk_forward_result.model_copy(
        update={"nav_points": (corrupted_nav, *walk_forward_result.nav_points[1:])}
    )
    with pytest.raises(ValueError, match="portfolio_value_krw must be positive"):
        validate_local_monthly_walk_forward_nav_sanity(
            run_config=run_config,
            walk_forward_result=corrupted,
        )


def test_nav_sanity_rejects_cash_above_portfolio_value(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    nav_point = walk_forward_result.nav_points[0]
    corrupted_nav = nav_point.model_copy(
        update={"cash_krw": nav_point.portfolio_value_krw + Decimal("1")}
    )
    corrupted = walk_forward_result.model_copy(
        update={"nav_points": (corrupted_nav, *walk_forward_result.nav_points[1:])}
    )
    with pytest.raises(ValueError, match="cash_krw must be <= portfolio_value_krw"):
        validate_local_monthly_walk_forward_nav_sanity(
            run_config=run_config,
            walk_forward_result=corrupted,
        )


def test_nav_sanity_rejects_negative_cash(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    step = walk_forward_result.steps[0]
    rebalance = step.rebalance_result.model_copy(update={"cash_krw_after": Decimal("-1")})
    corrupted_step = step.model_copy(update={"rebalance_result": rebalance})
    corrupted = walk_forward_result.model_copy(update={"steps": (corrupted_step, *walk_forward_result.steps[1:])})
    with pytest.raises(ValueError, match="cash_krw_after must be >= 0"):
        validate_local_monthly_walk_forward_nav_sanity(
            run_config=run_config,
            walk_forward_result=corrupted,
        )


def test_nav_sanity_rejects_period_return_above_max_abs_period_return(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    first_nav = walk_forward_result.nav_points[0]
    second_nav = walk_forward_result.nav_points[1].model_copy(
        update={
            "portfolio_value_krw": first_nav.portfolio_value_krw * Decimal("3"),
            "cash_krw": walk_forward_result.nav_points[1].cash_krw,
        }
    )
    corrupted = walk_forward_result.model_copy(
        update={"nav_points": (first_nav, second_nav)}
    )
    with pytest.raises(ValueError, match="period return exceeds max_abs_period_return"):
        validate_local_monthly_walk_forward_nav_sanity(
            run_config=run_config,
            walk_forward_result=corrupted,
        )


def test_nav_sanity_rejects_terminal_return_above_max_terminal_return(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    last_nav = walk_forward_result.nav_points[-1].model_copy(
        update={"portfolio_value_krw": Decimal("3000000000")}
    )
    corrupted = walk_forward_result.model_copy(
        update={"nav_points": (*walk_forward_result.nav_points[:-1], last_nav)}
    )
    with pytest.raises(ValueError, match="terminal strategy return exceeds max_terminal_return"):
        validate_local_monthly_walk_forward_nav_sanity(
            run_config=run_config,
            walk_forward_result=corrupted,
            max_abs_period_return=Decimal("100"),
        )


def test_nav_sanity_rejects_duplicate_holdings(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    step = walk_forward_result.steps[0]
    duplicate = (
        BacktestHolding(asset_id="kospi", quantity=Decimal("1")),
        BacktestHolding(asset_id="kospi", quantity=Decimal("2")),
    )
    rebalance = step.rebalance_result.model_copy(update={"post_trade_holdings": duplicate})
    corrupted_step = step.model_copy(update={"rebalance_result": rebalance})
    corrupted = walk_forward_result.model_copy(update={"steps": (corrupted_step, *walk_forward_result.steps[1:])})
    with pytest.raises(ValueError, match="unique asset ids"):
        validate_local_monthly_walk_forward_nav_sanity(
            run_config=run_config,
            walk_forward_result=corrupted,
        )


def test_nav_sanity_rejects_non_positive_trade_quantity(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    step = walk_forward_result.steps[0]
    if not step.rebalance_result.trades:
        pytest.skip("synthetic dry-run produced no trades in first step")
    trade = step.rebalance_result.trades[0]
    bad_trade = BacktestTrade.model_construct(
        asset_id=trade.asset_id,
        symbol=trade.symbol,
        market=trade.market,
        side=trade.side,
        quantity=Decimal("0"),
        execution_price=trade.execution_price,
        usdkrw_rate=trade.usdkrw_rate,
        gross_notional_krw=trade.gross_notional_krw,
        fee_krw=trade.fee_krw,
        tax_krw=trade.tax_krw,
        fx_spread_krw=trade.fx_spread_krw,
        total_cost_krw=trade.total_cost_krw,
    )
    rebalance = step.rebalance_result.model_copy(update={"trades": (bad_trade,)})
    corrupted_step = step.model_copy(update={"rebalance_result": rebalance})
    corrupted = walk_forward_result.model_copy(update={"steps": (corrupted_step, *walk_forward_result.steps[1:])})
    with pytest.raises(ValueError, match="quantity must be finite and positive"):
        validate_local_monthly_walk_forward_nav_sanity(
            run_config=run_config,
            walk_forward_result=corrupted,
        )


def test_nav_sanity_rejects_trade_gross_notional_above_nav_multiple(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    step = walk_forward_result.steps[0]
    pre_trade = step.rebalance_result.pre_trade_portfolio_value_krw
    huge_notional = pre_trade * Decimal("3")
    bad_trade = BacktestTrade.model_construct(
        asset_id="kospi",
        symbol="KOSPI",
        market="KR",
        side="BUY",
        quantity=Decimal("1"),
        execution_price=Decimal("1"),
        usdkrw_rate=None,
        gross_notional_krw=huge_notional,
        fee_krw=Decimal("0"),
        tax_krw=Decimal("0"),
        fx_spread_krw=Decimal("0"),
        total_cost_krw=Decimal("0"),
    )
    rebalance = step.rebalance_result.model_copy(update={"trades": (bad_trade,)})
    corrupted_step = step.model_copy(update={"rebalance_result": rebalance})
    corrupted = walk_forward_result.model_copy(update={"steps": (corrupted_step, *walk_forward_result.steps[1:])})
    with pytest.raises(ValueError, match="exceeds deterministic pre-trade NAV multiple"):
        validate_local_monthly_walk_forward_nav_sanity(
            run_config=run_config,
            walk_forward_result=corrupted,
        )


def test_nav_sanity_rejects_post_trade_holdings_plus_cash_mismatch(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    step = walk_forward_result.steps[0]
    rebalance = step.rebalance_result.model_copy(
        update={"post_trade_portfolio_value_krw": step.rebalance_result.post_trade_portfolio_value_krw + Decimal("1")}
    )
    corrupted_step = step.model_copy(update={"rebalance_result": rebalance})
    corrupted = walk_forward_result.model_copy(update={"steps": (corrupted_step, *walk_forward_result.steps[1:])})
    with pytest.raises(
        ValueError,
        match=(
            "post-trade holdings value plus cash must equal "
            "post_trade_portfolio_value_krw; run sanitized NAV sanity diagnostic "
            "for this step"
        ),
    ):
        validate_local_monthly_walk_forward_nav_sanity(
            run_config=run_config,
            walk_forward_result=corrupted,
        )


def test_local_nav_sanity_diagnostic_policy_constant_exists() -> None:
    assert LOCAL_NAV_SANITY_DIAGNOSTIC_POLICY_V1 == (
        "local_monthly_walk_forward_nav_sanity_diagnostic.v1"
    )


def test_local_nav_sanity_step_diagnostic_model_is_frozen_and_forbids_extra() -> None:
    assert LocalNavSanityStepDiagnostic.model_config.get("frozen") is True
    assert LocalNavSanityStepDiagnostic.model_config.get("extra") == "forbid"


def test_build_local_nav_sanity_step_diagnostic_helper_exists() -> None:
    assert callable(build_local_nav_sanity_step_diagnostic)


def test_diagnostic_rejects_negative_step_index(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    with pytest.raises(ValueError, match="step_index must be in"):
        build_local_nav_sanity_step_diagnostic(
            run_config=run_config,
            walk_forward_result=walk_forward_result,
            step_index=-1,
        )


def test_diagnostic_rejects_out_of_range_step_index(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    with pytest.raises(ValueError, match="step_index must be in"):
        build_local_nav_sanity_step_diagnostic(
            run_config=run_config,
            walk_forward_result=walk_forward_result,
            step_index=len(run_config.period_specs),
        )


def test_diagnostic_preserves_input_objects(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    run_config_before = run_config.model_copy(deep=True)
    walk_forward_before = walk_forward_result.model_copy(deep=True)
    build_local_nav_sanity_step_diagnostic(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        step_index=0,
    )
    assert run_config == run_config_before
    assert walk_forward_result == walk_forward_before


def test_diagnostic_includes_step_period_and_timestamps(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    diagnostic = build_local_nav_sanity_step_diagnostic(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        step_index=0,
    )
    period_spec = run_config.period_specs[0]
    nav_point = walk_forward_result.nav_points[0]
    assert diagnostic.step_index == 0
    assert diagnostic.period_index == run_config.rolling_lookback_count
    assert diagnostic.decision_time == period_spec.decision_time
    assert diagnostic.intended_execution_time == period_spec.intended_execution_time
    assert diagnostic.nav_as_of == nav_point.as_of


def test_diagnostic_includes_counts_not_raw_rows(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    diagnostic = build_local_nav_sanity_step_diagnostic(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        step_index=0,
    )
    assert diagnostic.asset_count == len(
        walk_forward_result.steps[0].execution_prices.prices
    )
    assert diagnostic.trade_count == len(
        walk_forward_result.steps[0].rebalance_result.trades
    )
    assert diagnostic.holding_count == len(
        walk_forward_result.steps[0].rebalance_result.post_trade_holdings
    )
    dumped = diagnostic.model_dump()
    assert "raw_csv_row" not in dumped
    assert "source_record" not in dumped
    assert "source_name" not in dumped


def test_diagnostic_includes_aggregate_nav_cash_recomputed_and_delta(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    diagnostic = build_local_nav_sanity_step_diagnostic(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        step_index=0,
    )
    rebalance = walk_forward_result.steps[0].rebalance_result
    assert diagnostic.pre_trade_nav_krw == rebalance.pre_trade_portfolio_value_krw
    assert diagnostic.post_trade_portfolio_value_krw == (
        rebalance.post_trade_portfolio_value_krw
    )
    assert diagnostic.cash_krw_after == rebalance.cash_krw_after
    assert diagnostic.accounting_delta_krw == (
        diagnostic.recomputed_post_trade_value_krw
        - diagnostic.post_trade_portfolio_value_krw
    )
    assert diagnostic.recomputed_post_trade_value_krw >= diagnostic.cash_krw_after


def test_diagnostic_includes_accounting_delta_ratio(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    diagnostic = build_local_nav_sanity_step_diagnostic(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        step_index=0,
    )
    if diagnostic.post_trade_portfolio_value_krw == Decimal("0"):
        expected_ratio = Decimal("0")
    else:
        expected_ratio = (
            diagnostic.accounting_delta_krw / diagnostic.post_trade_portfolio_value_krw
        )
    assert diagnostic.accounting_delta_ratio == expected_ratio


def test_diagnostic_includes_max_trade_notional_to_pre_nav_ratio(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    diagnostic = build_local_nav_sanity_step_diagnostic(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        step_index=0,
    )
    rebalance = walk_forward_result.steps[0].rebalance_result
    if rebalance.trades:
        expected = max(
            trade.gross_notional_krw / rebalance.pre_trade_portfolio_value_krw
            for trade in rebalance.trades
        )
        assert diagnostic.max_trade_notional_to_pre_nav_ratio == expected
    else:
        assert diagnostic.max_trade_notional_to_pre_nav_ratio is None


def test_diagnostic_includes_asset_ids_and_markets_only(tmp_path: Path) -> None:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    diagnostic = build_local_nav_sanity_step_diagnostic(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        step_index=0,
    )
    step = walk_forward_result.steps[0]
    expected_nonzero = tuple(
        sorted(
            holding.asset_id
            for holding in step.rebalance_result.post_trade_holdings
            if holding.quantity > Decimal("0")
        )
    )
    expected_traded = tuple(
        sorted({trade.asset_id for trade in step.rebalance_result.trades})
    )
    expected_markets = tuple(
        sorted(
            {
                price_record.market
                for price_record in step.execution_prices.prices
            }
            | {trade.market for trade in step.rebalance_result.trades}
        )
    )
    assert diagnostic.nonzero_holding_asset_ids == expected_nonzero
    assert diagnostic.traded_asset_ids == expected_traded
    assert diagnostic.markets_seen == expected_markets
    dumped = diagnostic.model_dump()
    assert "source_record" not in dumped
    assert "source_name" not in dumped


def _corrupted_accounting_mismatch_inputs(
    tmp_path: Path,
) -> tuple[LocalMonthlyRunConfig, BacktestWalkForwardResult]:
    run_config, walk_forward_result = _sanity_inputs(tmp_path)
    step = walk_forward_result.steps[0]
    rebalance = step.rebalance_result.model_copy(
        update={
            "post_trade_portfolio_value_krw": (
                step.rebalance_result.post_trade_portfolio_value_krw + Decimal("1")
            )
        }
    )
    corrupted_step = step.model_copy(update={"rebalance_result": rebalance})
    corrupted = walk_forward_result.model_copy(
        update={"steps": (corrupted_step, *walk_forward_result.steps[1:])}
    )
    return run_config, corrupted


def test_diagnostic_warning_includes_accounting_delta_nonzero_for_mismatch(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result = _corrupted_accounting_mismatch_inputs(tmp_path)
    diagnostic = build_local_nav_sanity_step_diagnostic(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        step_index=0,
    )
    assert "accounting_delta_nonzero" in diagnostic.warnings


def test_diagnostic_warning_includes_cash_and_holdings_not_equal_nav_for_mismatch(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result = _corrupted_accounting_mismatch_inputs(tmp_path)
    diagnostic = build_local_nav_sanity_step_diagnostic(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        step_index=0,
    )
    assert "cash_and_holdings_not_equal_nav" in diagnostic.warnings


def test_diagnostic_model_has_no_forbidden_fields() -> None:
    fields = set(LocalNavSanityStepDiagnostic.model_fields)
    forbidden = {
        "raw_csv_row",
        "source_record",
        "source_name",
        "config_path",
        "secret",
        "recommendation",
        "investment_advice",
        "project_conclusion",
    }
    assert fields.isdisjoint(forbidden)


def test_sanity_mismatch_error_is_sanitized_without_numeric_internals(
    tmp_path: Path,
) -> None:
    run_config, walk_forward_result = _corrupted_accounting_mismatch_inputs(tmp_path)
    with pytest.raises(ValueError) as exc_info:
        validate_local_monthly_walk_forward_nav_sanity(
            run_config=run_config,
            walk_forward_result=walk_forward_result,
        )
    message = str(exc_info.value)
    assert "run sanitized NAV sanity diagnostic for this step" in message
    rebalance = walk_forward_result.steps[0].rebalance_result
    forbidden_values = (
        str(rebalance.post_trade_portfolio_value_krw),
        str(rebalance.cash_krw_after),
        str(rebalance.pre_trade_portfolio_value_krw),
    )
    for value in forbidden_values:
        assert value not in message


def test_nav_sanity_holding_value_us_gold_uses_usdkrw() -> None:
    value = _holding_value_krw_for_sanity(
        Decimal("2"),
        Decimal("100"),
        market="US",
        usdkrw_rate=Decimal("1300"),
    )
    assert value == Decimal("260000")
    gold_value = _holding_value_krw_for_sanity(
        Decimal("1"),
        Decimal("50"),
        market="GOLD",
        usdkrw_rate=Decimal("1200"),
    )
    assert gold_value == Decimal("60000")


def test_nav_sanity_holding_value_kr_does_not_use_usdkrw() -> None:
    value = _holding_value_krw_for_sanity(
        Decimal("2"),
        Decimal("100"),
        market="KR",
        usdkrw_rate=Decimal("1300"),
    )
    assert value == Decimal("200")


def test_run_local_monthly_evaluation_dry_run_calls_sanity_before_alignment(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    call_order: list[str] = []

    def _track_sanity(**kwargs: object) -> tuple[str, ...]:
        call_order.append("sanity")
        return validate_local_monthly_walk_forward_nav_sanity(**kwargs)

    def _track_align(**kwargs: object) -> tuple[BenchmarkReturnPoint, ...]:
        call_order.append("align")
        return align_local_monthly_benchmark_points_to_nav_calendar(**kwargs)

    with patch(
        "backtest_engine.local_evaluation.validate_local_monthly_walk_forward_nav_sanity",
        side_effect=_track_sanity,
    ):
        with patch(
            "backtest_engine.local_evaluation.align_local_monthly_benchmark_points_to_nav_calendar",
            side_effect=_track_align,
        ):
            run_local_monthly_evaluation_dry_run(
                repo_root=repo_root,
                data_root=data_root,
            )
    assert call_order.index("sanity") < call_order.index("align")


def test_dry_run_skips_alignment_when_nav_sanity_raises(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.validate_local_monthly_walk_forward_nav_sanity",
        side_effect=ValueError("nav sanity failed"),
    ):
        with patch(
            "backtest_engine.local_evaluation.align_local_monthly_benchmark_points_to_nav_calendar",
        ) as align_mock:
            with pytest.raises(ValueError, match="nav sanity failed"):
                run_local_monthly_evaluation_dry_run(
                    repo_root=repo_root,
                    data_root=data_root,
                )
    align_mock.assert_not_called()


def test_dry_run_skips_benchmark_adapter_when_nav_sanity_raises(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.validate_local_monthly_walk_forward_nav_sanity",
        side_effect=ValueError("nav sanity failed"),
    ):
        with patch(
            "backtest_engine.local_evaluation.compute_walk_forward_benchmark_relative_metrics",
        ) as adapter_mock:
            with pytest.raises(ValueError, match="nav sanity failed"):
                run_local_monthly_evaluation_dry_run(
                    repo_root=repo_root,
                    data_root=data_root,
                )
    adapter_mock.assert_not_called()


def test_dry_run_skips_report_bundle_when_nav_sanity_raises(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.validate_local_monthly_walk_forward_nav_sanity",
        side_effect=ValueError("nav sanity failed"),
    ):
        with patch(
            "backtest_engine.local_evaluation.render_backtest_evaluation_report_bundle",
        ) as report_mock:
            with pytest.raises(ValueError, match="nav sanity failed"):
                run_local_monthly_evaluation_dry_run(
                    repo_root=repo_root,
                    data_root=data_root,
                )
    report_mock.assert_not_called()


def test_successful_dry_run_includes_nav_sanity_passed_warning(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert any(
        "local monthly walk-forward NAV passed deterministic sanity checks" in warning
        for warning in result.warnings
    )


def test_nav_sanity_errors_exclude_raw_csv_and_secrets() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    sanity_start = text.index("def validate_local_monthly_walk_forward_nav_sanity")
    sanity_end = text.index("\ndef _holding_value_krw_for_sanity")
    sanity_text = text[sanity_start:sanity_end]
    forbidden = (
        "close_adjusted",
        "source_name",
        "config.toml",
        "api_key",
        "secret",
        "password",
        "beats S&P",
        "beat S&P",
    )
    for token in forbidden:
        assert token not in sanity_text, f"forbidden token in NAV sanity helper: {token}"


def test_focused_regression_suite_passes() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    completed = subprocess.run(
        ["uv", "run", "pytest", *FOCUSED_TEST_FILES, "-q"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
