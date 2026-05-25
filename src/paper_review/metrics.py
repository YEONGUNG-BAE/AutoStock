from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal

from config.settings import ExecutionMode
from domain.enums import OrderSide
from domain.order import Fill, OrderIntent
from domain.portfolio import NavSnapshot
from emergency.models import EmergencyTriggerEvent, EmergencyTriggerStatus, EmergencyTriggerType
from paper_review.models import ExecutionReviewMetrics, PaperPerformanceMetrics, ReviewPeriod, SampleSufficiency


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
