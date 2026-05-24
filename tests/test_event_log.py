from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decision.canonical_json import canonical_json_dumps, canonicalize_payload
from logs import DebugEvent, DebugEventSource, JsonlEventLog, LogSeverity

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _sample_event(*, event_id: str = "debug-event-001") -> DebugEvent:
    return DebugEvent(
        event_id=event_id,
        timestamp_kst=NOW,
        source=DebugEventSource.PAPER_BROKER,
        severity=LogSeverity.HIGH,
        event_code="PAPER_BROKER_SIM_ERROR",
        detail="broker rejected order",
        run_id="paper-loop-001",
        metadata={"reason": "insufficient_cash"},
    )


def test_append_one_event(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    event_log = JsonlEventLog(log_path)

    event_log.append(_sample_event())

    assert log_path.exists()
    assert event_log.list_events() == (_sample_event(),)


def test_append_multiple_order_preserved(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    event_log = JsonlEventLog(log_path)
    first = _sample_event(event_id="debug-001")
    second = _sample_event(event_id="debug-002")

    event_log.append(first)
    event_log.append(second)

    assert event_log.list_events() == (first, second)


def test_duplicate_event_id_allowed_and_order_preserved(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    event_log = JsonlEventLog(log_path)
    first = _sample_event(event_id="debug-dup")
    second = _sample_event(event_id="debug-dup")

    event_log.append(first)
    event_log.append(second)

    restored = event_log.list_events()
    assert len(restored) == 2
    assert restored[0].event_id == restored[1].event_id == "debug-dup"


def test_reopen_restore(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    event = _sample_event()

    JsonlEventLog(log_path).append(event)
    restored = JsonlEventLog(log_path).list_events()

    assert restored == (event,)


def test_invalid_row_raises(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    log_path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSONL row"):
        JsonlEventLog(log_path).list_events()


def test_canonical_json_deterministic(tmp_path: Path) -> None:
    event = _sample_event()
    expected = canonical_json_dumps(event.to_canonical_dict())

    log_path = tmp_path / "events.jsonl"
    JsonlEventLog(log_path).append(event)

    line = log_path.read_text(encoding="utf-8").strip()
    assert line == expected
    assert canonicalize_payload(event.to_canonical_dict()) == canonicalize_payload(
        __import__("json").loads(line)
    )


def test_append_only_previous_rows_preserved(tmp_path: Path) -> None:
    log_path = tmp_path / "events.jsonl"
    event_log = JsonlEventLog(log_path)
    first = _sample_event(event_id="debug-001")
    second = _sample_event(event_id="debug-002")

    event_log.append(first)
    before = log_path.read_text(encoding="utf-8")
    event_log.append(second)
    after = log_path.read_text(encoding="utf-8")

    assert after.startswith(before)
    assert after.count("\n") == 2


def test_missing_file_is_empty_log(tmp_path: Path) -> None:
    log_path = tmp_path / "missing.jsonl"
    assert JsonlEventLog(log_path).list_events() == ()
