"""Walk-forward NAV to Phase 1 benchmark-relative metrics adapter for Phase 2c-8.

This module adapts ``BacktestWalkForwardResult.nav_points`` and explicit
``BenchmarkReturnPoint`` inputs into existing ``BenchmarkRelativeMetrics``.
It does not load or fetch benchmark data, run walk-forward, render human-readable
reports, or produce investment conclusions.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from backtest_engine.walk_forward import BacktestNavPoint, BacktestWalkForwardResult
from domain.portfolio import NavSnapshot
from paper_review.metrics import (
    DEFAULT_BENCHMARK_PERIODS_PER_YEAR,
    compute_benchmark_relative_metrics,
    resolve_periods_per_year,
)
from paper_review.models import BenchmarkRelativeMetrics, BenchmarkReturnPoint

BENCHMARK_ADAPTER_POLICY_V1 = "walk_forward_nav_to_benchmark_relative_metrics.v1"
BENCHMARK_ADAPTER_POLICY_V2 = (
    "walk_forward_nav_to_benchmark_relative_metrics.frequency_aware.v2"
)


class BacktestBenchmarkRelativeResult(BaseModel):
    """Immutable benchmark-relative metrics adapted from walk-forward NAV."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_adapter_policy: Literal[
        "walk_forward_nav_to_benchmark_relative_metrics.v1",
        "walk_forward_nav_to_benchmark_relative_metrics.frequency_aware.v2",
    ]
    walk_forward_result: BacktestWalkForwardResult
    benchmark_points: tuple[BenchmarkReturnPoint, ...]
    common_dates: tuple[date, ...]
    metrics: BenchmarkRelativeMetrics

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if not self.benchmark_points:
            raise ValueError("benchmark_points must not be empty.")
        if not self.common_dates:
            raise ValueError("common_dates must not be empty.")
        for previous, current in zip(self.common_dates, self.common_dates[1:], strict=False):
            if previous >= current:
                raise ValueError("common_dates must be strictly increasing.")
        if len(self.common_dates) != self.metrics.aligned_observation_count:
            raise ValueError(
                "common_dates length must equal metrics.aligned_observation_count."
            )
        return self


def _assert_no_duplicate_calendar_dates(
    calendar_dates: Iterable[date],
    *,
    series_label: str,
) -> None:
    """동일 calendar date 중복 관측치가 있으면 실패한다."""
    seen: set[date] = set()
    for item in calendar_dates:
        if item in seen:
            raise ValueError(
                f"duplicate {series_label} calendar date: {item.isoformat()}"
            )
        seen.add(item)


def _nav_snapshots_from_walk_forward_nav_points(
    nav_points: tuple[BacktestNavPoint, ...],
) -> tuple[NavSnapshot, ...]:
    """BacktestNavPoint를 Phase 1 metric 함수가 기대하는 NavSnapshot으로 변환한다."""
    snapshots: list[NavSnapshot] = []
    for index, nav_point in enumerate(nav_points):
        invested_krw = nav_point.portfolio_value_krw - nav_point.cash_krw
        if invested_krw < 0:
            raise ValueError(
                "portfolio_value_krw must be >= cash_krw for NavSnapshot conversion."
            )
        snapshots.append(
            NavSnapshot(
                snapshot_id=f"backtest-nav-{index}-{nav_point.as_of.isoformat()}",
                as_of=nav_point.as_of,
                total_nav_krw=nav_point.portfolio_value_krw,
                cash_krw=nav_point.cash_krw,
                invested_krw=invested_krw,
            )
        )
    return tuple(snapshots)


def _common_calendar_dates(
    *,
    strategy_dates: set[date],
    benchmark_dates: set[date],
) -> tuple[date, ...]:
    """전략·벤치마크 calendar date 교집합을 strictly increasing tuple로 반환한다."""
    return tuple(sorted(strategy_dates & benchmark_dates))


def compute_walk_forward_benchmark_relative_metrics(
    *,
    walk_forward_result: BacktestWalkForwardResult,
    benchmark_points: Iterable[BenchmarkReturnPoint],
    periods_per_year: Decimal | int | str = DEFAULT_BENCHMARK_PERIODS_PER_YEAR,
) -> BacktestBenchmarkRelativeResult:
    """Walk-forward NAV와 explicit benchmark points를 Phase 1 metric 함수로 연결한다."""
    resolved_periods_per_year = resolve_periods_per_year(periods_per_year)
    materialized_benchmark_points = tuple(benchmark_points)
    if not materialized_benchmark_points:
        raise ValueError("benchmark_points must not be empty.")

    nav_points = walk_forward_result.nav_points
    if not nav_points:
        raise ValueError("walk_forward_result.nav_points must not be empty.")

    strategy_dates_list = [point.as_of.date() for point in nav_points]
    benchmark_dates_list = [point.as_of.date() for point in materialized_benchmark_points]

    _assert_no_duplicate_calendar_dates(strategy_dates_list, series_label="strategy NAV")
    _assert_no_duplicate_calendar_dates(
        benchmark_dates_list,
        series_label="benchmark",
    )

    common_dates = _common_calendar_dates(
        strategy_dates=set(strategy_dates_list),
        benchmark_dates=set(benchmark_dates_list),
    )
    if len(common_dates) < 2:
        raise ValueError(
            "at least 2 common calendar dates are required for benchmark-relative metrics."
        )

    nav_snapshots = _nav_snapshots_from_walk_forward_nav_points(nav_points)
    metrics = compute_benchmark_relative_metrics(
        nav_snapshots,
        materialized_benchmark_points,
        periods_per_year=resolved_periods_per_year,
    )

    if len(common_dates) != metrics.aligned_observation_count:
        raise ValueError(
            "common_dates length must equal metrics.aligned_observation_count."
        )

    adapter_policy = (
        BENCHMARK_ADAPTER_POLICY_V1
        if resolved_periods_per_year == DEFAULT_BENCHMARK_PERIODS_PER_YEAR
        else BENCHMARK_ADAPTER_POLICY_V2
    )

    return BacktestBenchmarkRelativeResult(
        benchmark_adapter_policy=adapter_policy,
        walk_forward_result=walk_forward_result,
        benchmark_points=materialized_benchmark_points,
        common_dates=common_dates,
        metrics=metrics,
    )
