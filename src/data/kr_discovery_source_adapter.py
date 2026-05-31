from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from data.dart_corp_code_resolver import DartCorpCodeResolverError, normalize_stock_code
from data.kr_candidate_pool import (
    KrCandidatePoolDocument,
    KrCandidatePoolEntry,
    KrCandidatePoolError,
    parse_kr_candidate_pool_toml,
)

StageName = Literal["args", "parse", "write", "validate", "complete"]

_KR_YFINANCE_SUFFIXES = (".KS", ".KQ")
_SECTOR_SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")

_SNAPSHOT_ROOT_KEYS = frozenset(
    {
        "source_key",
        "external_service",
        "snapshot_version",
        "fetched_at",
        "as_of",
        "market",
        "universe_hint",
        "records",
    }
)
_SNAPSHOT_RECORD_KEYS = frozenset(
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


class KrDiscoverySourceAdapterError(ValueError):
    """KR discovery snapshot replay adapter 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class KrDiscoverySnapshotRecord:
    """discovery snapshot record (corp_code 미포함)."""

    symbol: str
    market: str
    display_name: str
    stock_code: str
    corp_name: str
    yfinance_provider_symbol: str
    currency: str
    sector: str
    industry: str
    enabled: bool
    eligible: bool
    priority: int | None
    notes: str | None
    source_timestamp: datetime
    source_url: str | None


@dataclass(frozen=True)
class KrDiscoverySnapshot:
    """KR discovery source snapshot root document."""

    source_key: str
    external_service: str
    snapshot_version: int
    fetched_at: datetime
    as_of: datetime
    market: str
    universe_hint: str
    records: tuple[KrDiscoverySnapshotRecord, ...]


def load_kr_discovery_snapshot(path: Path) -> KrDiscoverySnapshot:
    """로컬 discovery snapshot JSON을 strict schema로 파싱한다."""
    if not path.is_file():
        raise KrDiscoverySourceAdapterError("parse", f"discovery snapshot file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KrDiscoverySourceAdapterError("parse", f"invalid discovery snapshot JSON: {exc.msg}") from exc

    if not isinstance(raw, dict):
        raise KrDiscoverySourceAdapterError("parse", "discovery snapshot root must be a JSON object")

    unknown_root = set(raw.keys()) - _SNAPSHOT_ROOT_KEYS
    if unknown_root:
        joined = ", ".join(sorted(unknown_root))
        raise KrDiscoverySourceAdapterError("parse", f"unknown discovery snapshot root fields: {joined}")

    source_key = _required_text(raw.get("source_key"), field_name="source_key")
    if source_key != "kr_discovery":
        raise KrDiscoverySourceAdapterError("parse", "source_key must be 'kr_discovery'")

    snapshot_version = raw.get("snapshot_version")
    if snapshot_version != 1:
        raise KrDiscoverySourceAdapterError("parse", "snapshot_version must be exactly 1")

    external_service = _required_text(raw.get("external_service"), field_name="external_service")
    universe_hint = _required_text(raw.get("universe_hint"), field_name="universe_hint")

    market = _required_text(raw.get("market"), field_name="market")
    if market != "KR":
        raise KrDiscoverySourceAdapterError("parse", "market must be 'KR'")

    fetched_at = _parse_timezone_aware_datetime(raw.get("fetched_at"), field_name="fetched_at")
    as_of = _parse_timezone_aware_datetime(raw.get("as_of"), field_name="as_of")

    records_raw = raw.get("records")
    if not isinstance(records_raw, list) or not records_raw:
        raise KrDiscoverySourceAdapterError("parse", "records must contain at least one entry")

    records: list[KrDiscoverySnapshotRecord] = []
    seen: set[tuple[str, str]] = set()
    for index, entry_raw in enumerate(records_raw):
        if not isinstance(entry_raw, dict):
            raise KrDiscoverySourceAdapterError("parse", f"records[{index}] must be a JSON object")
        record = _parse_snapshot_record(entry_raw, index=index)
        key = (record.market, record.symbol)
        if key in seen:
            raise KrDiscoverySourceAdapterError(
                "parse",
                f"duplicate discovery record: market={record.market!r}, symbol={record.symbol!r}",
            )
        seen.add(key)
        records.append(record)

    return KrDiscoverySnapshot(
        source_key=source_key,
        external_service=external_service,
        snapshot_version=1,
        fetched_at=fetched_at,
        as_of=as_of,
        market=market,
        universe_hint=universe_hint,
        records=tuple(records),
    )


def discovery_snapshot_to_candidate_pool(
    snapshot: KrDiscoverySnapshot,
    *,
    pool_name: str,
    pool_description: str,
) -> KrCandidatePoolDocument:
    """discovery snapshot → 3G1 sector-tagged candidate pool document."""
    name = _validate_pool_text(pool_name, field_name="pool_name")
    description = _validate_pool_text(pool_description, field_name="pool_description")

    candidates: list[KrCandidatePoolEntry] = []
    for record in snapshot.records:
        candidates.append(
            KrCandidatePoolEntry(
                symbol=record.symbol,
                market=record.market,
                display_name=record.display_name,
                stock_code=record.stock_code,
                corp_name=record.corp_name,
                yfinance_provider_symbol=record.yfinance_provider_symbol,
                currency=record.currency,
                sector=record.sector,
                industry=record.industry,
                enabled=record.enabled,
                eligible=record.eligible,
                priority=record.priority,
                notes=record.notes,
            )
        )

    return KrCandidatePoolDocument(
        version=1,
        name=name,
        description=description,
        base_market="KR",
        candidates=tuple(candidates),
    )


def render_candidate_pool_toml(pool: KrCandidatePoolDocument) -> str:
    """full 3G1 candidate pool TOML schema를 렌더한다."""
    lines = [
        "# KR candidate pool replayed from discovery snapshot (3G3-3).",
        "version = 1",
        f"name = {_toml_string(pool.name)}",
        f"description = {_toml_string(pool.description)}",
        'base_market = "KR"',
        "",
    ]
    for entry in pool.candidates:
        lines.extend(
            [
                "[[candidates]]",
                f"symbol = {_toml_string(entry.symbol)}",
                f"market = {_toml_string(entry.market)}",
                f"display_name = {_toml_string(entry.display_name)}",
                f"stock_code = {_toml_string(entry.stock_code)}",
                f"corp_name = {_toml_string(entry.corp_name)}",
                f"yfinance_provider_symbol = {_toml_string(entry.yfinance_provider_symbol)}",
                f"currency = {_toml_string(entry.currency)}",
                f"sector = {_toml_string(entry.sector)}",
                f"industry = {_toml_string(entry.industry)}",
                f"enabled = {_toml_bool(entry.enabled)}",
                f"eligible = {_toml_bool(entry.eligible)}",
            ]
        )
        if entry.priority is not None:
            lines.append(f"priority = {entry.priority}")
        if entry.notes is not None:
            lines.append(f"notes = {_toml_string(entry.notes)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_candidate_pool_toml(
    pool: KrCandidatePoolDocument,
    out_path: Path,
    *,
    force: bool = False,
) -> None:
    """full candidate pool TOML을 기록하고 3G1 parser로 self-validate한다."""
    if out_path.exists() and not force:
        raise KrDiscoverySourceAdapterError(
            "write",
            f"candidate pool output already exists: {out_path} (use --force to overwrite)",
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_candidate_pool_toml(pool)
    out_path.write_text(rendered, encoding="utf-8")
    try:
        parse_kr_candidate_pool_toml(out_path)
    except KrCandidatePoolError as exc:
        raise KrDiscoverySourceAdapterError("validate", exc.message) from exc


def replay_kr_discovery_snapshot(
    *,
    snapshot_path: Path,
    candidate_pool_out: Path,
    pool_name: str,
    pool_description: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """discovery snapshot → candidate pool TOML replay (local files only)."""
    snapshot = load_kr_discovery_snapshot(snapshot_path)
    effective_description = (
        pool_description
        if pool_description is not None
        else f"Replayed KR discovery candidate pool from {snapshot.external_service}."
    )
    pool = discovery_snapshot_to_candidate_pool(
        snapshot,
        pool_name=pool_name,
        pool_description=effective_description,
    )
    write_candidate_pool_toml(pool, candidate_pool_out, force=force)

    sector_list = sorted({entry.sector for entry in pool.candidates})
    return {
        "status": "ok",
        "stage": "complete",
        "snapshot": str(snapshot_path),
        "candidate_pool_out": str(candidate_pool_out),
        "records_read": len(snapshot.records),
        "candidates_written": len(pool.candidates),
        "pool_name": pool.name,
        "market": snapshot.market,
        "sectors": sector_list,
    }


def _parse_snapshot_record(raw: dict[str, Any], *, index: int) -> KrDiscoverySnapshotRecord:
    prefix = f"records[{index}]"
    if "corp_code" in raw:
        raise KrDiscoverySourceAdapterError(
            "parse",
            f"{prefix}: corp_code must not appear in discovery snapshot",
        )

    unknown = set(raw.keys()) - _SNAPSHOT_RECORD_KEYS
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise KrDiscoverySourceAdapterError("parse", f"{prefix}: unknown fields: {joined}")

    market = _required_text(raw.get("market"), field_name=f"{prefix}.market")
    if market != "KR":
        raise KrDiscoverySourceAdapterError("parse", f"{prefix}.market must be 'KR'")

    display_name = _required_text(raw.get("display_name"), field_name=f"{prefix}.display_name")
    corp_name = _required_text(raw.get("corp_name"), field_name=f"{prefix}.corp_name")
    sector = _required_text(raw.get("sector"), field_name=f"{prefix}.sector")
    _validate_sector_slug(sector, field_name=f"{prefix}.sector")
    industry = _required_text(raw.get("industry"), field_name=f"{prefix}.industry")

    yfinance_provider_symbol = _required_text(
        raw.get("yfinance_provider_symbol"),
        field_name=f"{prefix}.yfinance_provider_symbol",
    )
    if not yfinance_provider_symbol.endswith(_KR_YFINANCE_SUFFIXES):
        raise KrDiscoverySourceAdapterError(
            "parse",
            f"{prefix}.yfinance_provider_symbol must end with .KS or .KQ",
        )

    currency = _required_text(raw.get("currency"), field_name=f"{prefix}.currency")
    if currency != "KRW":
        raise KrDiscoverySourceAdapterError("parse", f"{prefix}.currency must be 'KRW'")

    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise KrDiscoverySourceAdapterError("parse", f"{prefix}.enabled must be a boolean")

    eligible = raw.get("eligible")
    if not isinstance(eligible, bool):
        raise KrDiscoverySourceAdapterError("parse", f"{prefix}.eligible must be a boolean")

    priority_raw = raw.get("priority")
    priority: int | None
    if priority_raw is None:
        priority = None
    elif isinstance(priority_raw, bool) or not isinstance(priority_raw, int):
        raise KrDiscoverySourceAdapterError("parse", f"{prefix}.priority must be an integer")
    else:
        priority = priority_raw

    notes_raw = raw.get("notes")
    notes: str | None
    if notes_raw is None:
        notes = None
    else:
        notes = _required_text(notes_raw, field_name=f"{prefix}.notes")

    source_timestamp = _parse_timezone_aware_datetime(
        raw.get("source_timestamp"),
        field_name=f"{prefix}.source_timestamp",
    )

    source_url_raw = raw.get("source_url")
    source_url: str | None
    if source_url_raw is None:
        source_url = None
    else:
        source_url = _required_text(source_url_raw, field_name=f"{prefix}.source_url")

    try:
        normalized_stock_code = normalize_stock_code(
            _required_text(raw.get("stock_code"), field_name=f"{prefix}.stock_code")
        )
    except DartCorpCodeResolverError as exc:
        raise KrDiscoverySourceAdapterError("parse", f"{prefix}.stock_code: {exc}") from exc

    symbol_raw = _required_text(raw.get("symbol"), field_name=f"{prefix}.symbol")
    try:
        normalized_symbol = normalize_stock_code(symbol_raw)
    except DartCorpCodeResolverError as exc:
        raise KrDiscoverySourceAdapterError("parse", f"{prefix}.symbol: {exc}") from exc

    if normalized_symbol != normalized_stock_code:
        raise KrDiscoverySourceAdapterError(
            "parse",
            f"{prefix}: symbol must match normalized stock_code "
            f"(symbol={normalized_symbol!r}, stock_code={normalized_stock_code!r})",
        )

    return KrDiscoverySnapshotRecord(
        symbol=normalized_symbol,
        market=market,
        display_name=display_name,
        stock_code=normalized_stock_code,
        corp_name=corp_name,
        yfinance_provider_symbol=yfinance_provider_symbol,
        currency=currency,
        sector=sector,
        industry=industry,
        enabled=enabled,
        eligible=eligible,
        priority=priority,
        notes=notes,
        source_timestamp=source_timestamp,
        source_url=source_url,
    )


def _validate_sector_slug(value: str, *, field_name: str) -> None:
    if value != value.lower():
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} must be lower-case ASCII slug")
    if not _SECTOR_SLUG_PATTERN.match(value):
        raise KrDiscoverySourceAdapterError(
            "parse",
            f"{field_name} must contain only [a-z0-9_-] characters",
        )


def _validate_pool_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} contains a control character")
    return normalized


def _parse_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} contains a control character")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} must be ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} must be timezone-aware")
    return parsed


def _contains_control_character(value: str) -> bool:
    """ASCII control(0x00–0x1F) 및 DEL(0x7F) 포함 여부."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _required_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} is required")
    if not isinstance(value, str):
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrDiscoverySourceAdapterError("parse", f"{field_name} contains a control character")
    return normalized


def _toml_string(value: str) -> str:
    if _contains_control_character(value):
        raise KrDiscoverySourceAdapterError("validate", "rendered text contains a control character")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
