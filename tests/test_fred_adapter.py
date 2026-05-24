from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data.fred_adapter import FredMacroAdapter

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


class FakeFredClient:
    """unit test 전용 fake FRED client. 실제 fredapi import/network를 사용하지 않는다."""

    def __init__(self, response: dict[str, object] | None = None) -> None:
        self._response = response or {
            "value": "4.25",
            "source_timestamp": NOW,
            "units": "Percent",
        }

    def get_latest_observation(self, series_id: str) -> dict[str, object]:
        return dict(self._response)


def test_fred_adapter_returns_macro_data_point() -> None:
    adapter = FredMacroAdapter(FakeFredClient())
    point = adapter.fetch_latest_observation("DGS10", as_of=NOW)

    assert point.series_id == "DGS10"
    assert point.value == Decimal("4.25")
    assert point.source_name == "fred"
    assert point.payload["units"] == "Percent"


def test_fred_adapter_rejects_blank_series_id() -> None:
    adapter = FredMacroAdapter(FakeFredClient())
    with pytest.raises(ValueError, match="must not be blank"):
        adapter.fetch_latest_observation(" ", as_of=NOW)


def test_fred_adapter_rejects_missing_value() -> None:
    adapter = FredMacroAdapter(FakeFredClient({"source_timestamp": NOW}))
    with pytest.raises(ValueError, match="value is required"):
        adapter.fetch_latest_observation("DGS10", as_of=NOW)


def test_fred_adapter_accepts_negative_value() -> None:
    adapter = FredMacroAdapter(
        FakeFredClient({"value": "-1.25", "source_timestamp": NOW})
    )
    point = adapter.fetch_latest_observation("REALYIELD", as_of=NOW)
    assert point.value == Decimal("-1.25")


def test_fred_adapter_accepts_zero_value() -> None:
    adapter = FredMacroAdapter(
        FakeFredClient({"value": "0", "source_timestamp": NOW})
    )
    point = adapter.fetch_latest_observation("T10Y2Y", as_of=NOW)
    assert point.value == Decimal("0")


def test_fred_adapter_rejects_nan_value() -> None:
    adapter = FredMacroAdapter(
        FakeFredClient({"value": "NaN", "source_timestamp": NOW})
    )
    with pytest.raises(ValueError, match="finite"):
        adapter.fetch_latest_observation("DGS10", as_of=NOW)


def test_fred_adapter_rejects_infinity_value() -> None:
    adapter = FredMacroAdapter(
        FakeFredClient({"value": "Infinity", "source_timestamp": NOW})
    )
    with pytest.raises(ValueError, match="finite"):
        adapter.fetch_latest_observation("DGS10", as_of=NOW)


def test_fred_adapter_rejects_naive_source_timestamp() -> None:
    adapter = FredMacroAdapter(
        FakeFredClient({"value": "4.25", "source_timestamp": NAIVE_NOW})
    )
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        adapter.fetch_latest_observation("DGS10", as_of=NOW)


def test_fred_adapter_rejects_naive_as_of() -> None:
    adapter = FredMacroAdapter(FakeFredClient())
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        adapter.fetch_latest_observation("DGS10", as_of=NAIVE_NOW)
