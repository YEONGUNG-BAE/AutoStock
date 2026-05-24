from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain import DateId, DecisionId
from logs import DailySummary, DebugEvent
from postmortem.models import (
    PostmortemEvaluation,
    PostmortemKind,
    PostmortemMarket,
    PostmortemRecord,
    PostmortemSource,
    PostmortemTagSummary,
    derive_top_error_tags,
)
from postmortem_fixtures import (
    MAY_END,
    MAY_PERIOD,
    MAY_START,
    NOW,
    W20_END,
    W20_PERIOD,
    W20_START,
    sample_evaluation,
    sample_postmortem_record,
    sample_tag_summary,
)

_record = sample_postmortem_record
_tag_summary = sample_tag_summary
_evaluation = sample_evaluation


def test_valid_weekly_kr_record() -> None:
    record = _record(market=PostmortemMarket.KR)
    assert record.kind == PostmortemKind.WEEKLY
    assert record.market == PostmortemMarket.KR


def test_valid_weekly_us_record() -> None:
    record = _record(
        market=PostmortemMarket.US,
        summary="weekly US review summary",
    )
    assert record.market == PostmortemMarket.US


def test_valid_monthly_kr_record() -> None:
    record = _record(
        kind=PostmortemKind.MONTHLY,
        period=MAY_PERIOD,
        evaluated_start_date=MAY_START,
        evaluated_end_date=MAY_END,
        tag_summary=_tag_summary(
            market=PostmortemMarket.KR,
            period=MAY_PERIOD,
            source=PostmortemSource.MONTHLY_POSTMORTEM,
        ),
        summary="monthly KR review summary",
    )
    assert record.kind == PostmortemKind.MONTHLY


def test_valid_monthly_us_record() -> None:
    record = _record(
        market=PostmortemMarket.US,
        kind=PostmortemKind.MONTHLY,
        period=MAY_PERIOD,
        evaluated_start_date=MAY_START,
        evaluated_end_date=MAY_END,
        tag_summary=_tag_summary(
            market=PostmortemMarket.US,
            period=MAY_PERIOD,
            source=PostmortemSource.MONTHLY_POSTMORTEM,
        ),
        summary="monthly US review summary",
    )
    assert record.market == PostmortemMarket.US


def test_weekly_period_format_reject_for_monthly() -> None:
    with pytest.raises(ValueError, match="invalid monthly period format"):
        _record(
            kind=PostmortemKind.MONTHLY,
            period=W20_PERIOD,
            evaluated_start_date=W20_START,
            evaluated_end_date=W20_END,
            tag_summary=_tag_summary(
                period=W20_PERIOD,
                source=PostmortemSource.MONTHLY_POSTMORTEM,
            ),
        )


def test_monthly_period_format_reject_for_weekly() -> None:
    with pytest.raises(ValueError, match="invalid weekly period format"):
        _record(
            period=MAY_PERIOD,
            evaluated_start_date=MAY_START,
            evaluated_end_date=MAY_END,
        )


def test_invalid_week_reject() -> None:
    with pytest.raises(ValueError, match="invalid ISO week"):
        PostmortemTagSummary(
            market=PostmortemMarket.KR,
            period="2026-W00",
            source=PostmortemSource.WEEKLY_POSTMORTEM,
            error_tags={"#정보_과신": 1},
        )


def test_invalid_month_reject() -> None:
    with pytest.raises(ValueError, match="invalid month"):
        PostmortemTagSummary(
            market=PostmortemMarket.KR,
            period="2026-13",
            source=PostmortemSource.MONTHLY_POSTMORTEM,
            error_tags={"#정보_과신": 1},
        )


def test_weekly_period_date_mismatch_reject() -> None:
    with pytest.raises(ValueError, match="evaluated_start_date must be Monday"):
        _record(evaluated_start_date=date(2026, 5, 12))


def test_monthly_period_date_mismatch_reject() -> None:
    with pytest.raises(ValueError, match="evaluated_start_date must be first day"):
        _record(
            kind=PostmortemKind.MONTHLY,
            period=MAY_PERIOD,
            evaluated_start_date=date(2026, 5, 2),
            evaluated_end_date=MAY_END,
            tag_summary=_tag_summary(
                period=MAY_PERIOD,
                source=PostmortemSource.MONTHLY_POSTMORTEM,
            ),
        )


def test_weekly_date_range_requires_same_iso_week() -> None:
    with pytest.raises(ValueError, match="evaluated_end_date must be Sunday"):
        _record(evaluated_end_date=date(2026, 5, 18))


