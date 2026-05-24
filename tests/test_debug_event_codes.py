from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from logs.event_codes import (
    DEBUG_EVENT_CODE_CATALOG,
    DEBUG_EVENT_DEFAULT_SEVERITY,
    DEBUG_EVENT_SOURCE_VALUES,
    is_valid_debug_event_code,
    parse_debug_event_code,
)
from logs.models import LogSeverity

DOCS_PATH = Path(__file__).resolve().parents[1] / "docs" / "DEBUG_EVENT_CODES.md"


def _parse_event_code_tables(markdown_text: str) -> dict[str, str]:
    """docs/DEBUG_EVENT_CODES.md event_code table rows를 파싱한다."""
    codes: dict[str, str] = {}
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("| `"):
            continue
        match = re.match(r"^\|\s*`([A-Z0-9_]+)`\s*\|\s*([A-Z]+)\s*\|", stripped)
        if match is None:
            continue
        event_code, severity = match.groups()
        codes[event_code] = severity
    return codes


def _parse_source_table(markdown_text: str) -> set[str]:
    """docs/DEBUG_EVENT_CODES.md source table values를 파싱한다."""
    sources: set[str] = set()
    in_source_section = False
    for line in markdown_text.splitlines():
        if line.strip() == "## Source values":
            in_source_section = True
            continue
        if in_source_section and line.startswith("---"):
            break
        if not in_source_section:
            continue
        if not line.strip().startswith("| `"):
            continue
        match = re.match(r"^\|\s*`([^`]+)`\s*\|", line.strip())
        if match is not None:
            sources.add(match.group(1))
    return sources


def test_valid_catalog_code_accept() -> None:
    assert is_valid_debug_event_code("LLM_SCHEMA_ERROR")
    assert parse_debug_event_code("LLM_SCHEMA_ERROR") == "LLM_SCHEMA_ERROR"


def test_unknown_code_reject() -> None:
    assert not is_valid_debug_event_code("INVENTED_EVENT_CODE")
    with pytest.raises(ValueError, match="unknown debug event_code"):
        parse_debug_event_code("INVENTED_EVENT_CODE")


def test_blank_code_reject() -> None:
    with pytest.raises(ValueError, match="blank"):
        parse_debug_event_code("   ")


def test_docs_and_code_catalog_match() -> None:
    docs_text = DOCS_PATH.read_text(encoding="utf-8")
    docs_codes = _parse_event_code_tables(docs_text)

    assert docs_codes
    assert set(docs_codes) == set(DEBUG_EVENT_CODE_CATALOG)
    for event_code, severity in docs_codes.items():
        assert DEBUG_EVENT_DEFAULT_SEVERITY[event_code] == severity


def test_paper_nav_snapshot_error_exists() -> None:
    docs_text = DOCS_PATH.read_text(encoding="utf-8")
    docs_codes = _parse_event_code_tables(docs_text)

    assert "PAPER_NAV_SNAPSHOT_ERROR" in docs_codes
    assert "PAPER_NAV_SNAPSHOT_ERROR" in DEBUG_EVENT_CODE_CATALOG
    assert DEBUG_EVENT_DEFAULT_SEVERITY["PAPER_NAV_SNAPSHOT_ERROR"] == "HIGH"


def test_severity_vocabulary_exact() -> None:
    allowed = {item.value for item in LogSeverity}
    assert allowed == {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert set(DEBUG_EVENT_DEFAULT_SEVERITY.values()).issubset(allowed)


def test_source_vocabulary_matches_docs() -> None:
    docs_text = DOCS_PATH.read_text(encoding="utf-8")
    docs_sources = _parse_source_table(docs_text)
    assert docs_sources == set(DEBUG_EVENT_SOURCE_VALUES)
