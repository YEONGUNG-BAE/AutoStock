from __future__ import annotations

from collections.abc import Sequence

from domain.portfolio import NavSnapshot
from logs.models import DailySummary
from paper_review.models import PaperReviewInput, ReviewPeriod, SampleSufficiency


def collect_data_quality_warnings(review_input: PaperReviewInput) -> tuple[str, ...]:
    """report 생성을 중단하지 않는 data quality warning을 수집한다."""
    warnings: list[str] = []
    period = review_input.period

    if period.sample_sufficiency == SampleSufficiency.INSUFFICIENT:
        warnings.append(
            f"review period is {period.calendar_days} calendar days (<90); "
            "parameter recommendations should remain OBSERVE_MORE."
        )

    if len(review_input.nav_snapshots) < 2:
        warnings.append(
            "fewer than 2 NAV snapshots; return/drawdown metrics may be insufficient."
        )

    snapshot_ids = [item.snapshot_id for item in review_input.nav_snapshots]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        warnings.append("duplicate NAV snapshot IDs detected in input.")

    sorted_by_as_of = sorted(review_input.nav_snapshots, key=lambda item: item.as_of.isoformat())
    if list(review_input.nav_snapshots) != sorted_by_as_of:
        warnings.append("NAV snapshots were not pre-sorted by as_of; canonicalized deterministically.")

    if _has_missing_daily_returns(review_input.nav_snapshots):
        warnings.append(
            "some NAV snapshots lack daily_return_percent; adjacent NAV fallback was used where possible."
        )

    if not review_input.daily_summaries:
        warnings.append("missing DailySummary coverage for review period.")
    else:
        coverage_warning = _daily_summary_coverage_warning(period, review_input.daily_summaries)
        if coverage_warning is not None:
            warnings.append(coverage_warning)

    if not review_input.postmortem_records:
        warnings.append("missing Postmortem coverage for review period.")

    if review_input.metadata.get("emergency_event_store_unavailable"):
        warnings.append("emergency event store unavailable; emergency counts may be incomplete.")

    fill_order_ids = {fill.order_id for fill in review_input.fills}
    intent_order_ids = {intent.order_id for intent in review_input.order_intents}
    orphan_fills = fill_order_ids - intent_order_ids
    if orphan_fills:
        warnings.append(f"{len(orphan_fills)} fill(s) without matching order intents.")

    missing_fills = intent_order_ids - fill_order_ids
    if missing_fills and review_input.order_intents:
        warnings.append(f"{len(missing_fills)} order intent(s) without fills when fills were expected.")

    if review_input.metadata.get("source") in {"kis_live", "kis_mock", "KIS_MOCK"}:
        warnings.append("live/KIS data detected as attempted paper review source; rejected for review.")

    return tuple(warnings)


def _has_missing_daily_returns(nav_snapshots: Sequence[NavSnapshot]) -> bool:
    """daily_return_percent가 없는 snapshot이 하나라도 있으면 True."""
    if len(nav_snapshots) < 2:
        return False
    sorted_snapshots = sorted(nav_snapshots, key=lambda item: item.as_of.isoformat())
    return any(item.daily_return_percent is None for item in sorted_snapshots[1:])


def _daily_summary_coverage_warning(
    period: ReviewPeriod,
    daily_summaries: Sequence[DailySummary],
) -> str | None:
    """DailySummary trading_date coverage gap warning."""
    covered_dates = {summary.trading_date for summary in daily_summaries}
    expected_days = period.calendar_days
    if len(covered_dates) < expected_days:
        return (
            f"DailySummary covers {len(covered_dates)} of ~{expected_days} calendar days "
            "in review period."
        )
    return None