def test_monthly_date_range_requires_same_month() -> None:
    with pytest.raises(ValueError, match="evaluated_end_date must be last day"):
        _record(
            kind=PostmortemKind.MONTHLY,
            period=MAY_PERIOD,
            evaluated_start_date=MAY_START,
            evaluated_end_date=date(2026, 5, 30),
            tag_summary=_tag_summary(
                period=MAY_PERIOD,
                source=PostmortemSource.MONTHLY_POSTMORTEM,
            ),
        )


def test_naive_created_at_reject() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _record(created_at=datetime(2026, 5, 24, 9, 0))


def test_blank_summary_reject() -> None:
    with pytest.raises(ValueError, match="blank"):
        _record(summary="   ")


def test_blank_evaluation_field_reject() -> None:
    with pytest.raises(ValueError, match="blank"):
        PostmortemEvaluation(
            price_result="ok",
            benchmark_relative_result="ok",
            evidence_validity="ok",
            date_id_interpretation_accuracy="ok",
            reasoning_action_consistency="ok",
            python_rule_outcome="   ",
            thesis_validity="ok",
        )


def test_blank_finding_reject() -> None:
    with pytest.raises(ValueError, match="blank"):
        _record(findings=(" ",))


def test_blank_lesson_reject() -> None:
    with pytest.raises(ValueError, match="blank"):
        _record(lessons=(" ",))


def test_extra_field_reject() -> None:
    with pytest.raises(ValueError):
        _record(extra_field="not allowed")


def test_tag_summary_market_mismatch_reject() -> None:
    with pytest.raises(ValueError, match="tag_summary.market"):
        _record(
            market=PostmortemMarket.KR,
            tag_summary=_tag_summary(market=PostmortemMarket.US),
        )


def test_tag_summary_period_mismatch_reject() -> None:
    with pytest.raises(ValueError, match="tag_summary.period"):
        _record(tag_summary=_tag_summary(period="2026-W21"))


def test_weekly_record_rejects_monthly_tag_summary() -> None:
    """record.kind=weekly인데 monthly tag summary를 붙이면 period/source 모두 불일치로 reject."""
    with pytest.raises(ValueError, match="tag_summary.period"):
        _record(
            tag_summary=_tag_summary(
                period=MAY_PERIOD,
                source=PostmortemSource.MONTHLY_POSTMORTEM,
            ),
        )


def test_top_error_tags_derived_deterministically() -> None:
    summary = _tag_summary(top_error_tags=None)
    assert summary.top_error_tags == ("#정보_과신", "#추격_매수")


def test_top_error_tags_tie_break_by_tag_name() -> None:
    tags = derive_top_error_tags({"#추격_매수": 2, "#정보_과신": 2, "#손절_지연": 1})
    assert tags == ("#정보_과신", "#추격_매수", "#손절_지연")


def test_wrong_top_error_tags_order_reject() -> None:
    with pytest.raises(ValueError, match="top_error_tags must match"):
        _tag_summary(top_error_tags=("#추격_매수", "#정보_과신"))


def test_only_top_three_accepted_in_tag_summary() -> None:
    with pytest.raises(ValueError, match="at most 3"):
        _tag_summary(
            error_tags={
                "#정보_과신": 4,
                "#추격_매수": 3,
                "#손절_지연": 2,
                "#근거_해석_오류": 1,
            },
            top_error_tags=(
                "#정보_과신",
                "#추격_매수",
                "#손절_지연",
                "#근거_해석_오류",
            ),
        )


def test_evaluation_fields_required_and_separate() -> None:
    evaluation = _evaluation(
        price_result="price fell 2%",
        benchmark_relative_result="still outperformed benchmark",
        python_rule_outcome="RiskFilter rejected one order; not an investment tag",
    )
    assert evaluation.price_result != evaluation.benchmark_relative_result
    assert "LLM_SCHEMA_ERROR" not in evaluation.python_rule_outcome


def test_debug_event_code_not_accepted_as_postmortem_tag() -> None:
    with pytest.raises(ValueError, match="must start with '#'"):
        _tag_summary(error_tags={"LLM_SCHEMA_ERROR": 1})


def test_canonical_dict_roundtrip_fields() -> None:
    record = _record(metadata={"reviewer": "human"})
    canonical = record.to_canonical_dict()
    assert canonical["postmortem_id"] == record.postmortem_id
    assert canonical["tag_summary"]["error_tags"]["#정보_과신"] == 2
    assert record.payload_hash() == record.payload_hash()


def test_debug_event_has_no_error_tags_field() -> None:
    assert "error_tags" not in DebugEvent.model_fields
    assert "top_error_tags" not in DebugEvent.model_fields


def test_daily_summary_has_no_error_tags_field() -> None:
    assert "error_tags" not in DailySummary.model_fields
    assert "top_error_tags" not in DailySummary.model_fields
