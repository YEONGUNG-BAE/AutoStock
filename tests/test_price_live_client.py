from __future__ import annotations

import math
from datetime import UTC, date, datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

sys_path = str(REPO_ROOT / "src")
import sys

if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from data.price_live_client import (
    PriceLiveFetchError,
    _stringify_close_price,
    fetch_live_price_snapshot,
)

KST = timezone.utc
FETCHED_AT = datetime(2026, 5, 30, 13, 0, 0, tzinfo=KST)


class _FakeILoc:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def __getitem__(self, idx: int) -> float:
        return self._values[idx]


class _FakeCloseSeries:
    def __init__(self, values: list[float], indices: list[object]) -> None:
        self._values = values
        self.index = indices
        self.iloc = _FakeILoc(values)

    def dropna(self) -> _FakeCloseSeries:
        return self

    def __len__(self) -> int:
        return len(self._values)


class FakeHistory:
    def __init__(
        self,
        closes: list[float],
        indices: list[object],
        *,
        columns: list[str] | None = None,
    ) -> None:
        self._closes = closes
        self._indices = indices
        self.columns = list(columns) if columns is not None else ["Close"]

    def __len__(self) -> int:
        return len(self._closes)

    @property
    def empty(self) -> bool:
        return len(self._closes) == 0

    def __getitem__(self, key: str) -> _FakeCloseSeries:
        if key == "Close":
            return _FakeCloseSeries(self._closes, self._indices)
        raise KeyError(key)


class FakeTicker:
    def __init__(self, *, history: FakeHistory | None, currency: str | None = None) -> None:
        self._history = history
        self.info = {"currency": currency} if currency else {}

    def history(self, period: str, interval: str) -> FakeHistory | None:
        return self._history


def _default_fake_ticker_factory(_provider_symbol: str) -> FakeTicker:
    return FakeTicker(
        history=FakeHistory(
            closes=[71500.0],
            indices=[datetime(2026, 5, 30, tzinfo=UTC)],
        ),
        currency="KRW",
    )


def test_stringify_close_price_uses_shortest_repr() -> None:
    assert _stringify_close_price(71500.0) == "71500.0"


def test_stringify_close_price_rejects_non_finite() -> None:
    with pytest.raises(PriceLiveFetchError, match="finite"):
        _stringify_close_price(float("nan"))
    with pytest.raises(PriceLiveFetchError, match="finite"):
        _stringify_close_price(float("inf"))


def test_fetch_live_price_snapshot_writes_generic_price_json(tmp_path: Path) -> None:
    snapshot_path = fetch_live_price_snapshot(
        provider_symbol="005930.KS",
        symbol="SYNTH-KR-0001",
        market="KR",
        currency="KRW",
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
        ticker_factory=_default_fake_ticker_factory,
    )

    assert snapshot_path.is_file()
    import json

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["source_key"] == "price"
    assert snapshot["external_service"] == "yfinance"
    assert snapshot["provider_symbol"] == "005930.KS"
    assert snapshot["symbol"] == "SYNTH-KR-0001"
    assert snapshot["market"] == "KR"
    assert snapshot["price"] == "71500.0"
    assert snapshot["currency"] == "KRW"
    assert snapshot["payload"]["provider"] == "yfinance"
    assert len(snapshot["payload"]) <= 8


def test_fetch_live_price_snapshot_naive_index_adds_assumption(tmp_path: Path) -> None:
    def factory(_provider_symbol: str) -> FakeTicker:
        return FakeTicker(
            history=FakeHistory(
                closes=[100.0],
                indices=[date(2026, 5, 30)],
            ),
        )

    snapshot_path = fetch_live_price_snapshot(
        provider_symbol="TEST",
        symbol="SYNTH-KR-0001",
        market="KR",
        currency=None,
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
        ticker_factory=factory,
    )
    import json

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["source_timestamp"] == "2026-05-30T00:00:00+00:00"
    assert snapshot["payload"]["source_timestamp_assumption"] == "naive_or_date_index_as_utc_midnight"


def test_fetch_live_price_snapshot_rejects_empty_history(tmp_path: Path) -> None:
    def factory(_provider_symbol: str) -> FakeTicker:
        return FakeTicker(history=FakeHistory(closes=[], indices=[]))

    with pytest.raises(PriceLiveFetchError, match="empty"):
        fetch_live_price_snapshot(
            provider_symbol="TEST",
            symbol="SYNTH-KR-0001",
            market="KR",
            currency=None,
            snapshot_dir=tmp_path,
            fetched_at=FETCHED_AT,
            ticker_factory=factory,
        )


def test_fetch_live_price_snapshot_rejects_missing_close_column(tmp_path: Path) -> None:
    def factory(_provider_symbol: str) -> FakeTicker:
        return FakeTicker(
            history=FakeHistory(
                closes=[100.0],
                indices=[datetime(2026, 5, 30, tzinfo=UTC)],
                columns=["Open"],
            )
        )

    with pytest.raises(PriceLiveFetchError, match="Close"):
        fetch_live_price_snapshot(
            provider_symbol="TEST",
            symbol="SYNTH-KR-0001",
            market="KR",
            currency=None,
            snapshot_dir=tmp_path,
            fetched_at=FETCHED_AT,
            ticker_factory=factory,
        )


@pytest.mark.parametrize("bad_close", [0.0, -1.0, math.nan])
def test_fetch_live_price_snapshot_rejects_invalid_close(
    tmp_path: Path,
    bad_close: float,
) -> None:
    def factory(_provider_symbol: str) -> FakeTicker:
        return FakeTicker(
            history=FakeHistory(
                closes=[bad_close],
                indices=[datetime(2026, 5, 30, tzinfo=UTC)],
            )
        )

    with pytest.raises(PriceLiveFetchError, match="Close"):
        fetch_live_price_snapshot(
            provider_symbol="TEST",
            symbol="SYNTH-KR-0001",
            market="KR",
            currency=None,
            snapshot_dir=tmp_path,
            fetched_at=FETCHED_AT,
            ticker_factory=factory,
        )
