from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "research" / "fred"
SUCCESS_SNAPSHOT = FIXTURES_DIR / "raw_dgs10_success.json"
MISSING_VALUE_SNAPSHOT = FIXTURES_DIR / "raw_dgs10_missing_value.json"

sys.path.insert(0, str(REPO_ROOT / "src"))

from data.fred_source_fetcher import FredSnapshotReplayFetcher
from domain import FactType

AS_OF = datetime(2026, 5, 29, 9, 0, tzinfo=timezone(timedelta(hours=9)))
SOURCE_TS = datetime(2026, 5, 28, 0, 0, tzinfo=UTC)


def test_fred_replay_success_produces_macro_record() -> None:
    fetcher = FredSnapshotReplayFetcher()
    records = fetcher.normalize_snapshot(
        SUCCESS_SNAPSHOT,
        series_id="DGS10",
        as_of=AS_OF,
        date_id="260529-1",
    )

    assert len(records) == 1
    record = records[0]
    assert record.date_id.value == "260529-1"
    assert record.fact_type == FactType.MACRO
    assert record.source_name == "fred"
    assert record.source_timestamp == SOURCE_TS
    assert record.created_at == AS_OF
    assert record.symbol is None
    assert record.market is None
    assert record.summary == "DGS10 latest observation 4.25"
    assert record.payload["series_id"] == "DGS10"
    assert record.payload["value"] == "4.25"
    assert record.payload["units"] == "Percent"
    assert record.payload["frequency"] == "Daily"


def test_fred_replay_rejects_mismatched_series_id() -> None:
    fetcher = FredSnapshotReplayFetcher()
    with pytest.raises(ValueError, match="snapshot series_id mismatch"):
        fetcher.normalize_snapshot(
            SUCCESS_SNAPSHOT,
            series_id="T10Y2Y",
            as_of=AS_OF,
            date_id="260529-1",
        )


def test_fred_replay_rejects_missing_value() -> None:
    fetcher = FredSnapshotReplayFetcher()
    with pytest.raises(ValueError, match="value is required"):
        fetcher.normalize_snapshot(
            MISSING_VALUE_SNAPSHOT,
            series_id="DGS10",
            as_of=AS_OF,
            date_id="260529-1",
        )


def test_fred_replay_rejects_missing_source_timestamp(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "raw_missing_source_timestamp.json"
    payload = json.loads(SUCCESS_SNAPSHOT.read_text(encoding="utf-8"))
    del payload["observation"]["source_timestamp"]
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    fetcher = FredSnapshotReplayFetcher()
    with pytest.raises(ValueError, match="source_timestamp is required"):
        fetcher.normalize_snapshot(
            snapshot_path,
            series_id="DGS10",
            as_of=AS_OF,
            date_id="260529-1",
        )
