from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from decision.canonical_json import canonical_json_dumps
from domain._datetime import require_timezone_aware_datetime
from domain.identifiers import DateId
from domain.source import DateIdSourceRecord, FactType, parse_fact_type


class DuplicateDateIdError(Exception):
    """동일 date_id insert 시 발생한다."""


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS date_id_sources (
    date_id TEXT PRIMARY KEY,
    fact_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    symbol TEXT NULL,
    market TEXT NULL,
    source_url TEXT NULL
);
"""


class SQLiteDateIdSourceStore:
    """DateIdSourceRecord 전용 SQLite 저장소. DecisionSnapshot store와 분리한다."""

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
        """DateIdSourceRecord 쓰기를 transaction 단위로 처리한다."""
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

    def save_record(self, record: DateIdSourceRecord) -> None:
        """DateIdSourceRecord를 INSERT한다. commit은 transaction()이 담당한다."""
        try:
            self._conn.execute(
                """
                INSERT INTO date_id_sources (
                    date_id,
                    fact_type,
                    source_name,
                    source_timestamp,
                    created_at,
                    summary,
                    payload_json,
                    symbol,
                    market,
                    source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.date_id.value,
                    record.fact_type.value,
                    record.source_name,
                    _datetime_to_str(record.source_timestamp),
                    _datetime_to_str(record.created_at),
                    record.summary,
                    canonical_json_dumps(record.payload),
                    record.symbol,
                    record.market,
                    record.source_url,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateDateIdError(
                f"date_id already exists: {record.date_id.value}"
            ) from exc

    def get_record(self, date_id: DateId | str) -> DateIdSourceRecord | None:
        """date_id에 해당하는 DateIdSourceRecord를 복원한다."""
        normalized_id = _normalize_date_id_lookup(date_id)
        row = self._conn.execute(
            "SELECT * FROM date_id_sources WHERE date_id = ?",
            (normalized_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_date_id_source_record(row)

    def list_records(self, fact_type: FactType | None = None) -> tuple[DateIdSourceRecord, ...]:
        """저장된 DateIdSourceRecord 목록을 date_id 순으로 반환한다."""
        if fact_type is None:
            rows = self._conn.execute(
                """
                SELECT * FROM date_id_sources
                ORDER BY date_id ASC
                """
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM date_id_sources
                WHERE fact_type = ?
                ORDER BY date_id ASC
                """,
                (fact_type.value,),
            ).fetchall()
        return tuple(_row_to_date_id_source_record(row) for row in rows)

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()


def _normalize_date_id_lookup(date_id: DateId | str) -> str:
    if isinstance(date_id, DateId):
        return date_id.value
    return DateId(date_id).value


def _datetime_to_str(value: datetime) -> str:
    return require_timezone_aware_datetime(value, field_name="datetime").isoformat()


def _str_to_datetime(value: str, *, field_name: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return require_timezone_aware_datetime(parsed, field_name=field_name)


def _loads_json_object(raw_json: str, *, field_name: str) -> dict[str, Any]:
    import json

    parsed = json.loads(raw_json)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must contain a JSON object.")
    return parsed


def _row_to_date_id_source_record(row: sqlite3.Row) -> DateIdSourceRecord:
    fact_type = parse_fact_type(row["fact_type"])
    payload = _loads_json_object(row["payload_json"], field_name="payload_json")
    return DateIdSourceRecord(
        date_id=DateId(row["date_id"]),
        fact_type=fact_type,
        source_name=row["source_name"],
        source_timestamp=_str_to_datetime(row["source_timestamp"], field_name="source_timestamp"),
        created_at=_str_to_datetime(row["created_at"], field_name="created_at"),
        summary=row["summary"],
        payload=payload,
        symbol=row["symbol"],
        market=row["market"],
        source_url=row["source_url"],
    )
