from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "research" / "dart"
SUCCESS_SNAPSHOT = FIXTURES / "raw_synth_dart_success.json"
EMPTY_SNAPSHOT = FIXTURES / "raw_synth_dart_empty.json"
MISMATCHED_SYMBOL_SNAPSHOT = FIXTURES / "raw_synth_dart_mismatched_symbol.json"
MISSING_TITLE_SNAPSHOT = FIXTURES / "raw_synth_dart_missing_title.json"
NAIVE_TIMESTAMP_SNAPSHOT = FIXTURES / "raw_synth_dart_naive_timestamp.json"
NON_LIST_DISCLOSURES_SNAPSHOT = FIXTURES / "raw_synth_dart_non_list_disclosures.json"

KST = timezone(timedelta(hours=9))
AS_OF = datetime(2026, 5, 30, 13, 0, 0, tzinfo=KST)

sys_path = str(REPO_ROOT / "src")
import sys

if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from domain import DateId, DateIdSourceRecord, FactType
from data import SQLiteDateIdSourceStore
from data.dart_source_fetcher import (
    DartDisclosureSnapshotReplayFetcher,
    allocate_date_ids_for_records,
)
from data.market_data import DisclosureRecord


@pytest.fixture
def fetcher() -> DartDisclosureSnapshotReplayFetcher:
    return DartDisclosureSnapshotReplayFetcher()


def _empty_store(tmp_path: Path) -> SQLiteDateIdSourceStore:
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    store.close()
    return SQLiteDateIdSourceStore(store_path)


def _seed_store(tmp_path: Path, *records: DateIdSourceRecord) -> SQLiteDateIdSourceStore:
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    with store.transaction():
        for record in records:
            store.save_record(record)
    store.close()
    return SQLiteDateIdSourceStore(store_path)


def _sample_store_record(*, date_id: str = "260530-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.MANUAL,
        source_name="operator-test",
        source_timestamp=datetime(2026, 5, 30, 9, 0, 0, tzinfo=KST),
        created_at=datetime(2026, 5, 30, 9, 5, 0, tzinfo=KST),
        summary="seed record",
        payload={"note": "seed"},
        symbol="SYNTH-KR-0001",
        market="KR",
    )


def test_success_fixture_produces_two_disclosure_records(
    tmp_path: Path,
    fetcher: DartDisclosureSnapshotReplayFetcher,
) -> None:
    store = _empty_store(tmp_path)
    try:
        records = fetcher.normalize_snapshot(
            SUCCESS_SNAPSHOT,
            symbol="SYNTH-KR-0001",
            as_of=AS_OF,
            store=store,
            limit=10,
        )
    finally:
        store.close()

    assert len(records) == 2
    assert records[0].fact_type == FactType.DISCLOSURE
    assert records[0].source_name == "dart"
    assert records[0].symbol == "SYNTH-KR-0001"
    assert records[0].market is None
    assert records[0].summary == "Synthetic DART disclosure fixture 1"
    assert records[0].source_url == "https://dart.fss.or.kr/example/1"
    assert records[0].payload["receipt_no"] == "202605300001"
    assert records[0].payload["corp_name"] == "Synthetic Corp"
    assert records[0].payload["report_type"] == "fixture"
    assert records[1].summary == "Synthetic DART disclosure fixture 2"
    assert records[0].date_id.value == "260530-1"
    assert records[1].date_id.value == "260530-2"


def test_seeded_store_allocates_next_same_day_sequences(
    tmp_path: Path,
    fetcher: DartDisclosureSnapshotReplayFetcher,
) -> None:
    store = _seed_store(tmp_path, _sample_store_record(date_id="260530-1"))
    try:
        records = fetcher.normalize_snapshot(
            SUCCESS_SNAPSHOT,
            symbol="SYNTH-KR-0001",
            as_of=AS_OF,
            store=store,
            limit=10,
        )
    finally:
        store.close()

    assert [record.date_id.value for record in records] == ["260530-2", "260530-3"]


def test_normalize_does_not_save_records_to_store(
    tmp_path: Path,
    fetcher: DartDisclosureSnapshotReplayFetcher,
) -> None:
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    store.close()

    store = SQLiteDateIdSourceStore(store_path)
    try:
        fetcher.normalize_snapshot(
            SUCCESS_SNAPSHOT,
            symbol="SYNTH-KR-0001",
            as_of=AS_OF,
            store=store,
            limit=10,
        )
        assert store.list_records() == ()
    finally:
        store.close()


