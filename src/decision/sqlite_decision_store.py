from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from decision.canonical_json import canonical_json_dumps
from domain._datetime import require_timezone_aware_datetime
from domain.decision import DecisionSnapshot
from domain.identifiers import DecisionId
from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity


class DuplicateDecisionIdError(Exception):
    """동일 decision_id insert 시 발생한다."""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decision_snapshots (
    decision_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    normalized_payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    validation_result_json TEXT NOT NULL,
    order_intent_ids_json TEXT NOT NULL,
    replay_metadata_json TEXT NOT NULL
);
"""


class SQLiteDecisionStore:
    """DecisionSnapshot 전용 SQLite 저장소. PaperBroker ledger와 분리한다."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        """SQLite 연결을 닫는다."""
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """DecisionSnapshot 쓰기를 transaction 단위로 처리한다."""
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def list_tables(self) -> tuple[str, ...]:
        """디버그/테스트용 테이블 목록을 반환한다."""
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        return tuple(row["name"] for row in rows)

    def save_decision_snapshot(self, snapshot: DecisionSnapshot) -> None:
        """DecisionSnapshot을 저장한다. duplicate decision_id는 거부한다."""
        try:
            self._conn.execute(
                """
                INSERT INTO decision_snapshots (
                    decision_id,
                    created_at,
                    schema_name,
                    raw_payload_json,
                    normalized_payload_json,
                    payload_hash,
                    validation_result_json,
                    order_intent_ids_json,
                    replay_metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.decision_id.value,
                    _datetime_to_str(snapshot.created_at),
                    snapshot.schema_name,
                    canonical_json_dumps(snapshot.raw_payload),
                    canonical_json_dumps(snapshot.normalized_payload),
                    snapshot.payload_hash,
                    canonical_json_dumps(snapshot.validation_result.to_canonical_dict()),
                    canonical_json_dumps(list(snapshot.order_intent_ids)),
                    canonical_json_dumps(snapshot.replay_metadata),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateDecisionIdError(
                f"decision_id already exists: {snapshot.decision_id.value}"
            ) from exc

    def get_decision_snapshot(self, decision_id: DecisionId | str) -> DecisionSnapshot | None:
        """decision_id에 해당하는 DecisionSnapshot을 복원한다."""
        normalized_id = _normalize_decision_id_lookup(decision_id)
        row = self._conn.execute(
            "SELECT * FROM decision_snapshots WHERE decision_id = ?",
            (normalized_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_decision_snapshot(row)

    def list_decision_snapshots(
        self,
        schema_name: str | None = None,
    ) -> tuple[DecisionSnapshot, ...]:
        """저장된 DecisionSnapshot 목록을 created_at, decision_id 순으로 반환한다."""
        if schema_name is None:
            rows = self._conn.execute(
                """
                SELECT * FROM decision_snapshots
                ORDER BY created_at ASC, decision_id ASC
                """
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM decision_snapshots
                WHERE schema_name = ?
                ORDER BY created_at ASC, decision_id ASC
                """,
                (schema_name,),
            ).fetchall()
        return tuple(_row_to_decision_snapshot(row) for row in rows)

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()


def _normalize_decision_id_lookup(decision_id: DecisionId | str) -> str:
    if isinstance(decision_id, DecisionId):
        return decision_id.value
    return DecisionId(decision_id).value


def _datetime_to_str(value: datetime) -> str:
    return require_timezone_aware_datetime(value, field_name="created_at").isoformat()


def _str_to_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return require_timezone_aware_datetime(parsed, field_name="created_at")


def _row_to_decision_snapshot(row: sqlite3.Row) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_id=DecisionId(row["decision_id"]),
        created_at=_str_to_datetime(row["created_at"]),
        schema_name=row["schema_name"],
        raw_payload=_loads_json_object(row["raw_payload_json"], field_name="raw_payload_json"),
        normalized_payload=_loads_json_object(
            row["normalized_payload_json"],
            field_name="normalized_payload_json",
        ),
        payload_hash=row["payload_hash"],
        validation_result=_loads_validation_result(row["validation_result_json"]),
        order_intent_ids=tuple(
            _loads_json_array(row["order_intent_ids_json"], field_name="order_intent_ids_json")
        ),
        replay_metadata=_loads_json_object(
            row["replay_metadata_json"],
            field_name="replay_metadata_json",
        ),
    )


def _loads_json_object(raw_json: str, *, field_name: str) -> dict[str, Any]:
    import json

    parsed = json.loads(raw_json)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must contain a JSON object.")
    return parsed


def _loads_json_array(raw_json: str, *, field_name: str) -> list[Any]:
    import json

    parsed = json.loads(raw_json)
    if not isinstance(parsed, list):
        raise ValueError(f"{field_name} must contain a JSON array.")
    return parsed


def _loads_validation_result(raw_json: str) -> ValidationResult:
    payload = _loads_json_object(raw_json, field_name="validation_result_json")
    issues_payload = payload.get("issues", [])
    if not isinstance(issues_payload, list):
        raise ValueError("validation_result_json.issues must be a JSON array.")

    issues = tuple(
        ValidationIssue(
            code=issue["code"],
            message=issue["message"],
            severity=ValidationSeverity(issue["severity"]),
            path=issue.get("path"),
        )
        for issue in issues_payload
    )
    return ValidationResult(
        passed=payload["passed"],
        issues=issues,
        schema_name=payload.get("schema_name"),
        validator_version=payload.get("validator_version"),
    )
