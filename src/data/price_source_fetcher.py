from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.identifiers import DateId
from domain.source import DateIdSourceRecord, FactType
from data.market_data import MarketDataPoint, market_data_point_to_source_record


class GenericPriceSnapshotReplayFetcher:
    """generic local price snapshot → DateIdSourceRecord replay fetcher (2A). network 호출 없음."""

    source_key = "price"

    @property
    def fact_types(self) -> tuple[FactType, ...]:
        return (FactType.PRICE,)

    def normalize_snapshot(
        self,
        snapshot_path: Path,
        *,
        symbol: str,
        market: str,
        as_of: datetime,
        date_id: str,
    ) -> list[DateIdSourceRecord]:
        """local generic price snapshot JSON을 MarketDataPoint 경유 DateIdSourceRecord로 변환한다."""
        snapshot = _load_snapshot_object(snapshot_path)
        _require_snapshot_source_key(snapshot)
        snapshot_symbol = _require_snapshot_symbol(snapshot)
        snapshot_market = _require_snapshot_market(snapshot)
        requested_symbol = normalize_required_string(symbol, field_name="symbol")
        requested_market = normalize_required_string(market, field_name="market")
        if snapshot_symbol != requested_symbol:
            raise ValueError(
                "snapshot symbol mismatch: "
                f"snapshot has {snapshot_symbol!r}, requested {requested_symbol!r}"
            )
        if snapshot_market != requested_market:
            raise ValueError(
                "snapshot market mismatch: "
                f"snapshot has {snapshot_market!r}, requested {requested_market!r}"
            )

        price = _require_positive_price(snapshot)
        currency = _optional_non_blank_currency(snapshot)
        source_timestamp = _require_parsed_source_timestamp(snapshot)
        payload = _optional_payload_object(snapshot)
        source_name = _resolve_source_name(snapshot)
        aware_as_of = require_timezone_aware_datetime(as_of, field_name="as_of")

        try:
            point = MarketDataPoint(
                symbol=snapshot_symbol,
                market=snapshot_market,
                price=price,
                currency=currency,
                source_name=source_name,
                source_timestamp=source_timestamp,
                as_of=aware_as_of,
                payload=payload,
            )
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

        record = market_data_point_to_source_record(point, DateId(date_id))
        return [record]


def _load_snapshot_object(snapshot_path: Path) -> dict[str, Any]:
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"snapshot not found: {snapshot_path}")
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid snapshot JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("snapshot root must be a JSON object")
    return payload


def _require_snapshot_source_key(snapshot: Mapping[str, Any]) -> None:
    source_key = snapshot.get("source_key")
    if source_key is None:
        raise ValueError("snapshot source_key is required")
    normalized = normalize_required_string(source_key, field_name="source_key")
    if normalized != "price":
        raise ValueError(f"snapshot source_key must be 'price', got {normalized!r}")


def _require_snapshot_symbol(snapshot: Mapping[str, Any]) -> str:
    symbol = snapshot.get("symbol")
    if symbol is None:
        raise ValueError("snapshot symbol is required")
    return normalize_required_string(symbol, field_name="symbol")


def _require_snapshot_market(snapshot: Mapping[str, Any]) -> str:
    market = snapshot.get("market")
    if market is None:
        raise ValueError("snapshot market is required")
    return normalize_required_string(market, field_name="market")


def _require_positive_price(snapshot: Mapping[str, Any]) -> Decimal:
    if "price" not in snapshot or snapshot.get("price") is None:
        raise ValueError("snapshot price is required")
    raw_price = snapshot.get("price")
    try:
        price = Decimal(str(raw_price))
    except Exception as exc:
        raise ValueError(f"snapshot price must be a positive decimal: {raw_price!r}") from exc
    if price <= Decimal("0"):
        raise ValueError(f"snapshot price must be greater than 0, got {price}")
    return price


def _optional_non_blank_currency(snapshot: Mapping[str, Any]) -> str | None:
    currency = snapshot.get("currency")
    if currency is None:
        return None
    return normalize_required_string(currency, field_name="currency")


def _require_parsed_source_timestamp(snapshot: Mapping[str, Any]) -> datetime:
    source_timestamp = snapshot.get("source_timestamp")
    if source_timestamp is None:
        raise ValueError("snapshot source_timestamp is required")
    return parse_timezone_aware_datetime(source_timestamp, field_name="source_timestamp")


def _optional_payload_object(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    payload = snapshot.get("payload", {})
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("snapshot payload must be a JSON object")
    return dict(payload)


def _resolve_source_name(snapshot: Mapping[str, Any]) -> str:
    external_service = snapshot.get("external_service")
    if external_service is None:
        return "price"
    return normalize_required_string(external_service, field_name="external_service")
