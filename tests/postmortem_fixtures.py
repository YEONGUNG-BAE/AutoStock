from __future__ import annotations

from datetime import UTC, date, datetime

from domain import DateId, DecisionId
from postmortem.models import (
    PostmortemEvaluation,
    PostmortemKind,
    PostmortemMarket,
    PostmortemRecord,
    PostmortemSource,
    PostmortemTagSummary,
    build_postmortem_id,
)

NOW = datetime(2026, 5, 24, 9, 0, tzinfo=UTC)

W20_START = date(2026, 5, 11)
W20_END = date(2026, 5, 17)
W20_PERIOD = "2026-W20"

MAY_START = date(2026, 5, 1)
MAY_END = date(2026, 5, 31)
MAY_PERIOD = "2026-05"


def sample_evaluation(**overrides: str) -> PostmortemEvaluation:
    base = {
        "price_result": "absolute return was modestly positive",
        "benchmark_relative_result": "underperformed benchmark by 1.2%",
        "evidence_validity": "Date-ID evidence was present but thin",
        "date_id_interpretation_accuracy": "macro signal was overweighted",
        "reasoning_action_consistency": "bearish evidence did not justify BUY size",
        "python_rule_outcome": "no Python rule rejection",
        "thesis_validity": "original thesis weakened by week end",
    }
    base.update(overrides)
    return PostmortemEvaluation(**base)


def sample_tag_summary(
    *,
    market: PostmortemMarket = PostmortemMarket.KR,
    period: str = W20_PERIOD,
    source: PostmortemSource = PostmortemSource.WEEKLY_POSTMORTEM,
    error_tags: dict[str, int] | None = None,
    top_error_tags: tuple[str, ...] | None = None,
) -> PostmortemTagSummary:
    tags = error_tags or {"#정보_과신": 2, "#추격_매수": 1}
    payload: dict[str, object] = {
        "market": market,
        "period": period,
        "source": source,
        "error_tags": tags,
    }
    if top_error_tags is not None:
        payload["top_error_tags"] = list(top_error_tags)
    return PostmortemTagSummary(**payload)


def sample_postmortem_record(
    *,
    market: PostmortemMarket = PostmortemMarket.KR,
    kind: PostmortemKind = PostmortemKind.WEEKLY,
    period: str = W20_PERIOD,
    evaluated_start_date: date = W20_START,
    evaluated_end_date: date = W20_END,
    tag_summary: PostmortemTagSummary | None = None,
    **overrides: object,
) -> PostmortemRecord:
    if tag_summary is None:
        source = (
            PostmortemSource.WEEKLY_POSTMORTEM
            if kind == PostmortemKind.WEEKLY
            else PostmortemSource.MONTHLY_POSTMORTEM
        )
        tag_summary = sample_tag_summary(market=market, period=period, source=source)

    base: dict[str, object] = {
        "postmortem_id": build_postmortem_id(kind=kind, market=market, period=period),
        "market": market,
        "kind": kind,
        "period": period,
        "created_at": NOW,
        "evaluated_start_date": evaluated_start_date,
        "evaluated_end_date": evaluated_end_date,
        "summary": "weekly KR review summary",
        "evaluation": sample_evaluation(),
        "findings": ("finding one",),
        "lessons": ("lesson one",),
        "tag_summary": tag_summary,
        "date_ids_used": (DateId("260511-1"),),
        "decision_snapshot_ids": (DecisionId("decision-001"),),
    }
    base.update(overrides)
    return PostmortemRecord(**base)
