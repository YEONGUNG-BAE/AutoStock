from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine.benchmark_adapter import (  # noqa: E402
    BENCHMARK_ADAPTER_POLICY_V1,
    BacktestBenchmarkRelativeResult,
)
from backtest_engine.report_bundle import (  # noqa: E402
    BACKTEST_REPORT_BUNDLE_POLICY_V1,
    BacktestEvaluationReportBundle,
    render_backtest_evaluation_report_bundle,
)
from backtest_engine.walk_forward import BacktestNavPoint, BacktestWalkForwardResult  # noqa: E402
from paper_review.models import BenchmarkRelativeMetrics, BenchmarkReturnPoint  # noqa: E402
from paper_review.report import render_benchmark_relative_metrics_markdown  # noqa: E402

BASE_TIME = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "backtest_engine" / "report_bundle.py"

FOCUSED_TEST_FILES = (
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


def _metrics(*, warnings: tuple[str, ...] = ()) -> BenchmarkRelativeMetrics:
    return BenchmarkRelativeMetrics(
        bot_total_return_percent=Decimal("5"),
        benchmark_total_return_percent=Decimal("2"),
        excess_return_percent=Decimal("3"),
        relative_drawdown_percent=Decimal("-1.25"),
        tracking_error_daily_percent=Decimal("0.50"),
        information_ratio_annualized=Decimal("1.75"),
        up_capture_percent=Decimal("125"),
        down_capture_percent=Decimal("81.81818181818181818181818182"),
        beta_to_benchmark=Decimal("0.90"),
        aligned_observation_count=3,
        return_observation_count=2,
        benchmark_observation_count=3,
        warnings=warnings,
    )


def _nav_point(day_offset: int, portfolio_value: str) -> BacktestNavPoint:
    value = Decimal(portfolio_value)
    return BacktestNavPoint(
        as_of=BASE_TIME + timedelta(days=day_offset),
        portfolio_value_krw=value,
        cash_krw=Decimal("0"),
        total_cost_krw=Decimal("0"),
    )


def _benchmark(day_offset: int, value: str) -> BenchmarkReturnPoint:
    return BenchmarkReturnPoint(
        as_of=BASE_TIME + timedelta(days=day_offset),
        total_return_index_value=Decimal(value),
    )


def _synthetic_benchmark_relative_result() -> BacktestBenchmarkRelativeResult:
    """walk-forward 실행 없이 adapter 결과 shell을 합성한다."""
    nav_points = (
        _nav_point(0, "100"),
        _nav_point(1, "110"),
        _nav_point(2, "105"),
    )
    benchmark_points = (
        _benchmark(0, "100"),
        _benchmark(1, "104"),
        _benchmark(2, "102"),
    )
    common_dates = (
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
    )
    walk_forward = BacktestWalkForwardResult.model_construct(
        walk_forward_policy="explicit_schedule_rules_walk_forward_nav.v1",
        initial_portfolio_state=object(),
        period_specs=(),
        steps=(),
        nav_points=nav_points,
        final_portfolio_state=object(),
        total_fee_krw=Decimal("0"),
        total_tax_krw=Decimal("0"),
        total_fx_spread_krw=Decimal("0"),
        total_cost_krw=Decimal("0"),
    )
    return BacktestBenchmarkRelativeResult.model_construct(
        benchmark_adapter_policy=BENCHMARK_ADAPTER_POLICY_V1,
        walk_forward_result=walk_forward,
        benchmark_points=benchmark_points,
        common_dates=common_dates,
        metrics=_metrics(),
    )


def test_builds_report_bundle_from_synthetic_benchmark_relative_result() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()

    bundle = render_backtest_evaluation_report_bundle(
        benchmark_relative_result=benchmark_relative_result,
    )

    assert isinstance(bundle, BacktestEvaluationReportBundle)
    assert bundle.report_bundle_policy == BACKTEST_REPORT_BUNDLE_POLICY_V1


def test_calls_existing_renderer_exactly_once_with_metrics() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()

    with patch(
        "backtest_engine.report_bundle.render_benchmark_relative_metrics_markdown",
        wraps=render_benchmark_relative_metrics_markdown,
    ) as mocked:
        bundle = render_backtest_evaluation_report_bundle(
            benchmark_relative_result=benchmark_relative_result,
        )

    mocked.assert_called_once_with(benchmark_relative_result.metrics)


def test_preserves_original_benchmark_relative_result() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()

    bundle = render_backtest_evaluation_report_bundle(
        benchmark_relative_result=benchmark_relative_result,
    )

    assert bundle.benchmark_relative_result is benchmark_relative_result


def test_stores_non_empty_markdown_report() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()

    bundle = render_backtest_evaluation_report_bundle(
        benchmark_relative_result=benchmark_relative_result,
    )

    assert isinstance(bundle.markdown_report, str)
    assert bundle.markdown_report.strip()


def test_markdown_contains_existing_renderer_fields() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()

    bundle = render_backtest_evaluation_report_bundle(
        benchmark_relative_result=benchmark_relative_result,
    )
    markdown = bundle.markdown_report

    assert markdown.startswith("# Benchmark-relative performance\n")
    assert "- Benchmark: S&P 500 total return (KRW-unhedged)" in markdown
    assert "- Aligned observations: 3" in markdown
    assert "- Return observations: 2" in markdown
    assert "- Benchmark observations supplied: 3" in markdown
    assert "- Bot total return (%): 5" in markdown
    assert "- Benchmark total return (%): 2" in markdown
    assert "- Excess return (%): 3" in markdown
    assert "- Relative drawdown (%): -1.25" in markdown
    assert "- Tracking error (%): 0.50" in markdown
    assert "- Information ratio annualized: 1.75" in markdown
    assert "- Beta to benchmark: 0.90" in markdown
    assert "- Up-capture (%): 125" in markdown
    assert "- Down-capture (%): 81.81818181818181818181818182" in markdown
    assert "## Warnings\n\n- None\n" in markdown
    assert "not a historical backtest" in markdown.lower()


def test_result_model_is_frozen_and_forbids_extra_fields() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()
    bundle = render_backtest_evaluation_report_bundle(
        benchmark_relative_result=benchmark_relative_result,
    )

    with pytest.raises(ValidationError):
        bundle.report_bundle_policy = "other"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        BacktestEvaluationReportBundle(
            report_bundle_policy=BACKTEST_REPORT_BUNDLE_POLICY_V1,
            benchmark_relative_result=benchmark_relative_result,
            markdown_report=bundle.markdown_report,
            artifact_path="/tmp/forbidden",  # type: ignore[call-arg]
        )


def test_result_has_no_forbidden_fields() -> None:
    forbidden = {
        "recommendation",
        "recommendations",
        "investment_advice",
        "conclusion",
        "real_data_path",
        "data_path",
        "artifact_path",
        "artifact",
        "report_path",
    }
    result_fields = set(BacktestEvaluationReportBundle.model_fields)
    assert result_fields.isdisjoint(forbidden)


def test_function_does_not_compute_benchmark_relative_metrics() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()

    with patch(
        "paper_review.metrics.compute_benchmark_relative_metrics",
    ) as mocked_metrics:
        render_backtest_evaluation_report_bundle(
            benchmark_relative_result=benchmark_relative_result,
        )

    mocked_metrics.assert_not_called()


def test_function_does_not_call_walk_forward_adapter() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()

    with patch(
        "backtest_engine.benchmark_adapter.compute_walk_forward_benchmark_relative_metrics",
    ) as mocked_adapter:
        render_backtest_evaluation_report_bundle(
            benchmark_relative_result=benchmark_relative_result,
        )

    mocked_adapter.assert_not_called()


def test_function_does_not_run_walk_forward() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()

    with patch(
        "backtest_engine.walk_forward.run_explicit_schedule_rules_walk_forward_nav",
    ) as mocked_walk_forward:
        render_backtest_evaluation_report_bundle(
            benchmark_relative_result=benchmark_relative_result,
        )

    mocked_walk_forward.assert_not_called()


def test_function_does_not_fetch_or_read_real_data() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()

    with patch("builtins.open") as mocked_open:
        render_backtest_evaluation_report_bundle(
            benchmark_relative_result=benchmark_relative_result,
        )

    mocked_open.assert_not_called()


def test_function_does_not_write_files_or_artifacts() -> None:
    benchmark_relative_result = _synthetic_benchmark_relative_result()

    with patch("pathlib.Path.write_text") as mocked_write_text:
        render_backtest_evaluation_report_bundle(
            benchmark_relative_result=benchmark_relative_result,
        )

    mocked_write_text.assert_not_called()


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
        "run_explicit_schedule_rules_walk_forward_nav",
        "run_single_period_rules_rebalance_step",
        "compute_walk_forward_benchmark_relative_metrics",
        "compute_benchmark_relative_metrics",
        "open(",
        ".write(",
        "Path(",
        "artifact",
        "recommendation",
        "investment advice",
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
            if path != "tests/test_backtest_report_bundle.py"
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
