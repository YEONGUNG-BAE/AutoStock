from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from config.settings import ExecutionMode
from domain.enums import OrderSide
from domain.order import Fill, OrderIntent
from domain.portfolio import NavSnapshot
from emergency.models import EmergencyTriggerEvent, EmergencyTriggerStatus, EmergencyTriggerType
from paper_review.models import (
    BenchmarkRelativeMetrics,
    BenchmarkReturnPoint,
    ExecutionReviewMetrics,
    PaperPerformanceMetrics,
    ReviewPeriod,
    SampleSufficiency,
)

DEFAULT_BENCHMARK_PERIODS_PER_YEAR = Decimal("252")


def resolve_periods_per_year(
    periods_per_year: Decimal | int | str = DEFAULT_BENCHMARK_PERIODS_PER_YEAR,
) -> Decimal:
    """Validate and coerce benchmark metric annualization frequency."""
    if isinstance(periods_per_year, bool):
        raise ValueError("periods_per_year must not be a bool.")
    if isinstance(periods_per_year, float):
        raise ValueError("periods_per_year must not be a float.")
    try:
        resolved = Decimal(str(periods_per_year))
    except Exception as exc:
        raise ValueError(
            "periods_per_year must be a finite positive Decimal, int, or str."
        ) from exc
    if not resolved.is_finite():
        raise ValueError("periods_per_year must be finite.")
    if resolved <= Decimal("0"):
        raise ValueError("periods_per_year must be positive.")
    return resolved


def sort_nav_snapshots(nav_snapshots: Sequence[NavSnapshot]) -> tuple[NavSnapshot, ...]:
    """as_of 기준 deterministic sort."""
    return tuple(sorted(nav_snapshots, key=lambda item: item.as_of.isoformat()))


def _compute_daily_returns(nav_snapshots: Sequence[NavSnapshot]) -> tuple[Decimal, ...]:
    """NavSnapshot daily_return_percent 또는 adjacent NAV fallback으로 daily return(%)을 계산한다."""
    if len(nav_snapshots) < 2:
        return ()

    returns: list[Decimal] = []
    sorted_snapshots = sort_nav_snapshots(nav_snapshots)

    for index in range(1, len(sorted_snapshots)):
        previous = sorted_snapshots[index - 1]
        current = sorted_snapshots[index]

        if current.daily_return_percent is not None:
            returns.append(current.daily_return_percent)
            continue

        if previous.total_nav_krw <= Decimal("0"):
            continue

        daily_return = (
            (current.total_nav_krw / previous.total_nav_krw) - Decimal("1")
        ) * Decimal("100")
        returns.append(daily_return)

    return tuple(returns)


def _compute_volatility(daily_returns: Sequence[Decimal]) -> Decimal | None:
    """daily return(%) population std dev를 반환한다."""
    if len(daily_returns) < 2:
        return None

    values = [float(item) for item in daily_returns]
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return Decimal(str(math.sqrt(variance)))


