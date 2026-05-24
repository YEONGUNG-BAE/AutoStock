from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from postmortem.aggregation import aggregate_error_tags, top_error_tags_from_summaries
from postmortem.models import PostmortemMarket, PostmortemSource, PostmortemTagSummary

W20_PERIOD = "2026-W20"
MAY_PERIOD = "2026-05"


def _summary(
    *,
    market: PostmortemMarket = PostmortemMarket.KR,
    period: str = W20_PERIOD,
    source: PostmortemSource = PostmortemSource.WEEKLY_POSTMORTEM,
    error_tags: dict[str, int],
) -> PostmortemTagSummary:
    return PostmortemTagSummary(
        market=market,
        period=period,
        source=source,
        error_tags=error_tags,
    )


def test_aggregate_multiple_summaries() -> None:
    summaries = (
        _summary(error_tags={"#정보_과신": 2, "#추격_매수": 1}),
        _summary(
            market=PostmortemMarket.US,
            error_tags={"#정보_과신": 1, "#손절_지연": 2},
        ),
        _summary(
            period=MAY_PERIOD,
            source=PostmortemSource.MONTHLY_POSTMORTEM,
            error_tags={"#추격_매수": 3},
        ),
    )

    aggregated = aggregate_error_tags(summaries)
    assert aggregated == {
        "#정보_과신": 3,
        "#손절_지연": 2,
        "#추격_매수": 4,
    }


def test_top_three_uses_only_postmortem_tags() -> None:
    summaries = (
        _summary(error_tags={"#정보_과신": 5, "#추격_매수": 4, "#손절_지연": 3, "#근거_해석_오류": 1}),
    )
    top = top_error_tags_from_summaries(summaries)
    assert top == ("#정보_과신", "#추격_매수", "#손절_지연")


def test_deterministic_tie_break() -> None:
    summaries = (
        _summary(error_tags={"#추격_매수": 2, "#정보_과신": 2}),
        _summary(error_tags={"#손절_지연": 2}),
    )
    top = top_error_tags_from_summaries(summaries)
    # count 동률 시 tag 문자열 오름차순 tie-break
    assert top == ("#손절_지연", "#정보_과신", "#추격_매수")


def test_empty_summaries_returns_empty_result() -> None:
    assert aggregate_error_tags(()) == {}
    assert top_error_tags_from_summaries(()) == ()


def test_invalid_limit_reject() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        top_error_tags_from_summaries((), limit=0)


def test_debug_event_code_cannot_enter_aggregation() -> None:
    with pytest.raises(ValueError, match="must start with '#'"):
        PostmortemTagSummary(
            market=PostmortemMarket.KR,
            period=W20_PERIOD,
            source=PostmortemSource.WEEKLY_POSTMORTEM,
            error_tags={"PAPER_NAV_SNAPSHOT_ERROR": 1},
        )
