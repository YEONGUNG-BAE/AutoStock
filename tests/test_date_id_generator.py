from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import DateIdGenerator, SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def _sample_record(*, date_id: str, source_timestamp: datetime = NOW) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.PRICE,
        source_name="yfinance",
        source_timestamp=source_timestamp,
        created_at=NOW,
        summary="sample",
        payload={"symbol": "AAPL"},
        symbol="AAPL",
    )


def test_date_id_generator_first_id_on_date(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    generator = DateIdGenerator(store)

    generated = generator.next_id_for_date(date(2026, 5, 22))
    store.close()

    assert generated == DateId("260522-1")


def test_date_id_generator_increments_existing_sequence(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        store.save_record(_sample_record(date_id="260522-1"))
        store.save_record(_sample_record(date_id="260522-2"))

    generator = DateIdGenerator(store)
    generated = generator.next_id_for_date(date(2026, 5, 22))
    store.close()

    assert generated == DateId("260522-3")


def test_date_id_generator_resets_sequence_for_different_date(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        store.save_record(_sample_record(date_id="260522-9"))

    generator = DateIdGenerator(store)
    generated = generator.next_id_for_date(date(2026, 5, 23))
    store.close()

    assert generated == DateId("260523-1")


def test_date_id_generator_uses_kst_date_from_source_timestamp(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    generator = DateIdGenerator(store)

    # 2026-05-21 16:00 UTC = 2026-05-22 01:00 KST
    source_timestamp = datetime(2026, 5, 21, 16, 0, tzinfo=UTC)
    generated = generator.next_id(source_timestamp)
    store.close()

    assert generated == DateId("260522-1")


def test_date_id_generator_rejects_naive_source_timestamp(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    generator = DateIdGenerator(store)

    with pytest.raises(ValueError, match="timezone-aware datetime"):
        generator.next_id(NAIVE_NOW)
    store.close()


def test_date_id_generator_requires_save_between_generations(tmp_path: Path) -> None:
    """같은 날짜에 여러 Date-ID를 생성할 때는 generate -> save를 반복해야 한다."""
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    generator = DateIdGenerator(store)
    target_date = date(2026, 5, 22)

    first = generator.next_id_for_date(target_date)
    duplicate_without_save = generator.next_id_for_date(target_date)
    assert first == duplicate_without_save == DateId("260522-1")

    with store.transaction():
        store.save_record(_sample_record(date_id=first.value))

    second = generator.next_id_for_date(target_date)
    store.close()

    assert second == DateId("260522-2")
