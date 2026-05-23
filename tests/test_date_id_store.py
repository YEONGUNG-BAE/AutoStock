from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import DuplicateDateIdError, SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _sample_record(
    *,
    date_id: str = "260522-1",
    fact_type: FactType = FactType.PRICE,
    symbol: str | None = "AAPL",
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=fact_type,
        source_name="yfinance",
        source_timestamp=NOW,
        created_at=NOW,
        summary="sample fact",
        payload={"symbol": symbol} if symbol is not None else {},
        symbol=symbol,
        market="US",
        source_url="https://example.com/fact",
    )


def test_sqlite_date_id_store_creates_table(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    assert "date_id_sources" in store.list_tables()
    store.close()


def test_sqlite_date_id_store_save_and_restore(tmp_path: Path) -> None:
    db_path = tmp_path / "date_ids.db"
    original = _sample_record()

    store = SQLiteDateIdSourceStore(db_path)
    with store.transaction():
        store.save_record(original)
    restored = store.get_record(original.date_id)
    store.close()

    assert restored == original


def test_sqlite_date_id_store_reopen_persists_record(tmp_path: Path) -> None:
    db_path = tmp_path / "date_ids.db"
    original = _sample_record(date_id="260522-2")

    store = SQLiteDateIdSourceStore(db_path)
    with store.transaction():
        store.save_record(original)
    store.close()

    reopened = SQLiteDateIdSourceStore(db_path)
    restored = reopened.get_record(original.date_id)
    reopened.close()

    assert restored == original


def test_sqlite_date_id_store_rejects_duplicate_date_id(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    first = _sample_record(date_id="260522-3")
    second = _sample_record(date_id="260522-3", symbol="MSFT")

    with store.transaction():
        store.save_record(first)

    with pytest.raises(DuplicateDateIdError, match="date_id already exists"):
        with store.transaction():
            store.save_record(second)

    restored = store.get_record(first.date_id)
    store.close()

    assert restored == first


def test_sqlite_date_id_store_list_records_filters_by_fact_type(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    price = _sample_record(date_id="260522-4", fact_type=FactType.PRICE)
    news = _sample_record(date_id="260522-5", fact_type=FactType.NEWS, symbol=None)

    with store.transaction():
        store.save_record(price)
        store.save_record(news)

    all_records = store.list_records()
    news_only = store.list_records(fact_type=FactType.NEWS)
    store.close()

    assert len(all_records) == 2
    assert len(news_only) == 1
    assert news_only[0].date_id == news.date_id


def test_sqlite_date_id_store_rejects_invalid_stored_fact_type(tmp_path: Path) -> None:
    db_path = tmp_path / "date_ids.db"
    store = SQLiteDateIdSourceStore(db_path)
    record = _sample_record(date_id="260522-6")
    with store.transaction():
        store.save_record(record)
    store.close()

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE date_id_sources SET fact_type = ? WHERE date_id = ?",
        ("invalid", record.date_id.value),
    )
    conn.commit()
    conn.close()

    reopened = SQLiteDateIdSourceStore(db_path)
    with pytest.raises(ValueError, match="invalid fact_type"):
        reopened.get_record(record.date_id)
    reopened.close()


def test_sqlite_date_id_store_rejects_invalid_stored_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "date_ids.db"
    store = SQLiteDateIdSourceStore(db_path)
    record = _sample_record(date_id="260522-7")
    with store.transaction():
        store.save_record(record)
    store.close()

    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE date_id_sources SET payload_json = ? WHERE date_id = ?",
        (json.dumps({"bad": 1.5}), record.date_id.value),
    )
    conn.commit()
    conn.close()

    reopened = SQLiteDateIdSourceStore(db_path)
    with pytest.raises(Exception, match="float values are not allowed"):
        reopened.get_record(record.date_id)
    reopened.close()


def test_sqlite_date_id_store_transaction_rolls_back_record_on_failure(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    record = _sample_record(date_id="260522-8")

    with pytest.raises(RuntimeError, match="force rollback"):
        with store.transaction():
            store.save_record(record)
            raise RuntimeError("force rollback")

    assert store.get_record(record.date_id) is None
    store.close()
