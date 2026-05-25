from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper_review.metrics import compute_paper_performance_metrics, sort_nav_snapshots
from paper_review_fixtures import sample_nav_series, sample_nav_series_with_drawdown, sample_review_period


def test_total_return_calculation() -> None:
    snapshots = sample_nav_series(count=3, start_nav=Decimal("10000000"))
    period = sample_review_period()
    metrics = compute_paper_performance_metrics(snapshots, period)

    expected_end = snapshots[-1].total_nav_krw
    expected_start = snapshots[0].total_nav_krw
    expected_return = ((expected_end / expected_start) - Decimal("1")) * Decimal("100")
    assert metrics.total_return_percent == expected_return


def test_max_drawdown_calculation() -> None:
    snapshots = sample_nav_series_with_drawdown()
    period = sample_review_period()
    metrics = compute_paper_performance_metrics(snapshots, period)

    assert metrics.max_drawdown_percent <= Decimal("0")
    assert metrics.max_drawdown_percent < Decimal("-5")


def test_adjacent_nav_return_fallback_when_daily_return_percent_absent() -> None:
    snapshots = sample_nav_series(count=4)
    period = sample_review_period()
    metrics = compute_paper_performance_metrics(snapshots, period)

    assert metrics.worst_daily_return_percent is not None
    assert metrics.best_daily_return_percent is not None


def test_uses_daily_return_percent_when_present() -> None:
    period = sample_review_period()
    base = sample_nav_series(count=3)
    snapshots = (
        base[0],
        base[1].model_copy(update={"daily_return_percent": Decimal("-2.5")}),
        base[2],
    )
    metrics = compute_paper_performance_metrics(snapshots, period)
    assert metrics.worst_daily_return_percent == Decimal("-2.5")


def test_annualized_return_none_for_insufficient_sample() -> None:
    period = sample_review_period(calendar_days=30)
    metrics = compute_paper_performance_metrics(sample_nav_series(count=5), period)
    assert metrics.annualized_return_percent is None


def test_snapshots_sorted_deterministically() -> None:
    snapshots = sample_nav_series(count=3)
    reversed_snapshots = tuple(reversed(snapshots))
    sorted_result = sort_nav_snapshots(reversed_snapshots)
    assert [item.snapshot_id for item in sorted_result] == [item.snapshot_id for item in snapshots]
