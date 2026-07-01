from __future__ import annotations

import inspect
import math
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain.portfolio import NavSnapshot
from paper_review.metrics import compute_benchmark_relative_metrics
from paper_review.models import BenchmarkReturnPoint


BASE_TIME = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)


def _nav(day_offset: int, nav: str, *, hour: int = 15, suffix: str | None = None) -> NavSnapshot:
    as_of = BASE_TIME.replace(hour=hour) + timedelta(days=day_offset)
    total_nav = Decimal(nav)
    return NavSnapshot(
        snapshot_id=f"nav-{day_offset}-{hour}-{suffix or nav}",
        as_of=as_of,
        total_nav_krw=total_nav,
        cash_krw=Decimal("0"),
        invested_krw=total_nav,
    )


def _benchmark(day_offset: int, value: str, *, hour: int = 15) -> BenchmarkReturnPoint:
    return BenchmarkReturnPoint(
        as_of=BASE_TIME.replace(hour=hour) + timedelta(days=day_offset),
        total_return_index_value=Decimal(value),
    )


def _assert_decimal_close(
    actual: Decimal | None,
    expected: Decimal,
    *,
    tolerance: Decimal = Decimal("1E-12"),
) -> None:
    assert actual is not None
    assert abs(actual - expected) <= tolerance


def test_benchmark_return_point_accepts_positive_values() -> None:
    point = BenchmarkReturnPoint(
        as_of="2026-01-01T15:00:00+00:00",
        total_return_index_value="100.25",
    )

    assert point.as_of == BASE_TIME
    assert point.total_return_index_value == Decimal("100.25")


@pytest.mark.parametrize("value", ["0", "-1"])
def test_benchmark_return_point_rejects_non_positive_values(value: str) -> None:
    with pytest.raises(ValidationError, match="total_return_index_value must be greater than 0"):
        BenchmarkReturnPoint(as_of=BASE_TIME, total_return_index_value=value)


def test_benchmark_return_point_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        BenchmarkReturnPoint(
            as_of=BASE_TIME,
            total_return_index_value="100",
            source="synthetic",
        )


def test_aligns_by_common_calendar_date_and_uses_latest_same_day_observation() -> None:
    navs = (
        _nav(2, "130"),
        _nav(1, "105", hour=9, suffix="early"),
        _nav(0, "100"),
        _nav(1, "110", hour=15, suffix="late"),
    )
    benchmarks = (
        _benchmark(3, "130"),
        _benchmark(1, "105"),
        _benchmark(0, "100"),
    )

    metrics = compute_benchmark_relative_metrics(navs, benchmarks)

    assert metrics.aligned_observation_count == 2
    assert metrics.return_observation_count == 1
    assert metrics.benchmark_observation_count == 3
    assert metrics.bot_total_return_percent == Decimal("10.0")
    assert metrics.benchmark_total_return_percent == Decimal("5.00")
    assert metrics.excess_return_percent == Decimal("5.00")


def test_basic_total_and_excess_return() -> None:
    metrics = compute_benchmark_relative_metrics(
        (_nav(0, "100"), _nav(1, "110")),
        (_benchmark(0, "100"), _benchmark(1, "105")),
    )

    assert metrics.bot_total_return_percent == Decimal("10.0")
    assert metrics.benchmark_total_return_percent == Decimal("5.00")
    assert metrics.excess_return_percent == Decimal("5.00")


def test_relative_drawdown_uses_relative_wealth_curve() -> None:
    metrics = compute_benchmark_relative_metrics(
        (_nav(0, "100"), _nav(1, "120"), _nav(2, "108")),
        (_benchmark(0, "100"), _benchmark(1, "110"), _benchmark(2, "110")),
    )

    _assert_decimal_close(metrics.relative_drawdown_percent, Decimal("-10.0"))


