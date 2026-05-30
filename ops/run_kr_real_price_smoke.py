#!/usr/bin/env python3
"""KR real-company sample universe live PRICE smoke (3E2).

provider mapping registry → yfinance live fetch → immutable generic PRICE snapshot
→ GenericPriceSnapshotReplayFetcher → DateIdSourceRecord JSONL.
network/env/API key/DART disclosure fetch 없음 (yfinance는 price_live_client lazy import).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

StageName = Literal["args", "mapping", "fetch", "snapshot", "normalize", "write", "complete"]

from data.date_id_store import SQLiteDateIdSourceStore
from data.dart_source_fetcher import allocate_date_ids_for_records
from data.price_source_fetcher import GenericPriceSnapshotReplayFetcher
from data.provider_mapping_registry import (
    ProviderMappingError,
    ProviderMappingRegistry,
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from data.research_source_fetcher import write_date_id_source_records_jsonl
from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime
from domain.identifiers import DateId
from domain.source import DateIdSourceRecord
from domain.universe import UniverseDefinition, load_universe_toml


class KrRealPriceSmokeError(Exception):
    """run_kr_real_price_smoke CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class _DateIdSeedRecord:
    """allocate_date_ids_for_records()용 source_timestamp carrier (DART disclosure fetch 아님)."""

    source_timestamp: datetime


