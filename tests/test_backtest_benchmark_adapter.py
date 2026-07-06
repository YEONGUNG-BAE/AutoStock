from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
from collections.abc import Iterable
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
    compute_walk_forward_benchmark_relative_metrics,
)
from backtest_engine import (  # noqa: E402
    COST_MODEL_V1,
    BacktestCostModel,
    BacktestHolding,
    BacktestNavPoint,
    BacktestPeriodSpec,
    BacktestPortfolioState,
    BacktestWalkForwardResult,
    RollingLongMaAssetConfig,
    run_explicit_schedule_rules_walk_forward_nav,
)
from backtest_data import (  # noqa: E402
    BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    InMemoryDateIdSourceReader,
)
from domain import DateId, DateIdSourceRecord, FactType  # noqa: E402
from paper_review.metrics import compute_benchmark_relative_metrics  # noqa: E402
from paper_review.models import BenchmarkReturnPoint  # noqa: E402

BASE_TIME = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
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
    Path(__file__).resolve().parents[1] / "src" / "backtest_engine" / "benchmark_adapter.py"
)

FOCUSED_TEST_FILES = (
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


def _run_walk_forward() -> BacktestWalkForwardResult:
    return run_explicit_schedule_rules_walk_forward_nav(
        _two_period_records(),
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )


def _benchmark_at(as_of: datetime, value: str) -> BenchmarkReturnPoint:
    return BenchmarkReturnPoint(
        as_of=as_of,
        total_return_index_value=Decimal(value),
    )


def _benchmarks_for_nav_points(
    nav_points: tuple[BacktestNavPoint, ...],
    values: tuple[str, ...],
) -> tuple[BenchmarkReturnPoint, ...]:
    return tuple(
        _benchmark_at(nav_point.as_of, value)
        for nav_point, value in zip(nav_points, values, strict=True)
    )


def _invalid_walk_forward_shell(
    nav_points: tuple[BacktestNavPoint, ...],
) -> BacktestWalkForwardResult:
    """duplicate-date 등 adapter 선행 검증 테스트용 비검증 shell."""
    return BacktestWalkForwardResult.model_construct(
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


def _nav_point(day_offset: int, portfolio_value: str, *, hour: int = 15) -> BacktestNavPoint:
    as_of = BASE_TIME.replace(hour=hour) + timedelta(days=day_offset)
    value = Decimal(portfolio_value)
    return BacktestNavPoint(
        as_of=as_of,
        portfolio_value_krw=value,
        cash_krw=Decimal("0"),
        total_cost_krw=Decimal("0"),
    )


def _benchmark(day_offset: int, value: str, *, hour: int = 15) -> BenchmarkReturnPoint:
    return BenchmarkReturnPoint(
        as_of=BASE_TIME.replace(hour=hour) + timedelta(days=day_offset),
        total_return_index_value=Decimal(value),
    )


def test_builds_benchmark_relative_result_from_synthetic_inputs() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "110"))

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert isinstance(result, BacktestBenchmarkRelativeResult)
    assert result.benchmark_adapter_policy == BENCHMARK_ADAPTER_POLICY_V1
    assert result.metrics.aligned_observation_count == len(walk_forward.nav_points)


def test_uses_existing_compute_benchmark_relative_metrics() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "105"))

    with patch(
        "backtest_engine.benchmark_adapter.compute_benchmark_relative_metrics",
        wraps=compute_benchmark_relative_metrics,
    ) as mocked:
        result = compute_walk_forward_benchmark_relative_metrics(
            walk_forward_result=walk_forward,
            benchmark_points=benchmarks,
        )

    mocked.assert_called_once()
    expected_bot_return = (
        (walk_forward.nav_points[-1].portfolio_value_krw / walk_forward.nav_points[0].portfolio_value_krw)
        - Decimal("1")
    ) * Decimal("100")
    assert result.metrics.bot_total_return_percent == expected_bot_return


def test_uses_nav_point_as_of_date_for_strategy_date() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "105"))

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert result.common_dates == tuple(
        nav_point.as_of.date() for nav_point in walk_forward.nav_points
    )


def test_uses_nav_point_portfolio_value_krw_for_strategy_level() -> None:
    walk_forward = _run_walk_forward()
    start_nav = walk_forward.nav_points[0].portfolio_value_krw
    end_nav = walk_forward.nav_points[-1].portfolio_value_krw
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "120"))

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert result.metrics.bot_total_return_percent == (
        (end_nav / start_nav) - Decimal("1")
    ) * Decimal("100")
    assert result.metrics.benchmark_total_return_percent == Decimal("20.00")


def test_aligns_only_common_dates() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = (
        _benchmark_at(walk_forward.nav_points[0].as_of, "100"),
        _benchmark_at(
            walk_forward.nav_points[0].as_of + timedelta(days=15),
            "105",
        ),
        _benchmark_at(walk_forward.nav_points[1].as_of, "110"),
    )

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert result.common_dates == tuple(
        nav_point.as_of.date() for nav_point in walk_forward.nav_points
    )
    assert result.metrics.aligned_observation_count == len(walk_forward.nav_points)


def test_does_not_forward_fill_missing_dates() -> None:
    walk_forward = _run_walk_forward()
    middle_as_of = walk_forward.nav_points[0].as_of + timedelta(days=15)
    benchmarks = (
        _benchmark_at(walk_forward.nav_points[0].as_of, "100"),
        _benchmark_at(middle_as_of, "105"),
        _benchmark_at(walk_forward.nav_points[1].as_of, "110"),
    )

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert len(result.common_dates) == 2
    assert result.metrics.return_observation_count == 1
    assert middle_as_of.date() not in result.common_dates


