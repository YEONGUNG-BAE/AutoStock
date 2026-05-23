from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from domain._datetime import require_timezone_aware_datetime
from domain._decimal import to_decimal
from domain._strings import normalize_required_string
from data.market_data import MarketDataPoint

SOURCE_NAME = "yfinance"


class YFinancePriceAdapter:
    """yfinance read-only 가격 adapter. client 주입으로 unit test에서 fake client를 사용한다."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def fetch_latest_price(self, symbol: str, *, as_of: datetime) -> MarketDataPoint:
        """client.get_latest_price() 결과를 MarketDataPoint로 변환한다."""
        normalized_symbol = normalize_required_string(symbol, field_name="symbol")
        aware_as_of = require_timezone_aware_datetime(as_of, field_name="as_of")

        raw = self._client.get_latest_price(normalized_symbol)
        if not isinstance(raw, Mapping):
            raise ValueError("client response must be a mapping.")

        price = _require_positive_decimal(raw.get("price"), field_name="price")
        source_timestamp = _require_source_timestamp(raw.get("source_timestamp"))
        market = _optional_string(raw.get("market"), field_name="market")
        currency = _optional_string(raw.get("currency"), field_name="currency")
        payload = _extract_extra_payload(raw, reserved_keys={"price", "source_timestamp", "market", "currency"})

        return MarketDataPoint(
            symbol=normalized_symbol,
            market=market,
            price=price,
            currency=currency,
            source_name=SOURCE_NAME,
            source_timestamp=source_timestamp,
            as_of=aware_as_of,
            payload=payload,
        )


def _require_positive_decimal(value: Any, *, field_name: str) -> Decimal:
    if value is None:
        raise ValueError(f"{field_name} is required.")
    parsed = to_decimal(value, field_name=field_name)
    if parsed <= Decimal("0"):
        raise ValueError(f"{field_name} must be greater than 0.")
    return parsed


def _require_source_timestamp(value: Any) -> datetime:
    if value is None:
        raise ValueError("source_timestamp is required.")
    return require_timezone_aware_datetime(value, field_name="source_timestamp")


def _optional_string(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    return normalize_required_string(value, field_name=field_name)


def _extract_extra_payload(raw: Mapping[str, Any], *, reserved_keys: set[str]) -> dict[str, Any]:
    """client mapping에서 reserved key를 제외한 나머지를 payload dict로 반환한다."""
    return {key: raw[key] for key in raw if key not in reserved_keys}
