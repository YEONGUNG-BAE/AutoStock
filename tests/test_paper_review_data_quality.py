from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper_review.data_quality import collect_data_quality_warnings
from paper_review_fixtures import sample_nav_series, sample_review_input


def test_review_period_under_90_days_warning() -> None:
    review_input = sample_review_input(calendar_days=30)
    warnings = collect_data_quality_warnings(review_input)
    assert any("<90" in warning for warning in warnings)


def test_fewer_than_two_nav_snapshots_warning() -> None:
    review_input = sample_review_input(nav_snapshots=sample_nav_series(count=1))
    warnings = collect_data_quality_warnings(review_input)
    assert any("fewer than 2 NAV snapshots" in warning for warning in warnings)


def test_missing_daily_summary_warning() -> None:
    review_input = sample_review_input(daily_summaries=())
    warnings = collect_data_quality_warnings(review_input)
    assert any("missing DailySummary coverage" in warning for warning in warnings)


def test_missing_postmortem_warning() -> None:
    review_input = sample_review_input(postmortem_records=())
    warnings = collect_data_quality_warnings(review_input)
    assert any("missing Postmortem coverage" in warning for warning in warnings)


def test_live_kis_source_warning() -> None:
    review_input = sample_review_input(metadata={"source": "kis_live"})
    warnings = collect_data_quality_warnings(review_input)
    assert any("live/KIS data detected" in warning for warning in warnings)