def test_does_not_back_fill_missing_dates() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "110"))

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert result.common_dates[0] == walk_forward.nav_points[0].as_of.date()
    assert all(item in result.common_dates for item in (point.as_of.date() for point in walk_forward.nav_points))


def test_does_not_interpolate_missing_dates() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "130"))

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert result.metrics.aligned_observation_count == len(walk_forward.nav_points)
    expected_bot_return = (
        (
            walk_forward.nav_points[-1].portfolio_value_krw
            / walk_forward.nav_points[0].portfolio_value_krw
        )
        - Decimal("1")
    ) * Decimal("100")
    assert result.metrics.bot_total_return_percent == expected_bot_return


def test_raises_when_fewer_than_two_common_dates_exist() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = (
        _benchmark_at(walk_forward.nav_points[0].as_of + timedelta(days=30), "105"),
        _benchmark_at(walk_forward.nav_points[0].as_of + timedelta(days=60), "110"),
    )

    with pytest.raises(ValueError, match="at least 2 common calendar dates"):
        compute_walk_forward_benchmark_relative_metrics(
            walk_forward_result=walk_forward,
            benchmark_points=benchmarks,
        )


def test_raises_on_duplicate_strategy_nav_dates() -> None:
    walk_forward = _invalid_walk_forward_shell(
        (
            _nav_point(0, "100", hour=9),
            _nav_point(0, "110", hour=15),
            _nav_point(1, "120"),
        )
    )
    benchmarks = (_benchmark(0, "100"), _benchmark(1, "105"))

    with pytest.raises(ValueError, match="duplicate strategy NAV calendar date"):
        compute_walk_forward_benchmark_relative_metrics(
            walk_forward_result=walk_forward,
            benchmark_points=benchmarks,
        )


def test_raises_on_duplicate_benchmark_dates() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = (
        _benchmark_at(walk_forward.nav_points[0].as_of.replace(hour=9), "100"),
        _benchmark_at(walk_forward.nav_points[0].as_of.replace(hour=15), "101"),
        _benchmark_at(walk_forward.nav_points[1].as_of, "105"),
    )

    with pytest.raises(ValueError, match="duplicate benchmark calendar date"):
        compute_walk_forward_benchmark_relative_metrics(
            walk_forward_result=walk_forward,
            benchmark_points=benchmarks,
        )


def test_materializes_benchmark_iterable_once() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "105"))
    iteration_count = 0

    def _generator() -> Iterable[BenchmarkReturnPoint]:
        nonlocal iteration_count
        iteration_count += 1
        yield from benchmarks

    compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=_generator(),
    )

    assert iteration_count == 1


def test_stores_strictly_increasing_common_dates() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "105"))

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert result.common_dates == tuple(sorted(result.common_dates))
    assert all(
        earlier < later
        for earlier, later in zip(result.common_dates, result.common_dates[1:], strict=False)
    )


def test_preserves_original_walk_forward_result() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "105"))

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert result.walk_forward_result is walk_forward


def test_preserves_benchmark_points_tuple() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "105"))

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert result.benchmark_points == benchmarks


def test_result_model_is_frozen_and_forbids_extra_fields() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "105"))
    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    with pytest.raises(ValidationError):
        result.benchmark_adapter_policy = "other"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        BacktestBenchmarkRelativeResult(
            benchmark_adapter_policy=BENCHMARK_ADAPTER_POLICY_V1,
            walk_forward_result=walk_forward,
            benchmark_points=benchmarks,
            common_dates=result.common_dates,
            metrics=result.metrics,
            report_markdown="forbidden",  # type: ignore[call-arg]
        )


def test_result_has_no_report_recommendation_or_conclusion_fields() -> None:
    forbidden = {
        "report",
        "report_markdown",
        "markdown",
        "recommendation",
        "conclusion",
        "investment_advice",
        "beats_sp500",
        "beat_sp500",
    }
    result_fields = set(BacktestBenchmarkRelativeResult.model_fields)
    assert result_fields.isdisjoint(forbidden)


def test_module_does_not_run_walk_forward() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "run_explicit_schedule_rules_walk_forward_nav",
        "run_single_period_rules_rebalance_step",
    )
    for token in forbidden:
        assert token not in text, f"forbidden token present: {token}"


def test_module_does_not_fetch_or_read_real_data() -> None:
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
        "render_benchmark_relative_metrics_markdown",
        "markdown",
        "recommend",
        "investment advice",
        "beats S&P",
        "beat S&P",
    )
    for token in forbidden_text:
        assert token not in text, f"forbidden token present: {token}"


def test_common_dates_match_metrics_aligned_observation_count() -> None:
    walk_forward = _run_walk_forward()
    benchmarks = _benchmarks_for_nav_points(walk_forward.nav_points, ("100", "105"))

    result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward,
        benchmark_points=benchmarks,
    )

    assert len(result.common_dates) == result.metrics.aligned_observation_count
    assert all(isinstance(item, date) for item in result.common_dates)


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
            if path != "tests/test_backtest_benchmark_adapter.py"
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


def test_adapter_source_does_not_define_benchmark_relative_math() -> None:
    source = inspect.getsource(
        compute_walk_forward_benchmark_relative_metrics
    ).lower()
    assert "information_ratio" not in source
    assert "tracking_error" not in source
    assert "beta_to_benchmark" not in source
