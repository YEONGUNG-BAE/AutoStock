from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decision import SQLiteDecisionStore
from domain import DecisionId, DecisionSnapshot, ValidationIssue, ValidationResult, ValidationSeverity


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _validation_result(*, passed: bool = True) -> ValidationResult:
    if passed:
        return ValidationResult(passed=True, issues=())
    return ValidationResult(
        passed=False,
        issues=(
            ValidationIssue(code="SCHEMA", message="invalid field", severity=ValidationSeverity.ERROR),
        ),
    )


def test_replay_same_raw_payload_key_order_produces_same_normalized_hash_and_validation() -> None:
    raw_payload_a = {"b": 1, "a": {"y": "2", "x": "1"}}
    raw_payload_b = {"a": {"x": "1", "y": "2"}, "b": 1}
    validation_result = _validation_result()

    snapshot_a = DecisionSnapshot.create(
        decision_id=DecisionId("decision-a"),
        created_at=NOW,
        schema_name="allocator.v1",
        raw_payload=raw_payload_a,
        validation_result=validation_result,
    )
    snapshot_b = DecisionSnapshot.create(
        decision_id=DecisionId("decision-b"),
        created_at=NOW,
        schema_name="allocator.v1",
        raw_payload=raw_payload_b,
        validation_result=validation_result,
    )

    assert snapshot_a.normalized_payload == snapshot_b.normalized_payload
    assert snapshot_a.payload_hash == snapshot_b.payload_hash
    assert snapshot_a.validation_result == snapshot_b.validation_result


def test_replay_sqlite_roundtrip_preserves_normalized_hash_and_validation(tmp_path: Path) -> None:
    snapshot = DecisionSnapshot.create(
        decision_id=DecisionId("decision-replay"),
        created_at=NOW,
        schema_name="allocator.v1",
        raw_payload={"b": 1, "a": {"y": "2", "x": "1"}},
        validation_result=_validation_result(),
        replay_metadata={"phase": 4},
    )

    store = SQLiteDecisionStore(tmp_path / "decisions.db")
    with store.transaction():
        store.save_decision_snapshot(snapshot)
    restored = store.get_decision_snapshot(snapshot.decision_id)
    store.close()

    assert restored is not None
    assert restored.normalized_payload == snapshot.normalized_payload
    assert restored.payload_hash == snapshot.payload_hash
    assert restored.validation_result == snapshot.validation_result


def test_replay_rejects_invalid_payload_before_persist() -> None:
    with pytest.raises(ValueError, match="set values are not allowed"):
        DecisionSnapshot.create(
            decision_id=DecisionId("decision-invalid-payload"),
            created_at=NOW,
            schema_name="allocator.v1",
            raw_payload={"bad": {1, 2}},  # type: ignore[arg-type]
            validation_result=_validation_result(),
        )


def test_replay_rejects_invalid_validation_result_before_persist() -> None:
    with pytest.raises(ValidationError, match="must contain at least one issue"):
        DecisionSnapshot.create(
            decision_id=DecisionId("decision-invalid-validation"),
            created_at=NOW,
            schema_name="allocator.v1",
            raw_payload={"a": 1},
            validation_result=ValidationResult(passed=False, issues=()),
        )