def test_tracking_error_information_ratio_beta_and_capture_ratios() -> None:
    navs = (
        _nav(0, "100"),
        _nav(1, "110"),
        _nav(2, "104.5"),
        _nav(3, "117.04"),
    )
    benchmarks = (
        _benchmark(0, "100"),
        _benchmark(1, "104"),
        _benchmark(2, "101.92"),
        _benchmark(3, "110.0736"),
    )

    metrics = compute_benchmark_relative_metrics(navs, benchmarks)

    bot_returns = [0.10, -0.05, 0.12]
    benchmark_returns = [0.04, -0.02, 0.08]
    excess_returns = [bot - benchmark for bot, benchmark in zip(bot_returns, benchmark_returns)]
    excess_mean = sum(excess_returns) / len(excess_returns)
    excess_stddev = math.sqrt(
        sum((item - excess_mean) ** 2 for item in excess_returns) / len(excess_returns)
    )
    benchmark_mean = sum(benchmark_returns) / len(benchmark_returns)
    benchmark_variance = (
        sum((item - benchmark_mean) ** 2 for item in benchmark_returns)
        / len(benchmark_returns)
    )
    bot_mean = sum(bot_returns) / len(bot_returns)
    covariance = (
        sum(
            (bot - bot_mean) * (benchmark - benchmark_mean)
            for bot, benchmark in zip(bot_returns, benchmark_returns)
        )
        / len(bot_returns)
    )

    _assert_decimal_close(
        metrics.tracking_error_daily_percent,
        Decimal(str(excess_stddev)) * Decimal("100"),
    )
    _assert_decimal_close(
        metrics.information_ratio_annualized,
        Decimal(str((excess_mean / excess_stddev) * math.sqrt(252))),
    )
    _assert_decimal_close(
        metrics.beta_to_benchmark,
        Decimal(str(covariance)) / Decimal(str(benchmark_variance)),
    )
    assert metrics.up_capture_percent is not None
    assert metrics.up_capture_percent > Decimal("100")
    assert metrics.down_capture_percent == Decimal("250.0")


def test_no_navs_returns_insufficient_alignment_warnings() -> None:
    metrics = compute_benchmark_relative_metrics((), (_benchmark(0, "100"),))

    assert metrics.aligned_observation_count == 0
    assert metrics.return_observation_count == 0
    assert metrics.benchmark_observation_count == 1
    assert metrics.bot_total_return_percent == Decimal("0")
    assert metrics.relative_drawdown_percent is None
    assert "insufficient_aligned_observations" in metrics.warnings
    assert "benchmark_observation_count=1" in metrics.warnings
    assert "no_common_observation_dates" in metrics.warnings


def test_one_aligned_observation_returns_insufficient_alignment_warnings() -> None:
    metrics = compute_benchmark_relative_metrics((_nav(0, "100"),), (_benchmark(0, "100"),))

    assert metrics.aligned_observation_count == 1
    assert metrics.return_observation_count == 0
    assert metrics.tracking_error_daily_percent is None
    assert metrics.beta_to_benchmark is None
    assert "insufficient_aligned_observations" in metrics.warnings


def test_no_common_dates_returns_insufficient_alignment_warnings() -> None:
    metrics = compute_benchmark_relative_metrics((_nav(0, "100"),), (_benchmark(1, "100"),))

    assert metrics.aligned_observation_count == 0
    assert "no_common_observation_dates" in metrics.warnings


def test_zero_benchmark_variance_returns_beta_none_with_warning() -> None:
    metrics = compute_benchmark_relative_metrics(
        (_nav(0, "100"), _nav(1, "110"), _nav(2, "120")),
        (_benchmark(0, "100"), _benchmark(1, "100"), _benchmark(2, "100")),
    )

    assert metrics.beta_to_benchmark is None
    assert "zero_benchmark_return_variance" in metrics.warnings


def test_missing_up_or_down_benchmark_periods_return_none_with_warning() -> None:
    all_down = compute_benchmark_relative_metrics(
        (_nav(0, "100"), _nav(1, "99"), _nav(2, "98")),
        (_benchmark(0, "100"), _benchmark(1, "99"), _benchmark(2, "98")),
    )
    all_up = compute_benchmark_relative_metrics(
        (_nav(0, "100"), _nav(1, "101"), _nav(2, "102")),
        (_benchmark(0, "100"), _benchmark(1, "101"), _benchmark(2, "102")),
    )

    assert all_down.up_capture_percent is None
    assert "no_benchmark_up_periods" in all_down.warnings
    assert all_up.down_capture_percent is None
    assert "no_benchmark_down_periods" in all_up.warnings


def test_benchmark_relative_metrics_function_has_no_data_fetch_imports() -> None:
    source = inspect.getsource(compute_benchmark_relative_metrics).lower()

    for forbidden in ("yfinance", "fred", "requests", "urlopen", "socket"):
        assert forbidden not in source
