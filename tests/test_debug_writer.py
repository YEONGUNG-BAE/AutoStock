from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decision.canonical_json import canonical_json_dumps
from logs import DebugEvent, DebugEventSource, DebugMarkdownWriter, LogSeverity

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _sample_event(**overrides: object) -> DebugEvent:
    base = {
        "event_id": "debug-md-001",
        "timestamp_kst": NOW,
        "source": DebugEventSource.ALLOCATOR,
        "severity": LogSeverity.HIGH,
        "event_code": "LLM_SCHEMA_ERROR",
        "detail": "schema validation failed",
        "run_id": "allocator-run-001",
        "validation_issue_codes": ("ALLOCATOR_SCHEMA_INVALID",),
        "metadata": {"schema_name": "allocator.v1", "attempt": 2},
    }
    base.update(overrides)
    return DebugEvent(**base)


def test_append_event_creates_file(tmp_path: Path) -> None:
    debug_path = tmp_path / "Debug.md"
    writer = DebugMarkdownWriter(debug_path)

    writer.append_event(_sample_event())

    assert debug_path.exists()
    content = debug_path.read_text(encoding="utf-8")
    assert "LLM_SCHEMA_ERROR" in content
    assert "debug-md-001" in content


def test_heading_is_exactly_event_id(tmp_path: Path) -> None:
    rendered = DebugMarkdownWriter(tmp_path / "Debug.md").render_event(_sample_event())
    assert rendered.startswith("## debug-md-001\n")


def test_canonical_bullet_keys_present(tmp_path: Path) -> None:
    rendered = DebugMarkdownWriter(tmp_path / "Debug.md").render_event(_sample_event())
    for key in ("timestamp_kst:", "source:", "severity:", "event_code:", "detail:"):
        assert f"- {key}" in rendered


def test_timestamp_rendered_as_kst(tmp_path: Path) -> None:
    rendered = DebugMarkdownWriter(tmp_path / "Debug.md").render_event(_sample_event())
    assert "+09:00" in rendered


def test_appending_preserves_previous_content(tmp_path: Path) -> None:
    debug_path = tmp_path / "Debug.md"
    writer = DebugMarkdownWriter(debug_path)
    first = _sample_event(event_id="debug-md-001")
    second = _sample_event(
        event_id="debug-md-002",
        event_code="PAPER_BROKER_SIM_ERROR",
        detail="broker rejected",
    )

    writer.append_event(first)
    before = debug_path.read_text(encoding="utf-8")
    writer.append_event(second)
    after = debug_path.read_text(encoding="utf-8")

    assert before in after
    assert after.count("## ") == 2


def test_metadata_canonical_rendered(tmp_path: Path) -> None:
    event = _sample_event(metadata={"b": 2, "a": 1})
    rendered = DebugMarkdownWriter(tmp_path / "Debug.md").render_event(event)

    assert '"a":1' in rendered.replace(" ", "")
    assert '"b":2' in rendered.replace(" ", "")
    assert canonical_json_dumps({"a": 1, "b": 2}) in rendered


def test_no_error_tags_field(tmp_path: Path) -> None:
    rendered = DebugMarkdownWriter(tmp_path / "Debug.md").render_event(_sample_event())
    assert "error_tags" not in rendered
    assert "top_error_tags" not in rendered


def test_multiple_events_order_preserved(tmp_path: Path) -> None:
    debug_path = tmp_path / "Debug.md"
    writer = DebugMarkdownWriter(debug_path)
    ids = ["debug-md-001", "debug-md-002", "debug-md-003"]
    for event_id in ids:
        writer.append_event(
            _sample_event(event_id=event_id, detail=f"msg-{event_id}")
        )

    content = debug_path.read_text(encoding="utf-8")
    positions = [content.index(event_id) for event_id in ids]
    assert positions == sorted(positions)
