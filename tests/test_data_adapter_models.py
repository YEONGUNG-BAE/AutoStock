from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data.market_data import (
    DisclosureRecord,
    MacroDataPoint,
    MarketDataPoint,
)

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def test_market_data_point_accepts_valid_point() -> None:
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
    assert point.symbol == "AAPL"
    assert point.price == Decimal("190.25")
    assert point.payload["exchange"] == "NASDAQ"


@pytest.mark.parametrize("field_name", ["symbol", "source_name"])
def test_market_data_point_rejects_blank_required_fields(field_name: str) -> None:
    base = {
        "symbol": "AAPL",
        "price": Decimal("1"),
        "source_name": "yfinance",
        "source_timestamp": NOW,
        "as_of": NOW,
    }
    base[field_name] = " "
    with pytest.raises(ValidationError, match="must not be blank"):
        MarketDataPoint(**base)


@pytest.mark.parametrize("field_name", ["market", "currency"])
def test_market_data_point_rejects_blank_optional_fields(field_name: str) -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        MarketDataPoint(
            symbol="AAPL",
            price=Decimal("1"),
            source_name="yfinance",
            source_timestamp=NOW,
            as_of=NOW,
            **{field_name: " "},
        )


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-1")])
def test_market_data_point_rejects_non_positive_price(price: Decimal) -> None:
    with pytest.raises(ValidationError):
        MarketDataPoint(
            symbol="AAPL",
            price=price,
            source_name="yfinance",
            source_timestamp=NOW,
            as_of=NOW,
        )


def test_market_data_point_rejects_nan_price() -> None:
    with pytest.raises(ValidationError, match="finite"):
        MarketDataPoint(
            symbol="AAPL",
            price=Decimal("NaN"),
            source_name="yfinance",
            source_timestamp=NOW,
            as_of=NOW,
        )


def test_market_data_point_rejects_naive_datetimes() -> None:
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        MarketDataPoint(
            symbol="AAPL",
            price=Decimal("1"),
            source_name="yfinance",
            source_timestamp=NAIVE_NOW,
            as_of=NOW,
        )


def test_market_data_point_rejects_invalid_payload() -> None:
    with pytest.raises(ValidationError, match="float values are not allowed"):
        MarketDataPoint(
            symbol="AAPL",
            price=Decimal("1"),
            source_name="yfinance",
            source_timestamp=NOW,
            as_of=NOW,
            payload={"bad": 1.5},
        )


def test_macro_data_point_accepts_valid_point() -> None:
    point = MacroDataPoint(
        series_id="DGS10",
        value=Decimal("4.25"),
        source_name="fred",
        source_timestamp=NOW,
        as_of=NOW,
        payload={"units": "Percent"},
    )
    assert point.series_id == "DGS10"
    assert point.value == Decimal("4.25")


@pytest.mark.parametrize("field_name", ["series_id", "source_name"])
def test_macro_data_point_rejects_blank_required_fields(field_name: str) -> None:
    base = {
        "series_id": "DGS10",
        "value": Decimal("1"),
        "source_name": "fred",
        "source_timestamp": NOW,
        "as_of": NOW,
    }
    base[field_name] = " "
    with pytest.raises(ValidationError, match="must not be blank"):
        MacroDataPoint(**base)


def test_macro_data_point_accepts_zero_value() -> None:
    point = MacroDataPoint(
        series_id="T10Y2Y",
        value=Decimal("0"),
        source_name="fred",
        source_timestamp=NOW,
        as_of=NOW,
    )
    assert point.value == Decimal("0")


def test_macro_data_point_accepts_negative_value() -> None:
    point = MacroDataPoint(
        series_id="REALYIELD",
        value=Decimal("-1.25"),
        source_name="fred",
        source_timestamp=NOW,
        as_of=NOW,
    )
    assert point.value == Decimal("-1.25")


def test_macro_data_point_rejects_nan_value() -> None:
    with pytest.raises(ValidationError, match="finite"):
        MacroDataPoint(
            series_id="DGS10",
            value=Decimal("NaN"),
            source_name="fred",
            source_timestamp=NOW,
            as_of=NOW,
        )


def test_macro_data_point_rejects_infinity_value() -> None:
    with pytest.raises(ValidationError, match="finite"):
        MacroDataPoint(
            series_id="DGS10",
            value=Decimal("Infinity"),
            source_name="fred",
            source_timestamp=NOW,
            as_of=NOW,
        )


def test_disclosure_record_accepts_valid_record() -> None:
    record = DisclosureRecord(
        symbol="005930",
        title="분기보고서",
        source_name="dart",
        source_timestamp=NOW,
        as_of=NOW,
        source_url="https://dart.example/report",
        payload={"rcept_no": "20260001"},
    )
    assert record.title == "분기보고서"
    assert record.source_url == "https://dart.example/report"


@pytest.mark.parametrize("field_name", ["symbol", "title", "source_name"])
def test_disclosure_record_rejects_blank_required_fields(field_name: str) -> None:
    base = {
        "symbol": "005930",
        "title": "분기보고서",
        "source_name": "dart",
        "source_timestamp": NOW,
        "as_of": NOW,
    }
    base[field_name] = " "
    with pytest.raises(ValidationError, match="must not be blank"):
        DisclosureRecord(**base)


def test_disclosure_record_rejects_naive_source_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        DisclosureRecord(
            symbol="005930",
            title="분기보고서",
            source_name="dart",
            source_timestamp=NAIVE_NOW,
            as_of=NOW,
        )
