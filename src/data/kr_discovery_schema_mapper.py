from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from data.dart_corp_code_resolver import DartCorpCodeResolverError, normalize_stock_code
from data.kr_discovery_live_client import KrDiscoveryLiveFetchError, fetch_live_kr_discovery_snapshot
from domain._datetime import require_timezone_aware_datetime

# 3G3-5: fixture-first KR discovery source schema mapper. network/env/API key 없음.

StageName = Literal["parse", "map", "snapshot"]

_KR_YFINANCE_SUFFIXES = (".KS", ".KQ")
_SECTOR_SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")
_EXPECTED_SOURCE_FORMAT = "synthetic-provider-v1"
_EXPECTED_MARKET = "KR"
_EXPECTED_CURRENCY = "KRW"

_ROOT_KEYS = frozenset({"source_format", "as_of", "market", "items"})
_ITEM_KEYS = frozenset(
    {
        "stockCode",
        "displayName",
        "corpName",
        "ticker",
        "currency",
        "sectorCode",
        "industryLabel",
        "enabled",
        "eligible",
        "priority",
        "note",
        "lastUpdated",
        "sourceUrl",
    }
)
_CANONICAL_RECORD_KEYS = frozenset(
    {
        "symbol",
        "market",
        "display_name",
        "stock_code",
        "corp_name",
        "yfinance_provider_symbol",
        "currency",
        "sector",
        "industry",
        "enabled",
        "eligible",
        "priority",
        "notes",
        "source_timestamp",
        "source_url",
    }
)

_SECTOR_CODE_TO_SLUG = {
    "SEMICONDUCTORS": "semiconductors",
    "INTERNET": "internet",
}

_INDUSTRY_LABEL_TO_SLUG = {
    "Memory": "memory",
    "Platform": "platform",
    "Fabless": "fabless",
    "Commerce": "commerce",
    "Equipment": "equipment",
}


