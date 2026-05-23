from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import (
    DateIdGenerator,
    DateIdValidator,
    SQLiteDateIdSourceStore,
    market_data_point_to_source_record,
    macro_data_point_to_source_record,
    disclosure_record_to_source_record,
)
from data.dart_adapter import DartDisclosureAdapter
from data.fred_adapter import FredMacroAdapter
from data.market_data import DisclosureRecord, MacroDataPoint, MarketDataPoint
from data.yfinance_adapter import YFinancePriceAdapter
from domain import DateId, FactType, StalenessPolicy

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


class FakeYFinanceClient:
    def __init__(self) -> None:
        self._response = {
            "price": "190.25",
            "source_timestamp": NOW,
            "market": "US",
            "currency": "USD",
        }

    def get_latest_price(self, symbol: str) -> dict[str, object]:
        return dict(self._response)


class FakeFredClient:
    def __init__(self) -> None:
        self._response = {
            "value": "4.25",
            "source_timestamp": NOW,
        }

    def get_latest_observation(self, series_id: str) -> dict[str, object]:
        return dict(self._response)


class FakeDartClient:
    def __init__(self) -> None:
        self._items = [
            {
                "title": "분기보고서",
                "source_timestamp": NOW,
                "source_url": "https://dart.example/report",
            }
        ]

    def get_recent_disclosures(self, symbol: str, limit: int) -> list[dict[str, object]]:
        return self._items[:limit]


def test_market_data_point_to_source_record() -> None:
    point = MarketDataPoint(
        symbol="AAPL",
        market="US",
        price=Decimal("190.25"),
        currency="USD",
        source_name="yfinance",
        source_timestamp=NOW,
        as_of=NOW,
        payload={"exchange": "NASDAQ"},
    )
    record = market_data_point_to_source_record(point, DateId("260522-1"))

    assert record.fact_type == FactType.PRICE
    assert record.created_at == NOW
    assert record.summary == "AAPL latest price 190.25"
    assert record.symbol == "AAPL"
    assert record.market == "US"
    assert record.payload["price"] == "190.25"
    assert record.payload["currency"] == "USD"
    assert record.payload["exchange"] == "NASDAQ"


def test_macro_data_point_to_source_record() -> None:
    point = MacroDataPoint(
        series_id="DGS10",
        value=Decimal("4.25"),
        source_name="fred",
        source_timestamp=NOW,
        as_of=NOW,
        payload={"units": "Percent"},
    )
    record = macro_data_point_to_source_record(point, DateId("260522-2"))

    assert record.fact_type == FactType.MACRO
    assert record.created_at == NOW
    assert record.summary == "DGS10 latest observation 4.25"
    assert record.payload["series_id"] == "DGS10"
    assert record.payload["value"] == "4.25"
    assert record.payload["units"] == "Percent"


def test_disclosure_record_to_source_record() -> None:
    disclosure = DisclosureRecord(
        symbol="005930",
        title="분기보고서",
        source_name="dart",
        source_timestamp=NOW,
        as_of=NOW,
        source_url="https://dart.example/report",
        payload={"rcept_no": "20260001"},
    )
    record = disclosure_record_to_source_record(disclosure, DateId("260522-3"))

    assert record.fact_type == FactType.DISCLOSURE
    assert record.created_at == NOW
    assert record.summary == "분기보고서"
    assert record.symbol == "005930"
    assert record.source_url == "https://dart.example/report"
    assert record.payload["title"] == "분기보고서"
    assert record.payload["source_url"] == "https://dart.example/report"


def test_integration_yfinance_adapter_to_store_and_validator(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    adapter = YFinancePriceAdapter(FakeYFinanceClient())
    generator = DateIdGenerator(store)
    validator = DateIdValidator(store, StalenessPolicy())

    point = adapter.fetch_latest_price("AAPL", as_of=NOW)
    date_id = generator.next_id(point.source_timestamp)
    record = market_data_point_to_source_record(point, date_id)

    with store.transaction():
        store.save_record(record)

    result = validator.validate_date_ids([date_id], now=NOW)
    store.close()

    assert date_id == DateId("260522-1")
    assert result.passed is True


def test_integration_stale_record_returns_date_id_stale(tmp_path: Path) -> None:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    adapter = YFinancePriceAdapter(FakeYFinanceClient())
    generator = DateIdGenerator(store)
    validator = DateIdValidator(store, StalenessPolicy())

    stale_timestamp = NOW - timedelta(hours=25)
    adapter._client._response["source_timestamp"] = stale_timestamp  # type: ignore[attr-defined]

    point = adapter.fetch_latest_price("AAPL", as_of=NOW)
    date_id = generator.next_id(point.source_timestamp)
    record = market_data_point_to_source_record(point, date_id)

    with store.transaction():
        store.save_record(record)

    result = validator.validate_date_ids([date_id], now=NOW)
    store.close()

    assert result.passed is False
    assert any(issue.code == "DATE_ID_STALE" for issue in result.issues)


def test_integration_fred_adapter_to_source_record() -> None:
    adapter = FredMacroAdapter(FakeFredClient())
    point = adapter.fetch_latest_observation("DGS10", as_of=NOW)
    record = macro_data_point_to_source_record(point, DateId("260522-4"))

    assert record.fact_type == FactType.MACRO
    assert record.payload["value"] == "4.25"


def test_integration_dart_adapter_to_source_record() -> None:
    adapter = DartDisclosureAdapter(FakeDartClient())
    records = adapter.fetch_recent_disclosures("005930", as_of=NOW, limit=1)
    source_record = disclosure_record_to_source_record(records[0], DateId("260522-5"))

    assert source_record.fact_type == FactType.DISCLOSURE
    assert source_record.summary == "분기보고서"
