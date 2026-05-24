from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

# docs/POSTMORTEM_ERROR_TAGS.md와 동기화되는 canonical Postmortem error tag catalog.
POSTMORTEM_ERROR_TAG_CATALOG: Final[frozenset[str]] = frozenset(
    {
        "#과도한_보수성",
        "#금_비중_판단_오류",
        "#근거_해석_오류",
        "#논리_일관성_부족",
        "#벤치마크_오판",
        "#리밸런싱_지연",
        "#손절_지연",
        "#정보_과신",
        "#추격_매수",
        "#현금_관리_오류",
    }
)


def is_valid_postmortem_error_tag(value: str) -> bool:
    """등록된 Postmortem error tag인지 확인한다."""
    return value in POSTMORTEM_ERROR_TAG_CATALOG


def parse_postmortem_error_tag(value: str) -> str:
    """canonical Postmortem error tag를 파싱하고 catalog membership을 검증한다."""
    if not isinstance(value, str):
        raise ValueError("postmortem error tag must be a string.")

    normalized = value.strip()
    if not normalized:
        raise ValueError("postmortem error tag must not be blank.")

    if normalized != value:
        raise ValueError("postmortem error tag must not contain leading or trailing whitespace.")

    if not normalized.startswith("#"):
        raise ValueError(f"postmortem error tag must start with '#': {normalized!r}")

    if any(char.isspace() for char in normalized):
        raise ValueError(f"postmortem error tag must not contain whitespace: {normalized!r}")

    if not is_valid_postmortem_error_tag(normalized):
        raise ValueError(f"unknown postmortem error tag: {normalized!r}")

    return normalized


def _validate_tag_count(value: Any, *, tag: str) -> int:
    """Postmortem error tag count를 검증한다. bool은 int로 취급하지 않는다."""
    if isinstance(value, bool):
        raise ValueError(f"error_tags[{tag!r}] count must be an int, not bool.")

    if not isinstance(value, int):
        raise ValueError(f"error_tags[{tag!r}] count must be an int.")

    if value <= 0:
        raise ValueError(f"error_tags[{tag!r}] count must be > 0.")

    return value


def validate_postmortem_error_tags(tags: Mapping[str, Any]) -> dict[str, int]:
    """Postmortem error_tags mapping을 검증하고 canonical dict를 반환한다."""
    if not isinstance(tags, Mapping):
        raise ValueError("error_tags must be a mapping of tag -> count.")

    if not tags:
        raise ValueError("error_tags must not be empty.")

    validated: dict[str, int] = {}
    for raw_tag, raw_count in tags.items():
        parsed_tag = parse_postmortem_error_tag(str(raw_tag))
        if parsed_tag in validated:
            raise ValueError(f"duplicate postmortem error tag: {parsed_tag!r}")
        validated[parsed_tag] = _validate_tag_count(raw_count, tag=parsed_tag)

    return {tag: validated[tag] for tag in sorted(validated)}
