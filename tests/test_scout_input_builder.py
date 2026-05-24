from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType
from scout import ScoutInputBuilder


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def _sample_record(
    *,
    date_id: str,
    fact_type: FactType = FactType.PRICE,
    source_timestamp: datetime | None = None,
    symbol: str | None = "AAPL",
    summary: str = "sample",
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=fact_type,
        source_name="yfinance",
        source_timestamp=source_timestamp or NOW,
        created_at=NOW,
        summary=summary,
        payload={"symbol": symbol},
        symbol=symbol,
        market="US",
    )


def _store_with_records(tmp_path: Path, *records: DateIdSourceRecord) -> SQLiteDateIdSourceStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        for record in records:
            store.save_record(record)
    return store


def test_builder_creates_scout_input_from_store(tmp_path: Path) -> None:
    record = _sample_record(date_id="260522-1")
    store = _store_with_records(tmp_path, record)
    builder = ScoutInputBuilder(store)

    scout_input = builder.build_input(universe="US", now=NOW)

    assert scout_input.universe == "US"
    assert len(scout_input.records) == 1
    assert scout_input.records[0].date_id.value == "260522-1"
    store.close()


def test_builder_ordering_is_deterministic(tmp_path: Path) -> None:
    older = _sample_record(
        date_id="260522-1",
        source_timestamp=NOW - timedelta(hours=2),
        symbol="AAPL",
    )
    newer = _sample_record(
        date_id="260522-2",
        source_timestamp=NOW - timedelta(hours=1),
        symbol="MSFT",
    )
    store = _store_with_records(tmp_path, older, newer)
    builder = ScoutInputBuilder(store)

    scout_input = builder.build_input(universe="US", now=NOW)

    assert [record.date_id.value for record in scout_input.records] == ["260522-2", "260522-1"]
    store.close()


def test_builder_ordering_same_timestamp_uses_date_id_asc(tmp_path: Path) -> None:
    first = _sample_record(date_id="260522-2", source_timestamp=NOW, symbol="AAPL")
    second = _sample_record(date_id="260522-1", source_timestamp=NOW, symbol="MSFT")
    store = _store_with_records(tmp_path, first, second)
    builder = ScoutInputBuilder(store)

    scout_input = builder.build_input(universe="US", now=NOW)

    assert [record.date_id.value for record in scout_input.records] == ["260522-1", "260522-2"]
    store.close()


def test_builder_fact_type_filter(tmp_path: Path) -> None:
    price = _sample_record(date_id="260522-1", fact_type=FactType.PRICE)
    news = _sample_record(date_id="260522-2", fact_type=FactType.NEWS, symbol="MSFT")
    store = _store_with_records(tmp_path, price, news)
    builder = ScoutInputBuilder(store)

    scout_input = builder.build_input(
        universe="US",
        now=NOW,
        fact_types=(FactType.NEWS,),
    )

    assert len(scout_input.records) == 1
    assert scout_input.records[0].fact_type == FactType.NEWS
    store.close()


def test_builder_symbol_filter_is_case_sensitive_exact_match(tmp_path: Path) -> None:
    aapl = _sample_record(date_id="260522-1", symbol="AAPL")
    msft = _sample_record(date_id="260522-2", symbol="MSFT")
    store = _store_with_records(tmp_path, aapl, msft)
    builder = ScoutInputBuilder(store)

    scout_input = builder.build_input(universe="US", now=NOW, symbols=("aapl",))

    assert scout_input.records == ()
    store.close()


def test_builder_symbol_filter_strips_input_symbols(tmp_path: Path) -> None:
    record = _sample_record(date_id="260522-1", symbol="AAPL")
    store = _store_with_records(tmp_path, record)
    builder = ScoutInputBuilder(store)

    scout_input = builder.build_input(universe="US", now=NOW, symbols=("  AAPL  ",))

    assert len(scout_input.records) == 1
    store.close()


def test_builder_max_records(tmp_path: Path) -> None:
    records = tuple(
        _sample_record(
            date_id=f"260522-{index}",
            source_timestamp=NOW - timedelta(hours=index),
            symbol=f"SYM{index}",
        )
        for index in range(1, 4)
    )
    store = _store_with_records(tmp_path, *records)
    builder = ScoutInputBuilder(store)

    scout_input = builder.build_input(universe="US", now=NOW, max_records=2)

    assert len(scout_input.records) == 2
    assert scout_input.records[0].date_id.value == "260522-1"
    store.close()


def test_builder_rejects_blank_universe(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    builder = ScoutInputBuilder(store)

    with pytest.raises(ValueError, match="must not be blank"):
        builder.build_input(universe=" ", now=NOW)
    store.close()


def test_builder_rejects_naive_now(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    builder = ScoutInputBuilder(store)

    with pytest.raises(ValueError, match="timezone-aware datetime"):
        builder.build_input(universe="US", now=NAIVE_NOW)
    store.close()


def test_builder_rejects_blank_symbol_filter(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    builder = ScoutInputBuilder(store)

    with pytest.raises(ValueError, match="blank"):
        builder.build_input(universe="US", now=NOW, symbols=(" ",))
    store.close()


def test_builder_does_not_write_to_store(tmp_path: Path) -> None:
    record = _sample_record(date_id="260522-1")
    store = _store_with_records(tmp_path, record)
    builder = ScoutInputBuilder(store)

    builder.build_input(universe="US", now=NOW)

    assert store.list_records() == (record,)
    store.close()


def test_builder_insertion_order_does_not_affect_output_order(tmp_path: Path) -> None:
    older = _sample_record(
        date_id="260522-1",
        source_timestamp=NOW - timedelta(hours=2),
        symbol="AAPL",
    )
    newer = _sample_record(
        date_id="260522-2",
        source_timestamp=NOW - timedelta(hours=1),
        symbol="MSFT",
    )
    store_a = _store_with_records(tmp_path / "a", older, newer)
    store_b = _store_with_records(tmp_path / "b", newer, older)
    builder_a = ScoutInputBuilder(store_a)
    builder_b = ScoutInputBuilder(store_b)

    input_a = builder_a.build_input(universe="US", now=NOW)
    input_b = builder_b.build_input(universe="US", now=NOW)

    assert input_a.to_canonical_dict() == input_b.to_canonical_dict()
    store_a.close()
    store_b.close()