def _population_stddev_decimal(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None

    float_values = [float(item) for item in values]
    mean = sum(float_values) / len(float_values)
    variance = sum((value - mean) ** 2 for value in float_values) / len(float_values)
    return Decimal(str(math.sqrt(variance)))


def _population_variance_decimal(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None

    float_values = [float(item) for item in values]
    mean = sum(float_values) / len(float_values)
    variance = sum((value - mean) ** 2 for value in float_values) / len(float_values)
    return Decimal(str(variance))


def _population_covariance_decimal(
    left_values: Sequence[Decimal],
    right_values: Sequence[Decimal],
) -> Decimal | None:
    if len(left_values) != len(right_values) or len(left_values) < 2:
        return None

    left_floats = [float(item) for item in left_values]
    right_floats = [float(item) for item in right_values]
    left_mean = sum(left_floats) / len(left_floats)
    right_mean = sum(right_floats) / len(right_floats)
    covariance = sum(
        (left - left_mean) * (right - right_mean)
        for left, right in zip(left_floats, right_floats, strict=True)
    ) / len(left_floats)
    return Decimal(str(covariance))


def _latest_nav_by_calendar_date(nav_snapshots: Sequence[NavSnapshot]) -> dict[date, NavSnapshot]:
    """각 timestamp의 calendar date별 최신 NAV 관측치를 deterministic하게 선택한다."""
    latest_by_date: dict[date, NavSnapshot] = {}
    sorted_snapshots = sorted(
        nav_snapshots,
        key=lambda item: (item.as_of, item.snapshot_id, item.total_nav_krw),
    )
    for snapshot in sorted_snapshots:
        latest_by_date[snapshot.as_of.date()] = snapshot
    return latest_by_date


def _latest_benchmark_by_calendar_date(
    benchmark_points: Sequence[BenchmarkReturnPoint],
) -> dict[date, BenchmarkReturnPoint]:
    """각 timestamp의 calendar date별 최신 benchmark 관측치를 deterministic하게 선택한다."""
    latest_by_date: dict[date, BenchmarkReturnPoint] = {}
    sorted_points = sorted(
        benchmark_points,
        key=lambda item: (item.as_of, item.total_return_index_value),
    )
    for point in sorted_points:
        latest_by_date[point.as_of.date()] = point
    return latest_by_date


def _compound_return(returns: Sequence[Decimal]) -> Decimal:
    growth = Decimal("1")
    for item in returns:
        growth *= Decimal("1") + item
    return growth - Decimal("1")


def compute_benchmark_relative_metrics(
    nav_snapshots: Sequence[NavSnapshot],
    benchmark_points: Sequence[BenchmarkReturnPoint],
    *,
    periods_per_year: Decimal | int | str = DEFAULT_BENCHMARK_PERIODS_PER_YEAR,
) -> BenchmarkRelativeMetrics:
    """NAV와 외부 benchmark total-return 관측치를 날짜 기준으로 맞춰 성과를 계산한다."""
    resolved_periods_per_year = resolve_periods_per_year(periods_per_year)
    warnings: list[str] = []
    benchmark_observation_count = len(benchmark_points)
    nav_by_date = _latest_nav_by_calendar_date(nav_snapshots)
    benchmark_by_date = _latest_benchmark_by_calendar_date(benchmark_points)
    common_dates = tuple(sorted(set(nav_by_date) & set(benchmark_by_date)))
    aligned_observation_count = len(common_dates)
    return_observation_count = (
        aligned_observation_count - 1 if aligned_observation_count >= 2 else 0
    )

    if aligned_observation_count < 2:
        warnings.append("insufficient_aligned_observations")
        warnings.append(f"benchmark_observation_count={benchmark_observation_count}")
        if not common_dates:
            warnings.append("no_common_observation_dates")
        return BenchmarkRelativeMetrics(
            bot_total_return_percent=Decimal("0"),
            benchmark_total_return_percent=Decimal("0"),
            excess_return_percent=Decimal("0"),
            relative_drawdown_percent=None,
            tracking_error_daily_percent=None,
            information_ratio_annualized=None,
            up_capture_percent=None,
            down_capture_percent=None,
            beta_to_benchmark=None,
            aligned_observation_count=aligned_observation_count,
            return_observation_count=return_observation_count,
            benchmark_observation_count=benchmark_observation_count,
            warnings=tuple(warnings),
        )

    aligned_navs = tuple(nav_by_date[item] for item in common_dates)
    aligned_benchmarks = tuple(benchmark_by_date[item] for item in common_dates)

    for snapshot in aligned_navs:
        if snapshot.total_nav_krw <= Decimal("0"):
            raise ValueError("aligned NavSnapshot total_nav_krw must be greater than 0.")

    start_nav = aligned_navs[0].total_nav_krw
    end_nav = aligned_navs[-1].total_nav_krw
    start_benchmark = aligned_benchmarks[0].total_return_index_value
    end_benchmark = aligned_benchmarks[-1].total_return_index_value

    bot_total_return = (end_nav / start_nav) - Decimal("1")
    benchmark_total_return = (end_benchmark / start_benchmark) - Decimal("1")
    excess_return = bot_total_return - benchmark_total_return

    running_peak = Decimal("1")
    relative_drawdown = Decimal("0")
    for nav, benchmark in zip(aligned_navs, aligned_benchmarks, strict=True):
        bot_wealth = nav.total_nav_krw / start_nav
        benchmark_wealth = benchmark.total_return_index_value / start_benchmark
        relative_wealth = bot_wealth / benchmark_wealth
        if relative_wealth > running_peak:
            running_peak = relative_wealth
        drawdown = (relative_wealth / running_peak) - Decimal("1")
        if drawdown < relative_drawdown:
            relative_drawdown = drawdown

    bot_period_returns: list[Decimal] = []
    benchmark_period_returns: list[Decimal] = []
    excess_period_returns: list[Decimal] = []
    for index in range(1, aligned_observation_count):
        previous_nav = aligned_navs[index - 1].total_nav_krw
        current_nav = aligned_navs[index].total_nav_krw
        previous_benchmark = aligned_benchmarks[index - 1].total_return_index_value
        current_benchmark = aligned_benchmarks[index].total_return_index_value

        bot_period_return = (current_nav / previous_nav) - Decimal("1")
        benchmark_period_return = (current_benchmark / previous_benchmark) - Decimal("1")
        bot_period_returns.append(bot_period_return)
        benchmark_period_returns.append(benchmark_period_return)
        excess_period_returns.append(bot_period_return - benchmark_period_return)

    tracking_error: Decimal | None = None
    information_ratio: Decimal | None = None
    excess_stddev = _population_stddev_decimal(excess_period_returns)
    if excess_stddev is None:
        warnings.append("insufficient_return_observations")
    else:
        tracking_error = excess_stddev * Decimal("100")
        if excess_stddev == Decimal("0"):
            warnings.append("zero_excess_return_variance")
        else:
            mean_excess = sum(excess_period_returns, start=Decimal("0")) / Decimal(
                len(excess_period_returns)
            )
            information_ratio = Decimal(
                str(
                    (float(mean_excess) / float(excess_stddev))
                    * math.sqrt(float(resolved_periods_per_year))
                )
            )

    benchmark_up_indexes = [
        index for index, item in enumerate(benchmark_period_returns) if item > Decimal("0")
    ]
    benchmark_down_indexes = [
        index for index, item in enumerate(benchmark_period_returns) if item < Decimal("0")
    ]

    up_capture: Decimal | None = None
    if benchmark_up_indexes:
        bot_up_compound = _compound_return(
            tuple(bot_period_returns[index] for index in benchmark_up_indexes)
        )
        benchmark_up_compound = _compound_return(
            tuple(benchmark_period_returns[index] for index in benchmark_up_indexes)
        )
        if benchmark_up_compound == Decimal("0"):
            warnings.append("zero_benchmark_up_capture_denominator")
        else:
            up_capture = (bot_up_compound / benchmark_up_compound) * Decimal("100")
    else:
        warnings.append("no_benchmark_up_periods")

    down_capture: Decimal | None = None
    if benchmark_down_indexes:
        bot_down_compound = _compound_return(
            tuple(bot_period_returns[index] for index in benchmark_down_indexes)
        )
        benchmark_down_compound = _compound_return(
            tuple(benchmark_period_returns[index] for index in benchmark_down_indexes)
        )
        if benchmark_down_compound == Decimal("0"):
            warnings.append("zero_benchmark_down_capture_denominator")
        else:
            down_capture = (bot_down_compound / benchmark_down_compound) * Decimal("100")
    else:
        warnings.append("no_benchmark_down_periods")

    beta: Decimal | None = None
    benchmark_variance = _population_variance_decimal(benchmark_period_returns)
    covariance = _population_covariance_decimal(bot_period_returns, benchmark_period_returns)
    if benchmark_variance is None or covariance is None:
        if "insufficient_return_observations" not in warnings:
            warnings.append("insufficient_return_observations")
    elif benchmark_variance == Decimal("0"):
        warnings.append("zero_benchmark_return_variance")
    else:
        beta = covariance / benchmark_variance

    return BenchmarkRelativeMetrics(
        bot_total_return_percent=bot_total_return * Decimal("100"),
        benchmark_total_return_percent=benchmark_total_return * Decimal("100"),
        excess_return_percent=excess_return * Decimal("100"),
        relative_drawdown_percent=relative_drawdown * Decimal("100"),
        tracking_error_daily_percent=tracking_error,
        information_ratio_annualized=information_ratio,
        up_capture_percent=up_capture,
        down_capture_percent=down_capture,
        beta_to_benchmark=beta,
        aligned_observation_count=aligned_observation_count,
        return_observation_count=return_observation_count,
        benchmark_observation_count=benchmark_observation_count,
        warnings=tuple(warnings),
    )


def compute_paper_performance_metrics(
    nav_snapshots: Sequence[NavSnapshot],
    period: ReviewPeriod,
) -> PaperPerformanceMetrics:
    """NAV snapshot 기반 paper 성과 지표를 deterministic하게 계산한다."""
    sorted_snapshots = sort_nav_snapshots(nav_snapshots)
    snapshot_count = len(sorted_snapshots)

    if snapshot_count < 2:
        start_nav = sorted_snapshots[0].total_nav_krw if snapshot_count == 1 else Decimal("0")
        end_nav = start_nav
        return PaperPerformanceMetrics(
            start_nav_krw=start_nav,
            end_nav_krw=end_nav,
            total_return_percent=Decimal("0"),
            annualized_return_percent=None,
            max_drawdown_percent=Decimal("0"),
            worst_daily_return_percent=None,
            best_daily_return_percent=None,
            volatility_daily_percent=None,
            cash_average_percent=None,
            invested_average_percent=None,
            nav_snapshot_count=snapshot_count,
        )

    start_nav = sorted_snapshots[0].total_nav_krw
    end_nav = sorted_snapshots[-1].total_nav_krw
    total_return = ((end_nav / start_nav) - Decimal("1")) * Decimal("100")

    running_peak = sorted_snapshots[0].total_nav_krw
    max_drawdown = Decimal("0")
    for snapshot in sorted_snapshots:
        if snapshot.total_nav_krw > running_peak:
            running_peak = snapshot.total_nav_krw
        if running_peak > Decimal("0"):
            drawdown = ((snapshot.total_nav_krw / running_peak) - Decimal("1")) * Decimal("100")
            if drawdown < max_drawdown:
                max_drawdown = drawdown

    daily_returns = _compute_daily_returns(sorted_snapshots)
    worst_daily = min(daily_returns) if daily_returns else None
    best_daily = max(daily_returns) if daily_returns else None
    volatility = _compute_volatility(daily_returns)

    cash_values: list[Decimal] = []
    invested_values: list[Decimal] = []
    for snapshot in sorted_snapshots:
        if snapshot.total_nav_krw > Decimal("0"):
            cash_values.append((snapshot.cash_krw / snapshot.total_nav_krw) * Decimal("100"))
            invested_values.append(
                (snapshot.invested_krw / snapshot.total_nav_krw) * Decimal("100")
            )

    cash_average = (
        sum(cash_values, start=Decimal("0")) / Decimal(len(cash_values))
        if cash_values
        else None
    )
    invested_average = (
        sum(invested_values, start=Decimal("0")) / Decimal(len(invested_values))
        if invested_values
        else None
    )

    annualized_return: Decimal | None = None
    if period.sample_sufficiency != SampleSufficiency.INSUFFICIENT and start_nav > Decimal("0"):
        growth_factor = end_nav / start_nav
        exponent = Decimal("365") / Decimal(str(period.calendar_days))
        annualized_return = (growth_factor ** exponent - Decimal("1")) * Decimal("100")

    return PaperPerformanceMetrics(
        start_nav_krw=start_nav,
        end_nav_krw=end_nav,
        total_return_percent=total_return,
        annualized_return_percent=annualized_return,
        max_drawdown_percent=max_drawdown,
        worst_daily_return_percent=worst_daily,
        best_daily_return_percent=best_daily,
        volatility_daily_percent=volatility,
        cash_average_percent=cash_average,
        invested_average_percent=invested_average,
        nav_snapshot_count=snapshot_count,
    )


def compute_execution_review_metrics(
    order_intents: Sequence[OrderIntent],
    fills: Sequence[Fill],
    *,
    rejected_count: int = 0,
    rejected_count_available: bool = False,
) -> ExecutionReviewMetrics:
    """order intent / fill 기반 execution review 지표를 계산한다. broker 호출 없음."""
    warnings: list[str] = []

    manual_count = sum(
        1 for intent in order_intents if intent.execution_mode == ExecutionMode.MANUAL
    )
    emergency_count = sum(
        1 for intent in order_intents if intent.execution_mode == ExecutionMode.EMERGENCY_TRIGGER
    )
    mdd_killswitch_count = sum(
        1 for intent in order_intents if intent.execution_mode == ExecutionMode.MDD_KILLSWITCH
    )
    buy_count = sum(1 for intent in order_intents if intent.side == OrderSide.BUY)
    sell_count = sum(1 for intent in order_intents if intent.side == OrderSide.SELL)

    if not rejected_count_available and rejected_count == 0 and order_intents:
        warnings.append(
            "rejected_count unavailable from input; defaulting to 0 without OrderResult evidence."
        )

    fill_order_ids = {fill.order_id for fill in fills}
    intent_order_ids = {intent.order_id for intent in order_intents}

    orphan_fills = fill_order_ids - intent_order_ids
    if orphan_fills:
        warnings.append(
            f"{len(orphan_fills)} fill(s) have no matching order intent in review input."
        )

    missing_fills = intent_order_ids - fill_order_ids
    if missing_fills and order_intents:
        warnings.append(
            f"{len(missing_fills)} order intent(s) have no matching fill in review input."
        )

    avg_fill_notional: Decimal | None = None
    if fills:
        total_notional = sum(
            fill.quantity * fill.fill_price for fill in fills
        )
        avg_fill_notional = total_notional / Decimal(len(fills))

    warnings.append(
        "slippage review unavailable: reference prices not stored consistently in Phase 16 inputs."
    )

    return ExecutionReviewMetrics(
        order_intent_count=len(order_intents),
        fill_count=len(fills),
        rejected_count=rejected_count,
        manual_count=manual_count,
        emergency_count=emergency_count,
        mdd_killswitch_count=mdd_killswitch_count,
        avg_fill_notional_krw=avg_fill_notional,
        sell_count=sell_count,
        buy_count=buy_count,
        paper_fill_consistency_warnings=tuple(warnings),
    )


def count_emergency_triggers(events: Sequence[EmergencyTriggerEvent]) -> dict[str, int]:
    """EmergencyTriggerEvent를 trigger type / status 기준 deterministic count한다."""
    counts: dict[str, int] = {}

    for event in events:
        trigger_type = event.payload.trigger_type.value
        counts[trigger_type] = counts.get(trigger_type, 0) + 1

        if event.payload.status == EmergencyTriggerStatus.SUPPRESSED_BY_COOLDOWN:
            counts["cooldown_suppressed"] = counts.get("cooldown_suppressed", 0) + 1

        if event.payload.trigger_type == EmergencyTriggerType.MDD_KILLSWITCH:
            stage = event.payload.metadata.get("mdd_stage")
            if isinstance(stage, str):
                key = f"mdd_{stage.lower()}"
                counts[key] = counts.get(key, 0) + 1

    return {key: counts[key] for key in sorted(counts)}
