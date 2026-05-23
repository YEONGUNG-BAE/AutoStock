from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string
from data.market_data import DisclosureRecord

SOURCE_NAME = "dart"


class DartDisclosureAdapter:
    """DART 공시 read-only adapter. client 주입으로 unit test에서 fake client를 사용한다."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def fetch_recent_disclosures(
        self,
        symbol: str,
        *,
        as_of: datetime,
        limit: int = 10,
    ) -> tuple[DisclosureRecord, ...]:
        """client.get_recent_disclosures() 결과를 DisclosureRecord tuple로 변환한다."""
        normalized_symbol = normalize_required_string(symbol, field_name="symbol")
        aware_as_of = require_timezone_aware_datetime(as_of, field_name="as_of")
        if limit <= 0:
            raise ValueError("limit must be greater than 0.")

        raw_items = self._client.get_recent_disclosures(normalized_symbol, limit)
        if raw_items is None:
            return ()

        records: list[DisclosureRecord] = []
        for raw in _iter_mappings(raw_items):
            records.append(_parse_disclosure_record(raw, symbol=normalized_symbol, as_of=aware_as_of))
        return tuple(records)


def _iter_mappings(raw_items: Iterable[Any]) -> Iterable[Mapping[str, Any]]:
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise ValueError("each disclosure item must be a mapping.")
        yield item


def _parse_disclosure_record(
    raw: Mapping[str, Any],
    *,
    symbol: str,
    as_of: datetime,
) -> DisclosureRecord:
    title = raw.get("title")
    if title is None:
        raise ValueError("title is required.")
    normalized_title = normalize_required_string(title, field_name="title")
    source_timestamp = _require_source_timestamp(raw.get("source_timestamp"))
    source_url = _optional_string(raw.get("source_url"), field_name="source_url")
    payload = _extract_extra_payload(raw, reserved_keys={"title", "source_timestamp", "source_url"})

    return DisclosureRecord(
        symbol=symbol,
        title=normalized_title,
        source_name=SOURCE_NAME,
        source_timestamp=source_timestamp,
        as_of=as_of,
        payload=payload,
        source_url=source_url,
    )


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
