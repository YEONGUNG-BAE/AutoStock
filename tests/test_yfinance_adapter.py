from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data.yfinance_adapter import YFinancePriceAdapter

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


class FakeYFinanceClient:
    """unit test 전용 fake yfinance client. 실제 yfinance import/network를 사용하지 않는다."""

    def __init__(self, response: dict[str, object] | None = None) -> None:
        self._response = response or {
            "price": "190.25",
            "source_timestamp": NOW,
            "market": "US",
            "currency": "USD",
            "exchange": "NASDAQ",
        }

    def get_latest_price(self, symbol: str) -> dict[str, object]:
        return dict(self._response)


def test_yfinance_adapter_returns_market_data_point() -> None:
    adapter = YFinancePriceAdapter(FakeYFinanceClient())
    point = adapter.fetch_latest_price("AAPL", as_of=NOW)

    assert point.symbol == "AAPL"
    assert point.price == Decimal("190.25")
    assert point.source_name == "yfinance"
    assert point.payload["exchange"] == "NASDAQ"


def test_yfinance_adapter_rejects_blank_symbol() -> None:
    adapter = YFinancePriceAdapter(FakeYFinanceClient())
    with pytest.raises(ValueError, match="must not be blank"):
        adapter.fetch_latest_price(" ", as_of=NOW)


def test_yfinance_adapter_rejects_naive_as_of() -> None:
    adapter = YFinancePriceAdapter(FakeYFinanceClient())
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        adapter.fetch_latest_price("AAPL", as_of=NAIVE_NOW)


def test_yfinance_adapter_rejects_missing_price() -> None:
    adapter = YFinancePriceAdapter(FakeYFinanceClient({"source_timestamp": NOW}))
    with pytest.raises(ValueError, match="price is required"):
        adapter.fetch_latest_price("AAPL", as_of=NOW)


def test_yfinance_adapter_rejects_naive_source_timestamp() -> None:
    adapter = YFinancePriceAdapter(
        FakeYFinanceClient({"price": "190.25", "source_timestamp": NAIVE_NOW})
    )
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        adapter.fetch_latest_price("AAPL", as_of=NOW)


def test_yfinance_adapter_rejects_non_mapping_client_response() -> None:
    class BadClient:
        def get_latest_price(self, symbol: str) -> list[object]:
            return []

    adapter = YFinancePriceAdapter(BadClient())
    with pytest.raises(ValueError, match="mapping"):
        adapter.fetch_latest_price("AAPL", as_of=NOW)
