from __future__ import annotations

from collections.abc import Iterable

from postmortem.error_tags import parse_postmortem_error_tag
from postmortem.models import PostmortemTagSummary, derive_top_error_tags


def aggregate_error_tags(summaries: Iterable[PostmortemTagSummary]) -> dict[str, int]:
    """여러 Postmortem tag summary의 error_tags count를 합산한다."""
    aggregated: dict[str, int] = {}
    for summary in summaries:
        for tag, count in summary.error_tags.items():
            parsed_tag = parse_postmortem_error_tag(tag)
            aggregated[parsed_tag] = aggregated.get(parsed_tag, 0) + count
    return {tag: aggregated[tag] for tag in sorted(aggregated)}


def top_error_tags_from_summaries(
    summaries: Iterable[PostmortemTagSummary],
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    """Postmortem tag summary만으로 Top N error tags를 집계한다."""
    if limit < 1:
        raise ValueError("limit must be >= 1.")

    aggregated = aggregate_error_tags(summaries)
    if not aggregated:
        return ()

    return derive_top_error_tags(aggregated, limit=limit)
