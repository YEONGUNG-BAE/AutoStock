from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import DateIdValidator, SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, EvidenceRef, FactType, StalenessPolicy


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def _sample_record(
    *,
    date_id: str = "260522-1",
    fact_type: FactType = FactType.PRICE,
    source_timestamp: datetime | None = None,
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=fact_type,
        source_name="yfinance",
        source_timestamp=source_timestamp or NOW,
        created_at=NOW,
        summary="sample fact",
        payload={"symbol": "AAPL"},
    )


def _store_with_records(tmp_path: Path, *records: DateIdSourceRecord) -> SQLiteDateIdSourceStore:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        for record in records:
            store.save_record(record)
    return store


def test_date_id_validator_passes_for_existing_fresh_date_id(tmp_path: Path) -> None:
    store = _store_with_records(tmp_path, _sample_record())
    validator = DateIdValidator(store, StalenessPolicy())

    result = validator.validate_date_ids(["260522-1"], now=NOW)

    assert result.passed is True
    assert result.issues == ()
    assert result.schema_name == "date_id_validation"
    store.close()


def test_date_id_validator_reports_missing_date_id(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    validator = DateIdValidator(store, StalenessPolicy())

    result = validator.validate_date_ids(["260522-99"], now=NOW)

    assert result.passed is False
    assert len(result.issues) == 1
    assert result.issues[0].code == "DATE_ID_MISSING"
    assert "260522-99" in result.issues[0].message
    store.close()


def test_date_id_validator_reports_stale_date_id(tmp_path: Path) -> None:
    stale_record = _sample_record(source_timestamp=NOW - timedelta(hours=25))
    store = _store_with_records(tmp_path, stale_record)
    validator = DateIdValidator(store, StalenessPolicy())

    result = validator.validate_date_ids(["260522-1"], now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_STALE"
    assert "260522-1" in result.issues[0].message
    assert "price" in result.issues[0].message
    store.close()


def test_date_id_validator_reports_future_source_timestamp(tmp_path: Path) -> None:
    future_record = _sample_record(source_timestamp=NOW + timedelta(hours=1))
    store = _store_with_records(tmp_path, future_record)
    validator = DateIdValidator(store, StalenessPolicy())

    result = validator.validate_date_ids(["260522-1"], now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_FUTURE_SOURCE"
    store.close()


def test_date_id_validator_reports_invalid_date_id_string(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    validator = DateIdValidator(store, StalenessPolicy())

    result = validator.validate_date_ids(["bad-id"], now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_INVALID"
    store.close()


def test_date_id_validator_deduplicates_duplicate_input_date_ids(tmp_path: Path) -> None:
    store = _store_with_records(tmp_path, _sample_record())
    validator = DateIdValidator(store, StalenessPolicy())

    result = validator.validate_date_ids(["260522-99", "260522-99"], now=NOW)

    assert result.passed is False
    assert len(result.issues) == 1
    assert result.issues[0].code == "DATE_ID_MISSING"
    store.close()


def test_date_id_validator_issue_ordering_is_deterministic(tmp_path: Path) -> None:
    fresh = _sample_record(date_id="260522-1")
    stale = _sample_record(
        date_id="260522-2",
        source_timestamp=NOW - timedelta(hours=25),
    )
    store = _store_with_records(tmp_path, fresh, stale)
    validator = DateIdValidator(store, StalenessPolicy())

    result = validator.validate_date_ids(["260522-99", "260522-2", "260522-1"], now=NOW)

    assert result.passed is False
    codes = [issue.code for issue in result.issues]
    assert codes == ["DATE_ID_MISSING", "DATE_ID_STALE"]
    store.close()


def test_date_id_validator_validate_evidence_refs_uses_date_id(tmp_path: Path) -> None:
    store = _store_with_records(tmp_path, _sample_record())
    validator = DateIdValidator(store, StalenessPolicy())
    refs = (
        EvidenceRef(reason="price momentum", date_id=DateId("260522-1")),
        EvidenceRef(reason="missing fact", date_id=DateId("260522-99")),
    )

    result = validator.validate_evidence_refs(refs, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_MISSING"
    store.close()


def test_date_id_validator_rejects_naive_now(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    validator = DateIdValidator(store, StalenessPolicy())

    with pytest.raises(ValueError, match="timezone-aware datetime"):
        validator.validate_date_ids(["260522-1"], now=NAIVE_NOW)
    store.close()
