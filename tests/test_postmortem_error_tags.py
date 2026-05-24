from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from logs.event_codes import DEBUG_EVENT_CODE_CATALOG
from postmortem.error_tags import (
    POSTMORTEM_ERROR_TAG_CATALOG,
    is_valid_postmortem_error_tag,
    parse_postmortem_error_tag,
    validate_postmortem_error_tags,
)

POSTMORTEM_DOCS_PATH = Path(__file__).resolve().parents[1] / "docs" / "POSTMORTEM_ERROR_TAGS.md"
DEBUG_DOCS_PATH = Path(__file__).resolve().parents[1] / "docs" / "DEBUG_EVENT_CODES.md"


def _parse_postmortem_tag_table(markdown_text: str) -> set[str]:
    """docs/POSTMORTEM_ERROR_TAGS.md tag table rows를 파싱한다."""
    tags: set[str] = set()
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("| `#"):
            continue
        match = re.match(r"^\|\s*`(#[^`]+)`\s*\|", stripped)
        if match is not None:
            tags.add(match.group(1))
    return tags


def test_valid_tags_accept() -> None:
    assert is_valid_postmortem_error_tag("#정보_과신")
    assert parse_postmortem_error_tag("#정보_과신") == "#정보_과신"
    assert is_valid_postmortem_error_tag("#추격_매수")


def test_unknown_tag_reject() -> None:
    assert not is_valid_postmortem_error_tag("#없는_태그")
    with pytest.raises(ValueError, match="unknown postmortem error tag"):
        parse_postmortem_error_tag("#없는_태그")


def test_blank_tag_reject() -> None:
    with pytest.raises(ValueError, match="blank"):
        parse_postmortem_error_tag("   ")


def test_leading_trailing_whitespace_reject() -> None:
    with pytest.raises(ValueError, match="whitespace"):
        parse_postmortem_error_tag(" #정보_과신")


def test_tag_without_hash_reject() -> None:
    with pytest.raises(ValueError, match="must start with '#'"):
        parse_postmortem_error_tag("정보_과신")


def test_tag_with_internal_whitespace_reject() -> None:
    with pytest.raises(ValueError, match="must not contain whitespace"):
        parse_postmortem_error_tag("#정보 과신")


def test_count_zero_reject() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        validate_postmortem_error_tags({"#정보_과신": 0})


def test_count_negative_reject() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        validate_postmortem_error_tags({"#정보_과신": -1})


def test_bool_count_reject() -> None:
    with pytest.raises(ValueError, match="not bool"):
        validate_postmortem_error_tags({"#정보_과신": True})


def test_docs_and_code_catalog_match() -> None:
    docs_text = POSTMORTEM_DOCS_PATH.read_text(encoding="utf-8")
    docs_tags = _parse_postmortem_tag_table(docs_text)

    assert docs_tags
    assert docs_tags == set(POSTMORTEM_ERROR_TAG_CATALOG)


def test_debug_event_codes_are_not_postmortem_tags() -> None:
    assert POSTMORTEM_ERROR_TAG_CATALOG.isdisjoint(DEBUG_EVENT_CODE_CATALOG)


def test_debug_event_code_strings_reject_as_postmortem_tags() -> None:
    for code in ("LLM_SCHEMA_ERROR", "PAPER_NAV_SNAPSHOT_ERROR"):
        assert not is_valid_postmortem_error_tag(code)
        with pytest.raises(ValueError, match="must start with '#'"):
            parse_postmortem_error_tag(code)


def test_validate_postmortem_error_tags_returns_sorted_dict() -> None:
    validated = validate_postmortem_error_tags(
        {"#추격_매수": 1, "#정보_과신": 2},
    )
    assert validated == {"#정보_과신": 2, "#추격_매수": 1}