class KrDiscoverySchemaMappingError(ValueError):
    """KR discovery source schema mapper 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class SyntheticProviderItem:
    """synthetic-provider-v1 단일 item."""

    stock_code: str
    display_name: str
    corp_name: str
    ticker: str
    currency: str
    sector_code: str
    industry_label: str
    enabled: bool
    eligible: bool
    priority: int | None
    note: str | None
    last_updated: datetime
    source_url: str | None


@dataclass(frozen=True)
class SyntheticProviderPayload:
    """synthetic-provider-v1 root payload."""

    source_format: str
    as_of: datetime
    market: str
    items: tuple[SyntheticProviderItem, ...]


def load_synthetic_provider_payload(path: Path) -> SyntheticProviderPayload:
    """로컬 synthetic-provider-v1 JSON payload를 strict schema로 파싱한다."""
    if not path.is_file():
        raise KrDiscoverySchemaMappingError("parse", f"source payload file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KrDiscoverySchemaMappingError("parse", f"invalid source payload JSON: {exc.msg}") from exc

    return parse_synthetic_provider_payload_mapping(raw)


def parse_synthetic_provider_payload_mapping(raw: Mapping[str, Any]) -> SyntheticProviderPayload:
    """in-memory synthetic-provider-v1 payload를 strict schema로 파싱한다."""
    if not isinstance(raw, dict):
        raise KrDiscoverySchemaMappingError("parse", "source payload root must be a JSON object")

    if "corp_code" in raw:
        raise KrDiscoverySchemaMappingError("parse", "corp_code must not appear in source payload root")

    unknown_root = set(raw.keys()) - _ROOT_KEYS
    if unknown_root:
        joined = ", ".join(sorted(unknown_root))
        raise KrDiscoverySchemaMappingError("parse", f"unknown source payload root fields: {joined}")

    source_format = _required_text(raw.get("source_format"), field_name="source_format")
    if source_format != _EXPECTED_SOURCE_FORMAT:
        raise KrDiscoverySchemaMappingError(
            "parse",
            f"source_format must be {_EXPECTED_SOURCE_FORMAT!r}",
        )

    market = _required_text(raw.get("market"), field_name="market")
    if market != _EXPECTED_MARKET:
        raise KrDiscoverySchemaMappingError("parse", f"market must be {_EXPECTED_MARKET!r}")

    as_of = _parse_timezone_aware_datetime(raw.get("as_of"), field_name="as_of")

    items_raw = raw.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise KrDiscoverySchemaMappingError("parse", "items must contain at least one entry")

    items: list[SyntheticProviderItem] = []
    for index, entry_raw in enumerate(items_raw):
        if not isinstance(entry_raw, dict):
            raise KrDiscoverySchemaMappingError("parse", f"items[{index}] must be a JSON object")
        items.append(_parse_item(entry_raw, index=index))

    return SyntheticProviderPayload(
        source_format=source_format,
        as_of=as_of,
        market=market,
        items=tuple(items),
    )


def map_synthetic_provider_payload_to_transport_payload(
    payload: SyntheticProviderPayload,
) -> dict[str, Any]:
    """synthetic-provider-v1 payload → 3G3-4A transport payload `{"records": [...]}`."""
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for index, item in enumerate(payload.items):
        record = _map_item_to_canonical_record(item, market=payload.market, index=index)
        key = (record["market"], record["symbol"])
        if key in seen:
            raise KrDiscoverySchemaMappingError(
                "map",
                f"duplicate discovery record: market={record['market']!r}, symbol={record['symbol']!r}",
            )
        seen.add(key)
        records.append(record)

    return {"records": records}


def map_synthetic_provider_fixture_to_snapshot(
    *,
    source_payload_path: Path,
    snapshot_dir: Path,
    fetched_at: datetime,
    as_of: datetime,
    universe_hint: str,
    external_service: str,
) -> Path:
    """source payload → transport map → 3G3-4A immutable raw discovery snapshot."""
    payload = load_synthetic_provider_payload(source_payload_path)
    transport_payload = map_synthetic_provider_payload_to_transport_payload(payload)

    def transport(_metadata: Mapping[str, str]) -> Mapping[str, Any]:
        return transport_payload

    try:
        require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
        require_timezone_aware_datetime(as_of, field_name="as_of")
    except ValueError as exc:
        raise KrDiscoverySchemaMappingError("snapshot", str(exc)) from exc

    try:
        return fetch_live_kr_discovery_snapshot(
            snapshot_dir=snapshot_dir,
            fetched_at=fetched_at,
            as_of=as_of,
            market=payload.market,
            universe_hint=universe_hint,
            external_service=external_service,
            transport=transport,
        )
    except FileExistsError as exc:
        raise KrDiscoverySchemaMappingError("snapshot", str(exc)) from exc
    except KrDiscoveryLiveFetchError as exc:
        raise KrDiscoverySchemaMappingError("snapshot", exc.message) from exc
    except ValueError as exc:
        raise KrDiscoverySchemaMappingError("snapshot", str(exc)) from exc


def _parse_item(raw: dict[str, Any], *, index: int) -> SyntheticProviderItem:
    prefix = f"items[{index}]"
    if "corp_code" in raw:
        raise KrDiscoverySchemaMappingError("parse", f"{prefix}: corp_code must not appear in source payload")

    unknown = set(raw.keys()) - _ITEM_KEYS
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise KrDiscoverySchemaMappingError("parse", f"{prefix}: unknown fields: {joined}")

    stock_code_raw = _required_text(raw.get("stockCode"), field_name=f"{prefix}.stockCode")
    display_name = _required_text(raw.get("displayName"), field_name=f"{prefix}.displayName")
    corp_name = _required_text(raw.get("corpName"), field_name=f"{prefix}.corpName")
    ticker = _required_text(raw.get("ticker"), field_name=f"{prefix}.ticker")
    currency = _required_text(raw.get("currency"), field_name=f"{prefix}.currency")
    sector_code = _required_text(raw.get("sectorCode"), field_name=f"{prefix}.sectorCode")
    industry_label = _required_text(raw.get("industryLabel"), field_name=f"{prefix}.industryLabel")

    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise KrDiscoverySchemaMappingError("parse", f"{prefix}.enabled must be a boolean")

    eligible = raw.get("eligible")
    if not isinstance(eligible, bool):
        raise KrDiscoverySchemaMappingError("parse", f"{prefix}.eligible must be a boolean")

    priority_raw = raw.get("priority")
    priority: int | None
    if priority_raw is None:
        priority = None
    elif isinstance(priority_raw, bool) or not isinstance(priority_raw, int):
        raise KrDiscoverySchemaMappingError("parse", f"{prefix}.priority must be an integer")
    else:
        priority = priority_raw

    note_raw = raw.get("note")
    note: str | None
    if note_raw is None:
        note = None
    else:
        note = _required_text(note_raw, field_name=f"{prefix}.note")

    last_updated = _parse_timezone_aware_datetime(raw.get("lastUpdated"), field_name=f"{prefix}.lastUpdated")

    source_url_raw = raw.get("sourceUrl")
    source_url: str | None
    if source_url_raw is None:
        source_url = None
    else:
        source_url = _required_text(source_url_raw, field_name=f"{prefix}.sourceUrl")

    try:
        normalize_stock_code(stock_code_raw)
    except DartCorpCodeResolverError as exc:
        raise KrDiscoverySchemaMappingError("parse", f"{prefix}.stockCode: {exc}") from exc

    if currency != _EXPECTED_CURRENCY:
        raise KrDiscoverySchemaMappingError("parse", f"{prefix}.currency must be {_EXPECTED_CURRENCY!r}")

    if not ticker.endswith(_KR_YFINANCE_SUFFIXES):
        raise KrDiscoverySchemaMappingError(
            "parse",
            f"{prefix}.ticker must end with .KS or .KQ",
        )

    return SyntheticProviderItem(
        stock_code=stock_code_raw,
        display_name=display_name,
        corp_name=corp_name,
        ticker=ticker,
        currency=currency,
        sector_code=sector_code,
        industry_label=industry_label,
        enabled=enabled,
        eligible=eligible,
        priority=priority,
        note=note,
        last_updated=last_updated,
        source_url=source_url,
    )


def _map_item_to_canonical_record(
    item: SyntheticProviderItem,
    *,
    market: str,
    index: int,
) -> dict[str, Any]:
    prefix = f"items[{index}]"

    try:
        normalized_stock_code = normalize_stock_code(item.stock_code)
    except DartCorpCodeResolverError as exc:
        raise KrDiscoverySchemaMappingError("map", f"{prefix}.stockCode: {exc}") from exc

    sector = _SECTOR_CODE_TO_SLUG.get(item.sector_code)
    if sector is None:
        raise KrDiscoverySchemaMappingError(
            "map",
            f"{prefix}.sectorCode has unknown sector code",
        )
    _validate_sector_slug(sector, field_name=f"{prefix}.sector")

    industry = _INDUSTRY_LABEL_TO_SLUG.get(item.industry_label)
    if industry is None:
        raise KrDiscoverySchemaMappingError(
            "map",
            f"{prefix}.industryLabel has unknown industry label",
        )

    record: dict[str, Any] = {
        "symbol": normalized_stock_code,
        "market": market,
        "display_name": item.display_name,
        "stock_code": normalized_stock_code,
        "corp_name": item.corp_name,
        "yfinance_provider_symbol": item.ticker,
        "currency": item.currency,
        "sector": sector,
        "industry": industry,
        "enabled": item.enabled,
        "eligible": item.eligible,
        "priority": item.priority,
        "notes": item.note,
        "source_timestamp": item.last_updated.isoformat(),
        "source_url": item.source_url,
    }

    unknown = set(record.keys()) - _CANONICAL_RECORD_KEYS
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise KrDiscoverySchemaMappingError("map", f"{prefix}: canonical record has unexpected fields: {joined}")

    return record


def _validate_sector_slug(value: str, *, field_name: str) -> None:
    if value != value.lower():
        raise KrDiscoverySchemaMappingError("map", f"{field_name} must be lower-case ASCII slug")
    if not _SECTOR_SLUG_PATTERN.match(value):
        raise KrDiscoverySchemaMappingError(
            "map",
            f"{field_name} must contain only [a-z0-9_-] characters",
        )


def _contains_control_character(value: str) -> bool:
    """ASCII control(0x00–0x1F) 및 DEL(0x7F) 포함 여부."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _required_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise KrDiscoverySchemaMappingError("parse", f"{field_name} is required")
    if not isinstance(value, str):
        raise KrDiscoverySchemaMappingError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrDiscoverySchemaMappingError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrDiscoverySchemaMappingError("parse", f"{field_name} contains a control character")
    return normalized


def _parse_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise KrDiscoverySchemaMappingError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrDiscoverySchemaMappingError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrDiscoverySchemaMappingError("parse", f"{field_name} contains a control character")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise KrDiscoverySchemaMappingError("parse", f"{field_name} must be ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise KrDiscoverySchemaMappingError("parse", f"{field_name} must be timezone-aware")
    return parsed
