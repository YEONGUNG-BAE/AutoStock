from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper_review.models import (
    ParameterRecommendation,
    PaperReviewInput,
    RecommendationActionability,
    RecommendationType,
    ReviewConfidence,
    ReviewPeriod,
    SampleSufficiency,
)
from paper_review_fixtures import sample_nav_series, sample_review_input, sample_review_period


def test_valid_review_period() -> None:
    period = ReviewPeriod.from_dates(start_date=date(2026, 1, 1), end_date=date(2026, 6, 30))
    assert period.calendar_days == 181
    assert period.sample_sufficiency == SampleSufficiency.SUFFICIENT


def test_invalid_date_range_rejected() -> None:
    with pytest.raises(ValidationError, match="start_date must be <= end_date"):
        ReviewPeriod(
            start_date=date(2026, 6, 1),
            end_date=date(2026, 1, 1),
            calendar_days=1,
            sample_sufficiency=SampleSufficiency.INSUFFICIENT,
        )


def test_sample_sufficiency_under_90_insufficient() -> None:
    period = ReviewPeriod.from_dates(start_date=date(2026, 1, 1), end_date=date(2026, 3, 30))
    assert period.calendar_days == 89
    assert period.sample_sufficiency == SampleSufficiency.INSUFFICIENT


def test_sample_sufficiency_90_to_179_partial() -> None:
    period = ReviewPeriod.from_dates(start_date=date(2026, 1, 1), end_date=date(2026, 6, 28))
    assert period.calendar_days == 179
    assert period.sample_sufficiency == SampleSufficiency.PARTIAL


def test_sample_sufficiency_180_or_more_sufficient() -> None:
    period = ReviewPeriod.from_dates(start_date=date(2026, 1, 1), end_date=date(2026, 6, 30))
    assert period.calendar_days == 181
    assert period.sample_sufficiency == SampleSufficiency.SUFFICIENT


def test_parameter_recommendation_rejects_auto_apply_true() -> None:
    with pytest.raises(ValidationError, match="auto_apply must be False"):
        ParameterRecommendation(
            recommendation_id="rec-1",
            parameter_name="mdd_threshold",
            current_value="-10%",
            candidate_value=None,
            recommendation_type=RecommendationType.KEEP,
            actionability=RecommendationActionability.NOT_ACTIONABLE,
            evidence=("test",),
            confidence_level=ReviewConfidence.LOW,
            risk_of_change="low",
            auto_apply=True,
        )


def test_parameter_recommendation_requires_human_approval() -> None:
    with pytest.raises(ValidationError, match="requires_human_approval must be True"):
        ParameterRecommendation(
            recommendation_id="rec-1",
            parameter_name="mdd_threshold",
            current_value="-10%",
            candidate_value=None,
            recommendation_type=RecommendationType.KEEP,
            actionability=RecommendationActionability.NOT_ACTIONABLE,
            evidence=("test",),
            confidence_level=ReviewConfidence.LOW,
            risk_of_change="low",
            requires_human_approval=False,
        )


def test_paper_review_input_rejects_duplicate_nav_snapshot_ids() -> None:
    snapshots = sample_nav_series(count=2)
    duplicate = snapshots[0].model_copy(update={"as_of": snapshots[1].as_of})
    period = sample_review_period()

    with pytest.raises(ValidationError, match="duplicate nav snapshot_id"):
        PaperReviewInput(
            review_id="review-dup",
            created_at=sample_review_input().created_at,
            period=period,
            nav_snapshots=(snapshots[0], duplicate),
        )


def test_metadata_canonical_validation() -> None:
    with pytest.raises(ValidationError, match="float values are not allowed"):
        PaperReviewInput(
            review_id="review-meta",
            created_at=sample_review_input().created_at,
            period=sample_review_period(),
            nav_snapshots=sample_nav_series(count=2),
            metadata={"ratio": 1.5},
        )


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        ReviewPeriod(
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
            calendar_days=2,
            sample_sufficiency=SampleSufficiency.INSUFFICIENT,
            unexpected="value",
        )
