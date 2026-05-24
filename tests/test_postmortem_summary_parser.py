from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from postmortem.models import PostmortemMarket, PostmortemSource
from postmortem.summary_parser import parse_postmortem_tag_summary_from_markdown

W20_PERIOD = "2026-W20"
MAY_PERIOD = "2026-05"


def _weekly_block(*, market: str = "KR", period: str = W20_PERIOD) -> str:
    return f"""## Weekly Review

Some prose review text.

```json
{{
  "market": "{market}",
  "period": "{period}",
  "source": "WeeklyPostmortem",
  "error_tags": {{
    "#정보_과신": 2,
    "#추격_매수": 1
  }},
  "top_error_tags": ["#정보_과신", "#추격_매수"]
}}
```"""


def _monthly_block(*, market: str = "KR", period: str = MAY_PERIOD) -> str:
    return f"""## Monthly Review

Some prose review text.

```json
{{
  "market": "{market}",
  "period": "{period}",
  "source": "MonthlyPostmortem",
  "error_tags": {{
    "#손절_지연": 1
  }},
  "top_error_tags": ["#손절_지연"]
}}
```"""


def test_valid_weekly_kr_json_block_parse() -> None:
    summary = parse_postmortem_tag_summary_from_markdown(_weekly_block())
    assert summary.market == PostmortemMarket.KR
    assert summary.period == W20_PERIOD
    assert summary.source == PostmortemSource.WEEKLY_POSTMORTEM


def test_valid_weekly_us_json_block_parse() -> None:
    summary = parse_postmortem_tag_summary_from_markdown(
        _weekly_block(market="US"),
    )
    assert summary.market == PostmortemMarket.US


def test_valid_monthly_kr_json_block_parse() -> None:
    summary = parse_postmortem_tag_summary_from_markdown(_monthly_block())
    assert summary.source == PostmortemSource.MONTHLY_POSTMORTEM
    assert summary.period == MAY_PERIOD


def test_valid_monthly_us_json_block_parse() -> None:
    summary = parse_postmortem_tag_summary_from_markdown(
        _monthly_block(market="US"),
    )
    assert summary.market == PostmortemMarket.US


def test_malformed_json_reject() -> None:
    markdown = """```json
{not valid json}
```"""
    with pytest.raises(ValueError, match="invalid postmortem tag summary JSON"):
        parse_postmortem_tag_summary_from_markdown(markdown)


def test_multiple_fenced_json_blocks_reject() -> None:
    markdown = _weekly_block() + "\n\n" + _monthly_block()
    with pytest.raises(ValueError, match="multiple fenced json blocks"):
        parse_postmortem_tag_summary_from_markdown(markdown)


def test_debug_event_code_reject() -> None:
    markdown = """```json
{
  "market": "KR",
  "period": "2026-W20",
  "source": "WeeklyPostmortem",
  "error_tags": {
    "LLM_SCHEMA_ERROR": 1
  }
}
```"""
    with pytest.raises(ValueError, match="must start with '#'"):
        parse_postmortem_tag_summary_from_markdown(markdown)


def test_unknown_postmortem_tag_reject() -> None:
    markdown = """```json
{
  "market": "KR",
  "period": "2026-W20",
  "source": "WeeklyPostmortem",
  "error_tags": {
    "#없는_태그": 1
  }
}
```"""
    with pytest.raises(ValueError, match="unknown postmortem error tag"):
        parse_postmortem_tag_summary_from_markdown(markdown)


def test_invalid_top_error_tags_reject() -> None:
    markdown = """```json
{
  "market": "KR",
  "period": "2026-W20",
  "source": "WeeklyPostmortem",
  "error_tags": {
    "#정보_과신": 2,
    "#추격_매수": 1
  },
  "top_error_tags": ["#추격_매수", "#정보_과신"]
}
```"""
    with pytest.raises(ValueError, match="top_error_tags must match"):
        parse_postmortem_tag_summary_from_markdown(markdown)


def test_missing_top_error_tags_derives_deterministically() -> None:
    markdown = """```json
{
  "market": "KR",
  "period": "2026-W20",
  "source": "WeeklyPostmortem",
  "error_tags": {
    "#정보_과신": 2,
    "#추격_매수": 1
  }
}
```"""
    summary = parse_postmortem_tag_summary_from_markdown(markdown)
    assert summary.top_error_tags == ("#정보_과신", "#추격_매수")


def test_trailing_json_object_without_fence() -> None:
    markdown = """\
## Review

Prose only.

{
  "market": "US",
  "period": "2026-W20",
  "source": "WeeklyPostmortem",
  "error_tags": {
    "#정보_과신": 1
  },
  "top_error_tags": ["#정보_과신"]
}"""
    summary = parse_postmortem_tag_summary_from_markdown(markdown)
    assert summary.market == PostmortemMarket.US


def test_parser_does_not_implement_llm_generation() -> None:
    assert "prompt" not in parse_postmortem_tag_summary_from_markdown.__name__
    assert parse_postmortem_tag_summary_from_markdown.__module__.endswith("summary_parser")