def test_empty_disclosures_returns_zero_records(
    tmp_path: Path,
    fetcher: DartDisclosureSnapshotReplayFetcher,
) -> None:
    store = _empty_store(tmp_path)
    try:
        records = fetcher.normalize_snapshot(
            EMPTY_SNAPSHOT,
            symbol="SYNTH-KR-0001",
            as_of=AS_OF,
            store=store,
            limit=10,
        )
    finally:
        store.close()

    assert records == []


def test_limit_one_returns_single_record(
    tmp_path: Path,
    fetcher: DartDisclosureSnapshotReplayFetcher,
) -> None:
    store = _empty_store(tmp_path)
    try:
        records = fetcher.normalize_snapshot(
            SUCCESS_SNAPSHOT,
            symbol="SYNTH-KR-0001",
            as_of=AS_OF,
            store=store,
            limit=1,
        )
    finally:
        store.close()

    assert len(records) == 1
    assert records[0].summary == "Synthetic DART disclosure fixture 1"


@pytest.mark.parametrize(
    ("snapshot", "match"),
    [
        (MISMATCHED_SYMBOL_SNAPSHOT, "symbol mismatch"),
        (MISSING_TITLE_SNAPSHOT, "title is required"),
        (NAIVE_TIMESTAMP_SNAPSHOT, "timezone-aware"),
        (NON_LIST_DISCLOSURES_SNAPSHOT, "JSON array"),
    ],
)
def test_rejects_invalid_snapshots(
    tmp_path: Path,
    fetcher: DartDisclosureSnapshotReplayFetcher,
    snapshot: Path,
    match: str,
) -> None:
    store = _empty_store(tmp_path)
    try:
        with pytest.raises(ValueError, match=match):
            fetcher.normalize_snapshot(
                snapshot,
                symbol="SYNTH-KR-0001",
                as_of=AS_OF,
                store=store,
                limit=10,
            )
    finally:
        store.close()


def test_rejects_non_object_root(
    tmp_path: Path,
    fetcher: DartDisclosureSnapshotReplayFetcher,
) -> None:
    snapshot = tmp_path / "array_root.json"
    snapshot.write_text("[]", encoding="utf-8")
    store = _empty_store(tmp_path)
    try:
        with pytest.raises(ValueError, match="JSON object"):
            fetcher.normalize_snapshot(
                snapshot,
                symbol="SYNTH-KR-0001",
                as_of=AS_OF,
                store=store,
                limit=10,
            )
    finally:
        store.close()


def test_allocate_date_ids_empty_store_starts_at_one(tmp_path: Path) -> None:
    store = _empty_store(tmp_path)
    try:
        records = (
            DisclosureRecord(
                symbol="SYNTH-KR-0001",
                title="a",
                source_name="dart",
                source_timestamp=datetime(2026, 5, 30, 9, 0, 0, tzinfo=KST),
                as_of=AS_OF,
            ),
            DisclosureRecord(
                symbol="SYNTH-KR-0001",
                title="b",
                source_name="dart",
                source_timestamp=datetime(2026, 5, 30, 10, 0, 0, tzinfo=KST),
                as_of=AS_OF,
            ),
        )
        date_ids = allocate_date_ids_for_records(records, store=store)
    finally:
        store.close()

    assert [date_id.value for date_id in date_ids] == ["260530-1", "260530-2"]


def test_allocate_date_ids_seeded_store_continues_sequence(tmp_path: Path) -> None:
    store = _seed_store(tmp_path, _sample_store_record(date_id="260530-1"))
    try:
        records = (
            DisclosureRecord(
                symbol="SYNTH-KR-0001",
                title="a",
                source_name="dart",
                source_timestamp=datetime(2026, 5, 30, 9, 0, 0, tzinfo=KST),
                as_of=AS_OF,
            ),
            DisclosureRecord(
                symbol="SYNTH-KR-0001",
                title="b",
                source_name="dart",
                source_timestamp=datetime(2026, 5, 30, 10, 0, 0, tzinfo=KST),
                as_of=AS_OF,
            ),
        )
        date_ids = allocate_date_ids_for_records(records, store=store)
    finally:
        store.close()

    assert [date_id.value for date_id in date_ids] == ["260530-2", "260530-3"]
