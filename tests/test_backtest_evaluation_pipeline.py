from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine.benchmark_adapter import (  # noqa: E402
    BENCHMARK_ADAPTER_POLICY_V1,
    BacktestBenchmarkRelativeResult,
    compute_walk_forward_benchmark_relative_metrics,
)
from backtest_engine.evaluation_pipeline import (  # noqa: E402
    BACKTEST_EVALUATION_PIPELINE_POLICY_V1,
    BacktestEvaluationPipelineResult,
    run_explicit_synthetic_backtest_evaluation_pipeline,
)
from backtest_engine import (  # noqa: E402
    COST_MODEL_V1,
    BacktestCostModel,
    BacktestPeriodSpec,
    BacktestPortfolioState,
    BacktestWalkForwardResult,
    RollingLongMaAssetConfig,
    run_explicit_schedule_rules_walk_forward_nav,
)
from backtest_engine.report_bundle import (  # noqa: E402
    BacktestEvaluationReportBundle,
    render_backtest_evaluation_report_bundle,
)
from backtest_data import (  # noqa: E402
    BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    InMemoryDateIdSourceReader,
)
from domain import DateId, DateIdSourceRecord, FactType  # noqa: E402
from paper_review.models import BenchmarkReturnPoint  # noqa: E402

INITIAL_AS_OF = datetime(2020, 4, 1, 0, 0, tzinfo=UTC)
PERIOD_1_DECISION = datetime(2020, 4, 30, 0, 0, tzinfo=UTC)
PERIOD_1_EXECUTION = datetime(2020, 5, 31, 0, 0, tzinfo=UTC)
PERIOD_2_DECISION = datetime(2020, 6, 30, 0, 0, tzinfo=UTC)
PERIOD_2_EXECUTION = datetime(2020, 7, 31, 0, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
USDKRW_PERIOD_1 = Decimal("1300")
USDKRW_PERIOD_2 = Decimal("1500")
SYMBOL_A = "SYN_US_PROXY"
MARKET_A = "US"
MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "backtest_engine" / "evaluation_pipeline.py"
)

