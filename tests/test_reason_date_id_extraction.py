from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import DateIdValidator, SQLiteDateIdSourceStore, extract_date_ids_from_reasons
from domain import DateId, DateIdSourceRecord, FactType, StalenessPolicy


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _sample_record(
    *,
    date_id: str = "260522-1",
    source_timestamp: datetime | None = None,
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.PRICE,
        source_name="yfinance",
        source_timestamp=source_timestamp or NOW,
        created_at=NOW,
        summary="sample fact",
        payload={"symbol": "AAPL"},
    )


def _store_with_record(tmp_path: Path, record: DateIdSourceRecord) -> SQLiteDateIdSourceStore:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        store.save_record(record)
    return store


def test_extract_date_ids_from_top_level_reasons() -> None:
    payload = {"reasons": [{"date_id": "260522-1"}]}
    assert extract_date_ids_from_reasons(payload) == (DateId("260522-1"),)


def test_extract_date_ids_from_nested_reasons() -> None:
    payload = {
        "analysis": {
            "reasons": [{"date_id": "260522-2"}],
        },
        "reasons": [{"date_id": "260522-1"}],
    }
    assert extract_date_ids_from_reasons(payload) == (
        DateId("260522-1"),
        DateId("260522-2"),
    )


def test_validate_reason_date_ids_reports_missing_date_id_field(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    validator = DateIdValidator(store, StalenessPolicy())
    payload = {"reasons": [{"reason": "no date id"}]}

    result = validator.validate_reason_date_ids(payload, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_MISSING_FIELD"
    store.close()


def test_validate_reason_date_ids_reports_invalid_date_id(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    validator = DateIdValidator(store, StalenessPolicy())
    payload = {"reasons": [{"date_id": "bad-id"}]}

    result = validator.validate_reason_date_ids(payload, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_INVALID"
    store.close()


def test_validate_reason_date_ids_passes_for_existing_fresh_date_id(tmp_path: Path) -> None:
    store = _store_with_record(tmp_path, _sample_record())
    validator = DateIdValidator(store, StalenessPolicy())
    payload = {"reasons": [{"date_id": "260522-1"}]}

    result = validator.validate_reason_date_ids(payload, now=NOW)

    assert result.passed is True
    store.close()


def test_validate_reason_date_ids_fails_for_stale_reason_date_id(tmp_path: Path) -> None:
    stale_record = _sample_record(source_timestamp=NOW - timedelta(hours=25))
    store = _store_with_record(tmp_path, stale_record)
    validator = DateIdValidator(store, StalenessPolicy())
    payload = {"reasons": [{"date_id": "260522-1"}]}

    result = validator.validate_reason_date_ids(payload, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_STALE"
    store.close()


def test_validate_reason_date_ids_rejects_non_list_reasons_object(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    validator = DateIdValidator(store, StalenessPolicy())
    payload = {"reasons": {"date_id": "260522-1"}}

    result = validator.validate_reason_date_ids(payload, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_INVALID"
    assert "reasons must be a list" in result.issues[0].message
    store.close()


def test_validate_reason_date_ids_rejects_non_list_reasons_string(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    validator = DateIdValidator(store, StalenessPolicy())
    payload = {"reasons": "260522-1"}

    result = validator.validate_reason_date_ids(payload, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_INVALID"
    store.close()


def test_validate_reason_date_ids_rejects_nested_non_list_reasons(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    validator = DateIdValidator(store, StalenessPolicy())
    payload = {"analysis": {"reasons": {"date_id": "260522-1"}}}

    result = validator.validate_reason_date_ids(payload, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_INVALID"
    assert result.issues[0].path == "analysis.reasons"
    store.close()
