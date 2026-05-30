from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from data.fred_http_client import snapshot_filename_for_payload, write_live_snapshot_file
from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string

HISTORY_PERIOD = "5d"
HISTORY_INTERVAL = "1d"
CLOSE_COLUMN = "Close"
EXTERNAL_SERVICE = "yfinance"


class PriceLiveFetchError(Exception):
    """yfinance live price fetch 실패. message에는 raw provider dump를 포함하지 않는다."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def fetch_live_price_snapshot(
    *,
    provider_symbol: str,
    symbol: str,
    market: str,
    currency: str | None,
    snapshot_dir: Path,
    fetched_at: datetime,
    ticker_factory: Callable[[str], Any] | None = None,
) -> Path:
    """yfinance live fetch → immutable generic PRICE snapshot (2A replay 호환). DateIdSourceRecord write 금지."""
    normalized_provider_symbol = normalize_required_string(
        provider_symbol,
        field_name="provider_symbol",
    )
    normalized_symbol = normalize_required_string(symbol, field_name="symbol")
    normalized_market = normalize_required_string(market, field_name="market")
    aware_fetched_at = require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    cli_currency = _optional_currency(currency)

    if ticker_factory is None:
        import yfinance

        ticker_factory = yfinance.Ticker

    ticker = ticker_factory(normalized_provider_symbol)
    try:
        history = ticker.history(period=HISTORY_PERIOD, interval=HISTORY_INTERVAL)
    except Exception as exc:
        raise PriceLiveFetchError(f"yfinance history request failed: {exc}") from exc

    close_value, source_timestamp, timestamp_payload = _extract_last_valid_close(history)
    price_str = _stringify_close_price(close_value)
    resolved_currency = cli_currency or _currency_from_ticker_metadata(ticker)

    payload_extra = {
        "provider": EXTERNAL_SERVICE,
        "provider_symbol": normalized_provider_symbol,
        "close_column": CLOSE_COLUMN,
        "period": HISTORY_PERIOD,
        "interval": HISTORY_INTERVAL,
        **timestamp_payload,
    }
    snapshot_payload: dict[str, Any] = {
        "source_key": "price",
        "external_service": EXTERNAL_SERVICE,
        "provider_symbol": normalized_provider_symbol,
        "symbol": normalized_symbol,
        "market": normalized_market,
        "fetched_at": aware_fetched_at.isoformat(),
        "price": price_str,
        "currency": resolved_currency,
        "source_timestamp": source_timestamp.isoformat(),
        "payload": payload_extra,
    }

    filename = snapshot_filename_for_payload(snapshot_payload, fetched_at=aware_fetched_at)
    snapshot_path = snapshot_dir / filename
    if snapshot_path.exists():
        raise FileExistsError(f"snapshot already exists: {snapshot_path}")
    write_live_snapshot_file(snapshot_path, snapshot_payload)
    return snapshot_path


def _optional_currency(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_required_string(value, field_name="currency")


def _extract_last_valid_close(history: Any) -> tuple[float, datetime, dict[str, Any]]:
    """history DataFrame에서 마지막 유효 Close와 source timestamp를 추출한다."""
    if history is None or getattr(history, "empty", len(history) == 0):
        raise PriceLiveFetchError("yfinance history is empty")
    columns = getattr(history, "columns", None)
    if columns is None or CLOSE_COLUMN not in columns:
        raise PriceLiveFetchError(f"yfinance history missing {CLOSE_COLUMN!r} column")

    close_series = history[CLOSE_COLUMN].dropna()
    if len(close_series) == 0:
        raise PriceLiveFetchError("yfinance history has no valid Close values")

    last_close = close_series.iloc[-1]
    last_index = close_series.index[-1]
    close_float = float(last_close)
    if not math.isfinite(close_float) or close_float <= 0:
        raise PriceLiveFetchError(
            f"yfinance last Close must be finite and > 0, got {last_close!r}"
        )

    source_timestamp, timestamp_payload = _coerce_source_timestamp(last_index)
    return close_float, source_timestamp, timestamp_payload


def _stringify_close_price(close_value: float) -> str:
    """yfinance Close float를 deterministic decimal string으로 변환한다 (R2)."""
    close_float = float(close_value)
    if not math.isfinite(close_float) or close_float <= 0:
        raise PriceLiveFetchError(
            f"yfinance Close must be finite and > 0 for stringification, got {close_value!r}"
        )
    return str(close_float)


def _coerce_source_timestamp(index_value: Any) -> tuple[datetime, dict[str, Any]]:
    """history index timestamp를 timezone-aware datetime으로 변환한다."""
    timestamp_payload: dict[str, Any] = {}
    if hasattr(index_value, "to_pydatetime"):
        ts = index_value.to_pydatetime()
    elif isinstance(index_value, datetime):
        ts = index_value
    elif isinstance(index_value, date):
        ts = datetime(index_value.year, index_value.month, index_value.day)
    else:
        raise PriceLiveFetchError(
            f"unsupported yfinance history index type: {type(index_value).__name__}"
        )

    if ts.tzinfo is not None and ts.utcoffset() is not None:
        return ts, timestamp_payload

    utc_midnight = datetime(ts.year, ts.month, ts.day, tzinfo=UTC)
    timestamp_payload["source_timestamp_assumption"] = "naive_or_date_index_as_utc_midnight"
    return utc_midnight, timestamp_payload


def _currency_from_ticker_metadata(ticker: Any) -> str | None:
    """CLI currency 미지정 시 ticker metadata에서 currency를 best-effort로 추출한다."""
    for attr in ("fast_info", "info"):
        metadata = getattr(ticker, attr, None)
        if metadata is None:
            continue
        try:
            if callable(metadata):
                metadata = metadata()
        except Exception:
            continue
        if not isinstance(metadata, dict):
            continue
        raw_currency = metadata.get("currency")
        if raw_currency is None:
            continue
        try:
            return normalize_required_string(raw_currency, field_name="currency")
        except ValueError:
            continue
    return None