FOCUSED_TEST_FILES = (
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


def _config() -> RollingLongMaAssetConfig:
    return RollingLongMaAssetConfig(
        asset_id="asset_A",
        symbol=SYMBOL_A,
        market=MARKET_A,
        lookback_count=3,
        risk_on_weight=Decimal("0.60"),
        risk_off_weight=Decimal("0.30"),
        min_weight=Decimal("0"),
        max_weight=Decimal("0.80"),
    )


def _record(
    *,
    date_id: str,
    payload_date: str,
    source_timestamp: datetime,
    close_adjusted: object,
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.PRICE,
        source_name="monthly_synthetic",
        source_timestamp=source_timestamp,
        created_at=CREATED_AT,
        summary="synthetic price record",
        payload={
            "schema_name": BACKTEST_INSTRUMENT_PRICE_SCHEMA,
            "date": payload_date,
            "symbol": SYMBOL_A,
            "market": MARKET_A,
            "close_adjusted": close_adjusted,
        },
        symbol=SYMBOL_A,
        market=MARKET_A,
    )


def _two_period_records() -> tuple[DateIdSourceRecord, ...]:
    monthly_signal_specs = (
        ("200228-1", "2020-02-28", datetime(2020, 2, 28, tzinfo=UTC), "100"),
        ("200328-2", "2020-03-28", datetime(2020, 3, 28, tzinfo=UTC), "102"),
        ("200428-3", "2020-04-28", datetime(2020, 4, 28, tzinfo=UTC), "104"),
        ("200628-5", "2020-06-28", datetime(2020, 6, 28, tzinfo=UTC), "108"),
    )
    records = [
        _record(
            date_id=date_id,
            payload_date=payload_date,
            source_timestamp=source_timestamp,
            close_adjusted=close_adjusted,
        )
        for date_id, payload_date, source_timestamp, close_adjusted in monthly_signal_specs
    ]
    records.append(
        _record(
            date_id="200531-6",
            payload_date="2020-05-31",
            source_timestamp=PERIOD_1_EXECUTION,
            close_adjusted="110",
        )
    )
    records.append(
        _record(
            date_id="200731-7",
            payload_date="2020-07-31",
            source_timestamp=PERIOD_2_EXECUTION,
            close_adjusted="112",
        )
    )
    return tuple(records)


def _period_specs() -> tuple[BacktestPeriodSpec, ...]:
    return (
        BacktestPeriodSpec(
            decision_time=PERIOD_1_DECISION,
            intended_execution_time=PERIOD_1_EXECUTION,
            usdkrw_rate=USDKRW_PERIOD_1,
        ),
        BacktestPeriodSpec(
            decision_time=PERIOD_2_DECISION,
            intended_execution_time=PERIOD_2_EXECUTION,
            usdkrw_rate=USDKRW_PERIOD_2,
        ),
    )


def _portfolio() -> BacktestPortfolioState:
    return BacktestPortfolioState(
        as_of=INITIAL_AS_OF,
        cash_krw=Decimal("1000000"),
        holdings=(),
    )


def _cost_model() -> BacktestCostModel:
    return BacktestCostModel(
        cost_model_version=COST_MODEL_V1,
        fee_bps=Decimal("10"),
        kr_sell_tax_bps=Decimal("23"),
        fx_spread_bps=Decimal("15"),
    )


def _benchmark_at(as_of: datetime, value: str) -> BenchmarkReturnPoint:
    return BenchmarkReturnPoint(
        as_of=as_of,
        total_return_index_value=Decimal(value),
    )


def _benchmarks_for_walk_forward(
    walk_forward: BacktestWalkForwardResult,
    values: tuple[str, ...],
) -> tuple[BenchmarkReturnPoint, ...]:
    return tuple(
        _benchmark_at(nav_point.as_of, value)
        for nav_point, value in zip(walk_forward.nav_points, values, strict=True)
    )


def _run_pipeline(
    *,
    source: InMemoryDateIdSourceReader
    | Iterable[DateIdSourceRecord]
    | None = None,
    period_specs: Iterable[BacktestPeriodSpec] | None = None,
    benchmark_points: Iterable[BenchmarkReturnPoint] | None = None,
) -> BacktestEvaluationPipelineResult:
    records = _two_period_records() if source is None else source
    specs = _period_specs() if period_specs is None else period_specs
    if benchmark_points is None:
        walk_forward = run_explicit_schedule_rules_walk_forward_nav(
            records,
            period_specs=specs,
            rolling_asset_configs=(_config(),),
            initial_portfolio_state=_portfolio(),
            cost_model=_cost_model(),
            cash_asset_id="cash",
            cash_min_weight=Decimal("0.05"),
        )
        benchmark_points = _benchmarks_for_walk_forward(walk_forward, ("100", "110"))

    return run_explicit_synthetic_backtest_evaluation_pipeline(
        records,
        period_specs=specs,
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
        benchmark_points=benchmark_points,
    )


def test_builds_pipeline_result_from_synthetic_inputs() -> None:
    result = _run_pipeline()

    assert isinstance(result, BacktestEvaluationPipelineResult)
    assert (
        result.evaluation_pipeline_policy == BACKTEST_EVALUATION_PIPELINE_POLICY_V1
    )


def test_calls_existing_walk_forward_function() -> None:
    with patch(
        "backtest_engine.evaluation_pipeline.run_explicit_schedule_rules_walk_forward_nav",
        wraps=run_explicit_schedule_rules_walk_forward_nav,
    ) as mocked:
        result = _run_pipeline()

    mocked.assert_called_once()
    assert isinstance(result.walk_forward_result, BacktestWalkForwardResult)
    assert mocked.call_args.kwargs["period_specs"] == _period_specs()


def test_calls_existing_benchmark_adapter() -> None:
    with patch(
        "backtest_engine.evaluation_pipeline.compute_walk_forward_benchmark_relative_metrics",
        wraps=compute_walk_forward_benchmark_relative_metrics,
    ) as mocked:
        result = _run_pipeline()

    mocked.assert_called_once()
    assert isinstance(result.benchmark_relative_result, BacktestBenchmarkRelativeResult)
    assert (
        mocked.call_args.kwargs["walk_forward_result"]
        == result.walk_forward_result
    )


def test_calls_existing_report_bundle_renderer() -> None:
    with patch(
        "backtest_engine.evaluation_pipeline.render_backtest_evaluation_report_bundle",
        wraps=render_backtest_evaluation_report_bundle,
    ) as mocked:
        result = _run_pipeline()

    mocked.assert_called_once()
    assert isinstance(result.report_bundle, BacktestEvaluationReportBundle)
    assert (
        mocked.call_args.kwargs["benchmark_relative_result"]
        == result.benchmark_relative_result
    )


def test_preserves_walk_forward_result() -> None:
    walk_forward = run_explicit_schedule_rules_walk_forward_nav(
        _two_period_records(),
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )
    benchmarks = _benchmarks_for_walk_forward(walk_forward, ("100", "110"))

    with patch(
        "backtest_engine.evaluation_pipeline.run_explicit_schedule_rules_walk_forward_nav",
        return_value=walk_forward,
    ):
        result = run_explicit_synthetic_backtest_evaluation_pipeline(
            _two_period_records(),
            period_specs=_period_specs(),
            rolling_asset_configs=(_config(),),
            initial_portfolio_state=_portfolio(),
            cost_model=_cost_model(),
            cash_asset_id="cash",
            cash_min_weight=Decimal("0.05"),
            benchmark_points=benchmarks,
        )

    assert result.walk_forward_result is walk_forward


def test_preserves_benchmark_relative_result() -> None:
    walk_forward = run_explicit_schedule_rules_walk_forward_nav(
        _two_period_records(),
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )
    benchmarks = _benchmarks_for_walk_forward(walk_forward, ("100", "110"))
    benchmark_relative = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    with patch(
        "backtest_engine.evaluation_pipeline.compute_walk_forward_benchmark_relative_metrics",
        return_value=benchmark_relative,
    ):
        result = _run_pipeline(benchmark_points=benchmarks)

    assert result.benchmark_relative_result is benchmark_relative


def test_preserves_report_bundle() -> None:
    walk_forward = run_explicit_schedule_rules_walk_forward_nav(
        _two_period_records(),
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )
    benchmarks = _benchmarks_for_walk_forward(walk_forward, ("100", "110"))
    benchmark_relative = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )
    report_bundle = render_backtest_evaluation_report_bundle(
        benchmark_relative_result=benchmark_relative,
    )

    with patch(
        "backtest_engine.evaluation_pipeline.render_backtest_evaluation_report_bundle",
        return_value=report_bundle,
    ):
        result = _run_pipeline(benchmark_points=benchmarks)

    assert result.report_bundle is report_bundle


