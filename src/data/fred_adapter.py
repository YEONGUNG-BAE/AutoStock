from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from domain._datetime import require_timezone_aware_datetime
from domain._decimal import to_decimal
from domain._strings import normalize_required_string
from data.market_data import MacroDataPoint

SOURCE_NAME = "fred"


class FredMacroAdapter:
    """FRED macro observation read-only adapter. client 주입으로 unit test에서 fake client를 사용한다."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def fetch_latest_observation(self, series_id: str, *, as_of: datetime) -> MacroDataPoint:
        """client.get_latest_observation() 결과를 MacroDataPoint로 변환한다."""
        normalized_series_id = normalize_required_string(series_id, field_name="series_id")
        aware_as_of = require_timezone_aware_datetime(as_of, field_name="as_of")

        raw = self._client.get_latest_observation(normalized_series_id)
        if not isinstance(raw, Mapping):
            raise ValueError("client response must be a mapping.")

        value = _require_positive_decimal(raw.get("value"), field_name="value")
        source_timestamp = _require_source_timestamp(raw.get("source_timestamp"))
        payload = _extract_extra_payload(raw, reserved_keys={"value", "source_timestamp"})

        return MacroDataPoint(
            series_id=normalized_series_id,
            value=value,
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


def _extract_extra_payload(raw: Mapping[str, Any], *, reserved_keys: set[str]) -> dict[str, Any]:
    """client mapping에서 reserved key를 제외한 나머지를 payload dict로 반환한다."""
    return {key: raw[key] for key in raw if key not in reserved_keys}
