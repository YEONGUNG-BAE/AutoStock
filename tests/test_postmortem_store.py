from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from postmortem.models import (
    PostmortemKind,
    PostmortemMarket,
    PostmortemSource,
    PostmortemTagSummary,
    build_postmortem_id,
)
from postmortem.store import PostmortemRecordStore
from postmortem_fixtures import (
    MAY_END,
    MAY_PERIOD,
    MAY_START,
    W20_END,
    W20_PERIOD,
    W20_START,
    sample_postmortem_record,
)


def test_save_get_roundtrip(tmp_path: Path) -> None:
    store = PostmortemRecordStore(tmp_path / "postmortem.jsonl")
    record = sample_postmortem_record()
    store.save(record)

    loaded = store.get(record.postmortem_id)
    assert loaded is not None
    assert loaded.to_canonical_dict() == record.to_canonical_dict()


def test_list_records_in_write_order(tmp_path: Path) -> None:
    store = PostmortemRecordStore(tmp_path / "postmortem.jsonl")
    first = sample_postmortem_record(
        postmortem_id=build_postmortem_id(
            kind=PostmortemKind.WEEKLY,
            market=PostmortemMarket.KR,
            period=W20_PERIOD,
        ),
    )
    second = sample_postmortem_record(
        market=PostmortemMarket.US,
        postmortem_id=build_postmortem_id(
            kind=PostmortemKind.WEEKLY,
            market=PostmortemMarket.US,
            period=W20_PERIOD,
        ),
        summary="weekly US review summary",
    )
    store.save(first)
    store.save(second)

    records = store.list_records()
    assert [item.postmortem_id for item in records] == [
        first.postmortem_id,
        second.postmortem_id,
    ]


def test_duplicate_postmortem_id_reject(tmp_path: Path) -> None:
    store = PostmortemRecordStore(tmp_path / "postmortem.jsonl")
    record = sample_postmortem_record()
    store.save(record)

    with pytest.raises(ValueError, match="duplicate postmortem_id"):
        store.save(record)


def test_missing_file_returns_empty_iteration(tmp_path: Path) -> None:
    store = PostmortemRecordStore(tmp_path / "missing.jsonl")
    assert store.list_records() == ()
    assert store.get("missing") is None


def test_invalid_jsonl_row_raises_with_line_and_path(tmp_path: Path) -> None:
    path = tmp_path / "postmortem.jsonl"
    path.write_text("{not-json}\n", encoding="utf-8")
    store = PostmortemRecordStore(path)

    with pytest.raises(ValueError, match=r"invalid JSONL row at line 1"):
        tuple(store.iter_records())


def test_json_array_row_reject(tmp_path: Path) -> None:
    path = tmp_path / "postmortem.jsonl"
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    store = PostmortemRecordStore(path)

    with pytest.raises(ValueError, match="row must be a JSON object"):
        tuple(store.iter_records())


def test_filter_by_market_kind_period(tmp_path: Path) -> None:
    store = PostmortemRecordStore(tmp_path / "postmortem.jsonl")

    weekly_kr = sample_postmortem_record()
    weekly_us = sample_postmortem_record(
        market=PostmortemMarket.US,
        postmortem_id=build_postmortem_id(
            kind=PostmortemKind.WEEKLY,
            market=PostmortemMarket.US,
            period=W20_PERIOD,
        ),
        summary="weekly US review summary",
        tag_summary=PostmortemTagSummary(
            market=PostmortemMarket.US,
            period=W20_PERIOD,
            source=PostmortemSource.WEEKLY_POSTMORTEM,
            error_tags={"#정보_과신": 1},
        ),
    )
    monthly_kr = sample_postmortem_record(
        kind=PostmortemKind.MONTHLY,
        period=MAY_PERIOD,
        evaluated_start_date=MAY_START,
        evaluated_end_date=MAY_END,
        postmortem_id=build_postmortem_id(
            kind=PostmortemKind.MONTHLY,
            market=PostmortemMarket.KR,
            period=MAY_PERIOD,
        ),
        summary="monthly KR review summary",
        tag_summary=PostmortemTagSummary(
            market=PostmortemMarket.KR,
            period=MAY_PERIOD,
            source=PostmortemSource.MONTHLY_POSTMORTEM,
            error_tags={"#손절_지연": 1},
        ),
    )

    store.save(weekly_kr)
    store.save(weekly_us)
    store.save(monthly_kr)

    assert len(store.list_records(market=PostmortemMarket.KR)) == 2
    assert len(store.list_records(kind=PostmortemKind.MONTHLY)) == 1
    assert len(store.list_records(period=MAY_PERIOD)) == 1
    assert len(store.list_tag_summaries(market=PostmortemMarket.US)) == 1


def test_deterministic_write_read_roundtrip(tmp_path: Path) -> None:
    store = PostmortemRecordStore(tmp_path / "postmortem.jsonl")
    record = sample_postmortem_record()
    store.save(record)

    raw_lines = (tmp_path / "postmortem.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(raw_lines) == 1
    payload = json.loads(raw_lines[0])
    reloaded = store.get(record.postmortem_id)
    assert reloaded is not None
    assert reloaded.to_canonical_dict() == payload