def test_validates_nested_linkage() -> None:
    result = _run_pipeline()

    assert (
        result.benchmark_relative_result.walk_forward_result
        == result.walk_forward_result
    )
    assert (
        result.report_bundle.benchmark_relative_result
        == result.benchmark_relative_result
    )


def test_produces_non_empty_markdown_report() -> None:
    result = _run_pipeline()

    assert isinstance(result.report_bundle.markdown_report, str)
    assert result.report_bundle.markdown_report.strip()


def test_uses_explicit_benchmark_points() -> None:
    walk_forward = run_explicit_schedule_rules_walk_forward_nav(
        _two_period_records(),
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )
    explicit_benchmarks = _benchmarks_for_walk_forward(walk_forward, ("100", "108"))

    result = run_explicit_synthetic_backtest_evaluation_pipeline(
        _two_period_records(),
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
        benchmark_points=explicit_benchmarks,
    )

    assert result.benchmark_relative_result.benchmark_points == explicit_benchmarks


def test_does_not_generate_schedule() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "generate_schedule",
        "build_rebalance_schedule",
        "build_schedule",
    )
    for token in forbidden:
        assert token not in text, f"forbidden token present: {token}"


def test_does_not_load_real_data() -> None:
    with patch("builtins.open") as mocked_open:
        _run_pipeline()

    mocked_open.assert_not_called()


def test_does_not_fetch_benchmark_data() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "yfinance",
        "fred",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "read_csv",
        "load_csv",
        "monthly.csv",
        "get_data.py",
    )
    for token in forbidden:
        assert token not in text, f"forbidden token present: {token}"


def test_does_not_write_files_or_artifacts() -> None:
    with patch("pathlib.Path.write_text") as mocked_write_text:
        _run_pipeline()

    mocked_write_text.assert_not_called()


def test_does_not_compute_benchmark_relative_metrics_manually() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "compute_benchmark_relative_metrics" not in text
    assert "compute_walk_forward_benchmark_relative_metrics" in text


def test_does_not_duplicate_report_rendering_logic() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "render_benchmark_relative_metrics_markdown" not in text
    assert "render_backtest_evaluation_report_bundle" in text


def test_materializes_one_shot_source_generator_once() -> None:
    records = _two_period_records()
    iteration_count = 0

    def _generator() -> Iterable[DateIdSourceRecord]:
        nonlocal iteration_count
        iteration_count += 1
        yield from records

    walk_forward = run_explicit_schedule_rules_walk_forward_nav(
        records,
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )
    benchmarks = _benchmarks_for_walk_forward(walk_forward, ("100", "110"))

    run_explicit_synthetic_backtest_evaluation_pipeline(
        _generator(),
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
        benchmark_points=benchmarks,
    )

    assert iteration_count == 1


