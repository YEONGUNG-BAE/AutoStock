from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain.identifiers import Percent
from logs.models import DailyRunStatus, DailySummary
from paper_review.metrics import compute_execution_review_metrics, compute_paper_performance_metrics
from paper_review.models import (
    RecommendationActionability,
    RecommendationType,
    ReviewPeriod,
    SampleSufficiency,
)
from paper_review.parameter_review import (
    review_allocator_tolerance,
    review_asset_bands,
    review_execution_model,
    review_mdd_threshold,
)
from paper_review_fixtures import (
    NOW,
    sample_daily_summary,
    sample_fill,
    sample_mdd_emergency_event,
    sample_nav_series,
    sample_order_intent,
    sample_review_period,
)


def test_mdd_insufficient_sample_observe_more() -> None:
    period = ReviewPeriod.from_dates(start_date=date(2026, 1, 1), end_date=date(2026, 2, 28))
    performance = compute_paper_performance_metrics(sample_nav_series(count=5), period)
    review = review_mdd_threshold(performance=performance, emergency_events=(), period=period)

    assert review.recommendations[0].recommendation_type == RecommendationType.OBSERVE_MORE
    assert review.recommendations[0].actionability == RecommendationActionability.OBSERVE_MORE


def test_mdd_level_3_investigate_human_review() -> None:
    period = sample_review_period()
    performance = compute_paper_performance_metrics(sample_nav_series(count=5), period)
    review = review_mdd_threshold(
        performance=performance,
        emergency_events=(sample_mdd_emergency_event(stage="LEVEL_3"),),
        period=period,
    )

    recommendation = review.recommendations[0]
    assert recommendation.recommendation_type == RecommendationType.INVESTIGATE
    assert recommendation.actionability == RecommendationActionability.HUMAN_REVIEW_REQUIRED
    assert review.mdd_level_3_count == 1


def test_repeated_mdd_level_1_2_investigate() -> None:
    period = sample_review_period()
    performance = compute_paper_performance_metrics(sample_nav_series(count=5), period)
    events = (
        sample_mdd_emergency_event(event_id="m1", stage="LEVEL_1"),
        sample_mdd_emergency_event(event_id="m2", stage="LEVEL_2"),
    )
    review = review_mdd_threshold(performance=performance, emergency_events=events, period=period)

    assert review.recommendations[0].recommendation_type == RecommendationType.INVESTIGATE


def test_false_positive_and_missed_risk_default_zero() -> None:
    period = sample_review_period()
    performance = compute_paper_performance_metrics(sample_nav_series(count=5), period)
    review = review_mdd_threshold(performance=performance, emergency_events=(), period=period)

    assert review.false_positive_suspected_count == 0
    assert review.missed_risk_suspected_count == 0


def test_all_recommendations_auto_apply_false() -> None:
    period = sample_review_period()
    performance = compute_paper_performance_metrics(sample_nav_series(count=5), period)
    review = review_mdd_threshold(
        performance=performance,
        emergency_events=(sample_mdd_emergency_event(stage="LEVEL_3"),),
        period=period,
    )
    for recommendation in review.recommendations:
        assert recommendation.auto_apply is False
        assert recommendation.requires_human_approval is True


def test_asset_band_sustained_breach_investigate() -> None:
    period = sample_review_period()
    summaries = tuple(
        sample_daily_summary(
            trading_date=period.start_date + timedelta(days=index),
            range_violation_count=2,
        )
        for index in range(5)
    )
    review = review_asset_bands(
        daily_summaries=summaries,
        emergency_events=(),
        postmortem_records=(),
        period=period,
    )

    assert review.recommendations[0].recommendation_type == RecommendationType.INVESTIGATE


def test_asset_band_insufficient_evidence_observe_more() -> None:
    period = ReviewPeriod.from_dates(start_date=date(2026, 1, 1), end_date=date(2026, 2, 28))
    review = review_asset_bands(
        daily_summaries=(),
        emergency_events=(),
        postmortem_records=(),
        period=period,
    )
    assert review.recommendations[0].recommendation_type == RecommendationType.OBSERVE_MORE


def test_allocator_fallback_investigate() -> None:
    period = sample_review_period()
    summaries = tuple(
        sample_daily_summary(
            trading_date=period.start_date + timedelta(days=index),
            allocator_fallback_count=1,
        )
        for index in range(4)
    )
    review = review_allocator_tolerance(daily_summaries=summaries, period=period)
    assert review.recommendations[0].recommendation_type == RecommendationType.INVESTIGATE


def test_execution_model_observe_more_insufficient_fills() -> None:
    period = sample_review_period()
    execution = compute_execution_review_metrics(
        (sample_order_intent(), sample_order_intent(order_id="o2")),
        (),
    )
    recommendations = review_execution_model(execution=execution, period=period)
    assert any(item.recommendation_type == RecommendationType.OBSERVE_MORE for item in recommendations)
