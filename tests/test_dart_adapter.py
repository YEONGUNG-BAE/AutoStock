from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data.dart_adapter import DartDisclosureAdapter

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


class FakeDartClient:
    """unit test 전용 fake DART client. 실제 OpenDartReader import/network를 사용하지 않는다."""

    def __init__(self, items: list[dict[str, object]] | None = None) -> None:
        self._items = items if items is not None else [
            {
                "title": "분기보고서",
                "source_timestamp": NOW,
                "source_url": "https://dart.example/report",
                "rcept_no": "20260001",
            }
        ]

    def get_recent_disclosures(self, symbol: str, limit: int) -> list[dict[str, object]]:
        return self._items[:limit]


def test_dart_adapter_returns_disclosure_records() -> None:
    adapter = DartDisclosureAdapter(FakeDartClient())
    records = adapter.fetch_recent_disclosures("005930", as_of=NOW, limit=1)

    assert len(records) == 1
    assert records[0].symbol == "005930"
    assert records[0].title == "분기보고서"
    assert records[0].source_name == "dart"
    assert records[0].payload["rcept_no"] == "20260001"


def test_dart_adapter_rejects_blank_symbol() -> None:
    adapter = DartDisclosureAdapter(FakeDartClient())
    with pytest.raises(ValueError, match="must not be blank"):
        adapter.fetch_recent_disclosures(" ", as_of=NOW)


def test_dart_adapter_rejects_non_positive_limit() -> None:
    adapter = DartDisclosureAdapter(FakeDartClient())
    with pytest.raises(ValueError, match="limit must be greater than 0"):
        adapter.fetch_recent_disclosures("005930", as_of=NOW, limit=0)


def test_dart_adapter_rejects_missing_title() -> None:
    adapter = DartDisclosureAdapter(
        FakeDartClient([{"source_timestamp": NOW}])
    )
    with pytest.raises(ValueError, match="title is required"):
        adapter.fetch_recent_disclosures("005930", as_of=NOW)


def test_dart_adapter_rejects_naive_source_timestamp() -> None:
    adapter = DartDisclosureAdapter(
        FakeDartClient([{"title": "분기보고서", "source_timestamp": NAIVE_NOW}])
    )
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        adapter.fetch_recent_disclosures("005930", as_of=NOW)


def test_dart_adapter_rejects_naive_as_of() -> None:
    adapter = DartDisclosureAdapter(FakeDartClient())
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        adapter.fetch_recent_disclosures("005930", as_of=NAIVE_NOW)


def test_dart_adapter_returns_empty_tuple_for_empty_response() -> None:
    adapter = DartDisclosureAdapter(FakeDartClient([]))
    records = adapter.fetch_recent_disclosures("005930", as_of=NOW)
    assert records == ()