def test_materializes_one_shot_period_specs_once() -> None:
    specs = _period_specs()
    iteration_count = 0

    def _generator() -> Iterable[BacktestPeriodSpec]:
        nonlocal iteration_count
        iteration_count += 1
        yield from specs

    walk_forward = run_explicit_schedule_rules_walk_forward_nav(
        _two_period_records(),
        period_specs=specs,
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )
    benchmarks = _benchmarks_for_walk_forward(walk_forward, ("100", "110"))

    run_explicit_synthetic_backtest_evaluation_pipeline(
        _two_period_records(),
        period_specs=_generator(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
        benchmark_points=benchmarks,
    )

    assert iteration_count == 1


def test_materializes_one_shot_benchmark_points_once() -> None:
    walk_forward = run_explicit_schedule_rules_walk_forward_nav(
        _two_period_records(),
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )
    benchmarks = _benchmarks_for_walk_forward(walk_forward, ("100", "110"))
    iteration_count = 0

    def _generator() -> Iterable[BenchmarkReturnPoint]:
        nonlocal iteration_count
        iteration_count += 1
        yield from benchmarks

    run_explicit_synthetic_backtest_evaluation_pipeline(
        _two_period_records(),
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
        benchmark_points=_generator(),
    )

    assert iteration_count == 1


def test_propagates_walk_forward_failure() -> None:
    with pytest.raises(ValueError, match="period_specs must not be empty"):
        run_explicit_synthetic_backtest_evaluation_pipeline(
            _two_period_records(),
            period_specs=(),
            rolling_asset_configs=(_config(),),
            initial_portfolio_state=_portfolio(),
            cost_model=_cost_model(),
            cash_asset_id="cash",
            cash_min_weight=Decimal("0.05"),
            benchmark_points=(
                _benchmark_at(PERIOD_1_EXECUTION, "100"),
                _benchmark_at(PERIOD_2_EXECUTION, "110"),
            ),
        )


def test_propagates_benchmark_adapter_failure() -> None:
    with pytest.raises(ValueError, match="benchmark_points must not be empty"):
        run_explicit_synthetic_backtest_evaluation_pipeline(
            _two_period_records(),
            period_specs=_period_specs(),
            rolling_asset_configs=(_config(),),
            initial_portfolio_state=_portfolio(),
            cost_model=_cost_model(),
            cash_asset_id="cash",
            cash_min_weight=Decimal("0.05"),
            benchmark_points=(),
        )


def test_propagates_report_renderer_failure() -> None:
    with patch(
        "backtest_engine.evaluation_pipeline.render_backtest_evaluation_report_bundle",
        side_effect=ValueError("renderer failed"),
    ):
        with pytest.raises(ValueError, match="renderer failed"):
            _run_pipeline()


def test_result_model_is_frozen_and_forbids_extra_fields() -> None:
    result = _run_pipeline()

    with pytest.raises(ValidationError):
        result.evaluation_pipeline_policy = "other"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        BacktestEvaluationPipelineResult(
            evaluation_pipeline_policy=BACKTEST_EVALUATION_PIPELINE_POLICY_V1,
            walk_forward_result=result.walk_forward_result,
            benchmark_relative_result=result.benchmark_relative_result,
            report_bundle=result.report_bundle,
            recommendation="forbidden",  # type: ignore[call-arg]
        )


def test_result_has_no_forbidden_fields() -> None:
    forbidden = {
        "recommendation",
        "recommendations",
        "investment_advice",
        "conclusion",
        "project_conclusion",
        "real_data_path",
        "data_path",
        "artifact_path",
        "artifact",
        "report_path",
    }
    result_fields = set(BacktestEvaluationPipelineResult.model_fields)
    assert result_fields.isdisjoint(forbidden)


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
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "random",
        "numpy.random",
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
        "read_csv",
        "load_csv",
        "monthly.csv",
        "get_data.py",
        "open(",
        ".write(",
        "Path(",
        "artifact",
        "recommendation",
        "investment advice",
        "beats S&P",
        "beat S&P",
    )
    for token in forbidden_text:
        assert token not in text, f"forbidden token present: {token}"


def test_focused_regression_suite_passes() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    command = [
        "uv",
        "run",
        "pytest",
        *[
            path
            for path in FOCUSED_TEST_FILES
            if path != "tests/test_backtest_evaluation_pipeline.py"
        ],
        "-q",
    ]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
