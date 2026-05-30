from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "research" / "price"
SUCCESS_SNAPSHOT = FIXTURES / "raw_synth_kr_success.json"
MISSING_PRICE_SNAPSHOT = FIXTURES / "raw_synth_kr_missing_price.json"
NEGATIVE_PRICE_SNAPSHOT = FIXTURES / "raw_synth_kr_negative_price.json"
MISMATCHED_SYMBOL_SNAPSHOT = FIXTURES / "raw_synth_kr_mismatched_symbol.json"
NAIVE_TIMESTAMP_SNAPSHOT = FIXTURES / "raw_synth_kr_naive_timestamp.json"

AS_OF = datetime(2026, 5, 30, 9, 0, 0, tzinfo=timezone.utc)
DATE_ID = "260530-1"

sys_path = str(REPO_ROOT / "src")
import sys

if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from domain.source import FactType
from data.price_source_fetcher import GenericPriceSnapshotReplayFetcher


@pytest.fixture
def fetcher() -> GenericPriceSnapshotReplayFetcher:
    return GenericPriceSnapshotReplayFetcher()


def test_success_fixture_produces_one_price_record(fetcher: GenericPriceSnapshotReplayFetcher) -> None:
    records = fetcher.normalize_snapshot(
        SUCCESS_SNAPSHOT,
        symbol="SYNTH-KR-0001",
        market="KR",
        as_of=AS_OF,
        date_id=DATE_ID,
    )

    assert len(records) == 1
    record = records[0]
    assert record.fact_type == FactType.PRICE
    assert record.source_name == "generic-price-fixture"
    assert record.symbol == "SYNTH-KR-0001"
    assert record.market == "KR"
    assert record.payload["price"] == "800000"
    assert record.payload["currency"] == "KRW"
    assert record.payload["previous_close"] == "799000"


@pytest.mark.parametrize(
    ("snapshot", "symbol", "market", "match"),
    [
        (SUCCESS_SNAPSHOT, "OTHER", "KR", "symbol mismatch"),
        (SUCCESS_SNAPSHOT, "SYNTH-KR-0001", "US", "market mismatch"),
    ],
)
def test_rejects_mismatched_symbol_or_market(
    fetcher: GenericPriceSnapshotReplayFetcher,
    snapshot: Path,
    symbol: str,
    market: str,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        fetcher.normalize_snapshot(
            snapshot,
            symbol=symbol,
            market=market,
            as_of=AS_OF,
            date_id=DATE_ID,
        )


def test_rejects_missing_price(fetcher: GenericPriceSnapshotReplayFetcher) -> None:
    with pytest.raises(ValueError, match="price is required"):
        fetcher.normalize_snapshot(
            MISSING_PRICE_SNAPSHOT,
            symbol="SYNTH-KR-0001",
            market="KR",
            as_of=AS_OF,
            date_id=DATE_ID,
        )


def test_rejects_negative_price(fetcher: GenericPriceSnapshotReplayFetcher) -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        fetcher.normalize_snapshot(
            NEGATIVE_PRICE_SNAPSHOT,
            symbol="SYNTH-KR-0001",
            market="KR",
            as_of=AS_OF,
            date_id=DATE_ID,
        )


def test_rejects_zero_price(tmp_path: Path, fetcher: GenericPriceSnapshotReplayFetcher) -> None:
    snapshot = tmp_path / "zero_price.json"
    snapshot.write_text(
        SUCCESS_SNAPSHOT.read_text(encoding="utf-8").replace('"800000"', '"0"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="greater than 0"):
        fetcher.normalize_snapshot(
            snapshot,
            symbol="SYNTH-KR-0001",
            market="KR",
            as_of=AS_OF,
            date_id=DATE_ID,
        )


def test_rejects_missing_source_timestamp(fetcher: GenericPriceSnapshotReplayFetcher, tmp_path: Path) -> None:
    snapshot = tmp_path / "no_timestamp.json"
    text = SUCCESS_SNAPSHOT.read_text(encoding="utf-8")
    snapshot.write_text(text.replace('"source_timestamp": "2026-05-30T00:00:00+09:00",\n', ""), encoding="utf-8")
    with pytest.raises(ValueError, match="source_timestamp is required"):
        fetcher.normalize_snapshot(
            snapshot,
            symbol="SYNTH-KR-0001",
            market="KR",
            as_of=AS_OF,
            date_id=DATE_ID,
        )


def test_rejects_naive_source_timestamp(fetcher: GenericPriceSnapshotReplayFetcher) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        fetcher.normalize_snapshot(
            NAIVE_TIMESTAMP_SNAPSHOT,
            symbol="SYNTH-KR-0001",
            market="KR",
            as_of=AS_OF,
            date_id=DATE_ID,
        )


def test_rejects_non_object_root(fetcher: GenericPriceSnapshotReplayFetcher, tmp_path: Path) -> None:
    snapshot = tmp_path / "array_root.json"
    snapshot.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        fetcher.normalize_snapshot(
            snapshot,
            symbol="SYNTH-KR-0001",
            market="KR",
            as_of=AS_OF,
            date_id=DATE_ID,
        )


def test_rejects_mismatched_snapshot_symbol(fetcher: GenericPriceSnapshotReplayFetcher) -> None:
    with pytest.raises(ValueError, match="symbol mismatch"):
        fetcher.normalize_snapshot(
            MISMATCHED_SYMBOL_SNAPSHOT,
            symbol="SYNTH-KR-0001",
            market="KR",
            as_of=AS_OF,
            date_id=DATE_ID,
        )