@dataclass(frozen=True)
class _FetchedPriceSnapshot:
    symbol: str
    market: str
    provider_symbol: str
    snapshot_path: Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "KR real sample universe live PRICE smoke — yfinance snapshot → generic PRICE replay → JSONL."
        ),
    )
    parser.add_argument("--universe", required=True, help="universe TOML path")
    parser.add_argument(
        "--provider-mapping",
        required=True,
        help="provider mapping registry TOML path",
    )
    parser.add_argument(
        "--store",
        required=True,
        help="SQLite Date-ID store path (seed only; fetch stage does not write records)",
    )
    parser.add_argument(
        "--snapshot-dir",
        required=True,
        help="immutable generic PRICE snapshot output directory",
    )
    parser.add_argument("--out-jsonl", required=True, help="combined DateIdSourceRecord JSONL path")
    parser.add_argument(
        "--as-of",
        required=True,
        help="timezone-aware as_of datetime for normalization (ISO-8601)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite --out-jsonl only; raw snapshots are never overwritten",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _require_path(value: str | None, *, flag: str) -> Path:
    if not value:
        raise KrRealPriceSmokeError("args", f"{flag} is required")
    return Path(value)


def _parse_as_of(value: str) -> datetime:
    try:
        return parse_timezone_aware_datetime(value, field_name="as_of")
    except ValueError as exc:
        raise KrRealPriceSmokeError("args", str(exc)) from exc


def _validate_store_parent_exists(store_path: Path) -> None:
    if not store_path.parent.is_dir():
        raise KrRealPriceSmokeError(
            "args",
            f"store parent directory does not exist: {store_path.parent}",
        )


def _load_universe_and_registry(
    *,
    universe_path: Path,
    provider_mapping_path: Path,
) -> tuple[UniverseDefinition, ProviderMappingRegistry]:
    try:
        universe = load_universe_toml(universe_path)
    except (FileNotFoundError, ValueError) as exc:
        raise KrRealPriceSmokeError("args", str(exc)) from exc

    try:
        registry = load_provider_mapping_toml(provider_mapping_path)
    except (FileNotFoundError, ProviderMappingError) as exc:
        raise KrRealPriceSmokeError("args", str(exc)) from exc

    return universe, registry


def _validate_yfinance_mapping_coverage(
    registry: ProviderMappingRegistry,
    universe: UniverseDefinition,
) -> list[tuple[str, str]]:
    try:
        validate_provider_mappings_cover_universe(
            registry,
            universe,
            require_yfinance=True,
            require_dart=False,
        )
    except ProviderMappingError as exc:
        raise KrRealPriceSmokeError("mapping", str(exc)) from exc

    enabled_kr_symbols: list[tuple[str, str]] = []
    for universe_symbol in universe.enabled_symbols:
        if universe_symbol.market != "KR":
            raise KrRealPriceSmokeError(
                "mapping",
                f"3E2 expects KR-only enabled symbols, got market={universe_symbol.market!r} "
                f"for symbol={universe_symbol.symbol!r}",
            )
        mapping = registry.resolve(
            symbol=universe_symbol.symbol,
            market=universe_symbol.market,
        )
        if not mapping.enabled:
            raise KrRealPriceSmokeError(
                "mapping",
                f"enabled universe symbol mapped to disabled registry entry: "
                f"market={universe_symbol.market!r}, symbol={universe_symbol.symbol!r}",
            )
        if mapping.yfinance is None:
            raise KrRealPriceSmokeError(
                "mapping",
                f"enabled universe symbol missing yfinance provider mapping: "
                f"market={universe_symbol.market!r}, symbol={universe_symbol.symbol!r}",
            )
        provider_symbol = mapping.yfinance.provider_symbol.strip()
        if not provider_symbol:
            raise KrRealPriceSmokeError(
                "mapping",
                f"yfinance provider_symbol must not be blank for "
                f"market={universe_symbol.market!r}, symbol={universe_symbol.symbol!r}",
            )
        enabled_kr_symbols.append((universe_symbol.market, universe_symbol.symbol))

    if not enabled_kr_symbols:
        raise KrRealPriceSmokeError("mapping", "no enabled KR universe symbols to fetch")

    return enabled_kr_symbols


def _fetch_price_snapshots(
    *,
    registry: ProviderMappingRegistry,
    enabled_kr_symbols: list[tuple[str, str]],
    snapshot_dir: Path,
    fetched_at: datetime,
    ticker_factory: Callable[[str], Any] | None,
) -> list[_FetchedPriceSnapshot]:
    from data.price_live_client import PriceLiveFetchError, fetch_live_price_snapshot

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    fetched: list[_FetchedPriceSnapshot] = []

    for market, symbol in enabled_kr_symbols:
        mapping = registry.resolve(symbol=symbol, market=market)
        assert mapping.yfinance is not None
        provider_symbol = mapping.yfinance.provider_symbol
        try:
            snapshot_path = fetch_live_price_snapshot(
                provider_symbol=provider_symbol,
                symbol=mapping.symbol,
                market=mapping.market,
                currency=mapping.yfinance.currency,
                snapshot_dir=snapshot_dir,
                fetched_at=fetched_at,
                ticker_factory=ticker_factory,
            )
        except FileExistsError as exc:
            raise KrRealPriceSmokeError("snapshot", str(exc)) from exc
        except PriceLiveFetchError as exc:
            raise KrRealPriceSmokeError("fetch", exc.message) from exc

        fetched.append(
            _FetchedPriceSnapshot(
                symbol=mapping.symbol,
                market=mapping.market,
                provider_symbol=provider_symbol,
                snapshot_path=snapshot_path,
            )
        )

    return fetched


def _read_snapshot_source_timestamp(snapshot_path: Path) -> datetime:
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KrRealPriceSmokeError("snapshot", f"invalid snapshot JSON: {snapshot_path}") from exc
    if not isinstance(payload, dict):
        raise KrRealPriceSmokeError("snapshot", f"snapshot root must be object: {snapshot_path}")
    raw_timestamp = payload.get("source_timestamp")
    if raw_timestamp is None:
        raise KrRealPriceSmokeError("snapshot", f"snapshot source_timestamp is required: {snapshot_path}")
    try:
        return parse_timezone_aware_datetime(raw_timestamp, field_name="source_timestamp")
    except ValueError as exc:
        raise KrRealPriceSmokeError("snapshot", str(exc)) from exc


def _allocate_price_date_ids(
    *,
    fetched: list[_FetchedPriceSnapshot],
    store_path: Path,
) -> list[DateId]:
    seed_records = [
        _DateIdSeedRecord(source_timestamp=_read_snapshot_source_timestamp(item.snapshot_path))
        for item in fetched
    ]
    store = SQLiteDateIdSourceStore(store_path)
    try:
        return allocate_date_ids_for_records(seed_records, store=store)
    finally:
        store.close()


def _normalize_price_records(
    *,
    fetched: list[_FetchedPriceSnapshot],
    date_ids: list[DateId],
    as_of: datetime,
) -> list[DateIdSourceRecord]:
    fetcher = GenericPriceSnapshotReplayFetcher()
    records: list[DateIdSourceRecord] = []
    for item, date_id in zip(fetched, date_ids, strict=True):
        try:
            normalized = fetcher.normalize_snapshot(
                item.snapshot_path,
                symbol=item.symbol,
                market=item.market,
                as_of=as_of,
                date_id=date_id.value,
            )
        except ValueError as exc:
            raise KrRealPriceSmokeError("normalize", str(exc)) from exc
        records.extend(normalized)
    return records


def run_kr_real_price_smoke(
    *,
    universe_path: Path,
    provider_mapping_path: Path,
    store_path: Path,
    snapshot_dir: Path,
    out_jsonl: Path,
    as_of: datetime,
    force: bool,
    fetched_at: datetime | None = None,
    ticker_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """KR real sample universe enabled symbols에 대해 live PRICE smoke를 실행한다."""
    _validate_store_parent_exists(store_path)
    aware_as_of = require_timezone_aware_datetime(as_of, field_name="as_of")
    effective_fetched_at = (
        require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
        if fetched_at is not None
        else datetime.now(tz=UTC)
    )

    universe, registry = _load_universe_and_registry(
        universe_path=universe_path,
        provider_mapping_path=provider_mapping_path,
    )
    enabled_kr_symbols = _validate_yfinance_mapping_coverage(registry, universe)
    fetched = _fetch_price_snapshots(
        registry=registry,
        enabled_kr_symbols=enabled_kr_symbols,
        snapshot_dir=snapshot_dir,
        fetched_at=effective_fetched_at,
        ticker_factory=ticker_factory,
    )
    date_ids = _allocate_price_date_ids(fetched=fetched, store_path=store_path)
    records = _normalize_price_records(
        fetched=fetched,
        date_ids=date_ids,
        as_of=aware_as_of,
    )

    try:
        write_date_id_source_records_jsonl(out_jsonl, records, force=force)
    except FileExistsError as exc:
        raise KrRealPriceSmokeError("write", str(exc)) from exc

    symbol_summaries = [
        {
            "symbol": item.symbol,
            "market": item.market,
            "provider_symbol": item.provider_symbol,
            "snapshot_path": str(item.snapshot_path),
            "date_id": date_id.value,
        }
        for item, date_id in zip(fetched, date_ids, strict=True)
    ]
    return {
        "status": "ok",
        "stage": "complete",
        "mode": "live-price-smoke",
        "universe": universe.name,
        "provider_mapping": registry.name,
        "records_count": len(records),
        "symbols": symbol_summaries,
        "out_jsonl": str(out_jsonl),
    }


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"KR real price smoke: {status}", file=out)
    for key in (
        "mode",
        "stage",
        "universe",
        "provider_mapping",
        "records_count",
        "symbols",
        "out_jsonl",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    try:
        payload = run_kr_real_price_smoke(
            universe_path=_require_path(args.universe, flag="--universe"),
            provider_mapping_path=_require_path(args.provider_mapping, flag="--provider-mapping"),
            store_path=_require_path(args.store, flag="--store"),
            snapshot_dir=_require_path(args.snapshot_dir, flag="--snapshot-dir"),
            out_jsonl=_require_path(args.out_jsonl, flag="--out-jsonl"),
            as_of=_parse_as_of(args.as_of),
            force=args.force,
        )
    except KrRealPriceSmokeError as exc:
        payload = {
            "status": "error",
            "stage": exc.stage,
            "error": exc.message,
        }
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    _emit_result(payload, as_json=as_json, out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
