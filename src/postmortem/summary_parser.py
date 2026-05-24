from __future__ import annotations

import json
import re

from postmortem.models import PostmortemTagSummary

# fenced ```json ... ``` block 추출
_JSON_FENCE_PATTERN = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_fenced_json_blocks(markdown: str) -> list[str]:
    """markdown에서 fenced json block 본문 목록을 반환한다."""
    return [match.group(1).strip() for match in _JSON_FENCE_PATTERN.finditer(markdown)]


def _extract_trailing_json_objects(markdown: str) -> list[str]:
    """fenced block 없을 때 문서 끝의 JSON object 후보를 추출한다."""
    stripped = markdown.rstrip()
    if not stripped:
        return []

    valid: list[str] = []
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        candidate = stripped[index:].strip()
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            valid.append(candidate)

    return valid


def parse_postmortem_tag_summary_from_markdown(markdown: str) -> PostmortemTagSummary:
    """Postmortem markdown의 machine-readable JSON block에서 tag summary를 파싱한다."""
    if not isinstance(markdown, str):
        raise ValueError("markdown must be a string.")

    fenced_blocks = _extract_fenced_json_blocks(markdown)
    if len(fenced_blocks) > 1:
        raise ValueError("ambiguous postmortem tag summary: multiple fenced json blocks.")

    json_text: str | None = None
    if len(fenced_blocks) == 1:
        json_text = fenced_blocks[0]
    else:
        trailing_candidates = _extract_trailing_json_objects(markdown)
        if len(trailing_candidates) > 1:
            raise ValueError("ambiguous postmortem tag summary: multiple JSON object blocks.")
        if len(trailing_candidates) == 1:
            json_text = trailing_candidates[0]

    if json_text is None:
        raise ValueError("postmortem tag summary JSON block not found.")

    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid postmortem tag summary JSON.") from exc

    if not isinstance(payload, dict):
        raise ValueError("postmortem tag summary must be a JSON object.")

    return PostmortemTagSummary.model_validate(payload)
