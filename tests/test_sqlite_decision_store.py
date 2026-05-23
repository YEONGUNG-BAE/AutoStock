from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decision import DuplicateDecisionIdError, SQLiteDecisionStore
from domain import DecisionId, DecisionSnapshot, ValidationResult


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _sample_snapshot(*, decision_id: str = "decision-001", schema_name: str = "allocator.v1") -> DecisionSnapshot:
    return DecisionSnapshot.create(
        decision_id=DecisionId(decision_id),
        created_at=NOW,
        schema_name=schema_name,
        raw_payload={"b": 1, "a": {"y": "2", "x": "1"}},
        validation_result=ValidationResult(passed=True, issues=()),
        order_intent_ids=("order-001",),
        replay_metadata={"runner": "unit-test"},
    )


def test_sqlite_decision_store_creates_table(tmp_path: Path) -> None:
    store = SQLiteDecisionStore(tmp_path / "decisions.db")
    assert "decision_snapshots" in store.list_tables()
    store.close()


def test_sqlite_decision_store_save_and_restore(tmp_path: Path) -> None:
    db_path = tmp_path / "decisions.db"
    original = _sample_snapshot()

    store = SQLiteDecisionStore(db_path)
    with store.transaction():
        store.save_decision_snapshot(original)
    restored = store.get_decision_snapshot(original.decision_id)
    store.close()

    assert restored == original


def test_sqlite_decision_store_reopen_persists_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "decisions.db"
    original = _sample_snapshot(decision_id="decision-reopen")

    store = SQLiteDecisionStore(db_path)
    with store.transaction():
        store.save_decision_snapshot(original)
    store.close()

    reopened = SQLiteDecisionStore(db_path)
    restored = reopened.get_decision_snapshot(original.decision_id)
    reopened.close()

    assert restored == original


def test_sqlite_decision_store_rejects_duplicate_decision_id(tmp_path: Path) -> None:
    store = SQLiteDecisionStore(tmp_path / "decisions.db")
    first = _sample_snapshot(decision_id="duplicate-id")
    second = _sample_snapshot(decision_id="duplicate-id")

    with store.transaction():
        store.save_decision_snapshot(first)

    with pytest.raises(DuplicateDecisionIdError, match="decision_id already exists"):
        with store.transaction():
            store.save_decision_snapshot(second)

    restored = store.get_decision_snapshot(first.decision_id)
    store.close()

    assert restored == first


def test_sqlite_decision_store_list_filters_by_schema_name(tmp_path: Path) -> None:
    store = SQLiteDecisionStore(tmp_path / "decisions.db")
    allocator = _sample_snapshot(decision_id="allocator-1", schema_name="allocator.v1")
    analysis = _sample_snapshot(decision_id="analysis-1", schema_name="analysis.v1")

    with store.transaction():
        store.save_decision_snapshot(allocator)
        store.save_decision_snapshot(analysis)

    all_snapshots = store.list_decision_snapshots()
    allocator_only = store.list_decision_snapshots(schema_name="allocator.v1")
    store.close()

    assert len(all_snapshots) == 2
    assert len(allocator_only) == 1
    assert allocator_only[0].decision_id == allocator.decision_id


def test_sqlite_decision_store_transaction_rolls_back_snapshot_on_failure(tmp_path: Path) -> None:
    store = SQLiteDecisionStore(tmp_path / "decisions.db")
    snapshot = _sample_snapshot(decision_id="rollback-test")

    with pytest.raises(RuntimeError, match="force rollback"):
        with store.transaction():
            store.save_decision_snapshot(snapshot)
            raise RuntimeError("force rollback")

    assert store.get_decision_snapshot(snapshot.decision_id) is None
    store.close()


def test_sqlite_decision_store_rejects_invalid_stored_validation_result(tmp_path: Path) -> None:
    db_path = tmp_path / "decisions.db"
    store = SQLiteDecisionStore(db_path)
    snapshot = _sample_snapshot(decision_id="invalid-validation")
    with store.transaction():
        store.save_decision_snapshot(snapshot)
    store.close()

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE decision_snapshots SET validation_result_json = ? WHERE decision_id = ?",
        (
            json.dumps({"passed": False, "issues": []}),
            snapshot.decision_id.value,
        ),
    )
    conn.commit()
    conn.close()

    reopened = SQLiteDecisionStore(db_path)
    with pytest.raises(Exception, match="must contain at least one issue"):
        reopened.get_decision_snapshot(snapshot.decision_id)
    reopened.close()


def test_sqlite_decision_store_rejects_invalid_stored_payload_hash(tmp_path: Path) -> None:
    db_path = tmp_path / "decisions.db"
    store = SQLiteDecisionStore(db_path)
    snapshot = _sample_snapshot(decision_id="invalid-hash")
    with store.transaction():
        store.save_decision_snapshot(snapshot)
    store.close()

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE decision_snapshots SET payload_hash = ? WHERE decision_id = ?",
        ("deadbeef", snapshot.decision_id.value),
    )
    conn.commit()
    conn.close()

    reopened = SQLiteDecisionStore(db_path)
    with pytest.raises(Exception, match="payload_hash must equal payload_sha256"):
        reopened.get_decision_snapshot(snapshot.decision_id)
    reopened.close()
