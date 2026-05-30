#!/usr/bin/env python3
"""KR real-company sample universe live DART disclosure smoke (3E3).

provider mapping registry → DART corp_code → immutable DART snapshot
→ DartSnapshotReplayClient + DartDisclosureAdapter → combined-batch Date-ID
→ DateIdSourceRecord JSONL. yfinance/FRED/broker 경로 없음.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

StageName = Literal["args", "mapping", "fetch", "snapshot", "normalize", "write", "complete"]

from data.dart_adapter import DartDisclosureAdapter
from data.dart_source_fetcher import DartSnapshotReplayClient, allocate_date_ids_for_records
from data.date_id_store import SQLiteDateIdSourceStore
from data.market_data import DisclosureRecord, disclosure_record_to_source_record
from data.provider_mapping_registry import (
    ProviderMappingError,
    ProviderMappingRegistry,
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from data.research_source_fetcher import write_date_id_source_records_jsonl
from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime
from domain.source import DateIdSourceRecord
from domain.universe import UniverseDefinition, load_universe_toml

# DART live-smoke 전용 env 이름. operator CLI default.
DART_DEFAULT_API_KEY_ENV = "DART_API_KEY"


class KrRealDartSmokeError(Exception):
    """run_kr_real_dart_smoke CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class _FetchedDartSnapshot:
    symbol: str
    market: str
    corp_code: str
    snapshot_path: Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "KR real sample universe live DART disclosure smoke — "
            "OpenDART snapshot → adapter replay → combined JSONL."
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
        help="immutable DART snapshot output directory",
    )
    parser.add_argument("--out-jsonl", required=True, help="combined DateIdSourceRecord JSONL path")
    parser.add_argument(
        "--as-of",
        required=True,
        help="timezone-aware as_of datetime for normalization (ISO-8601)",
    )
    parser.add_argument(
        "--bgn-de",
        required=True,
        help="OpenDART search start date YYYYMMDD",
    )
    parser.add_argument(
        "--end-de",
        default=None,
        help="optional OpenDART search end date YYYYMMDD",
    )
    parser.add_argument(
        "--page-count",
        type=int,
        default=100,
        help="OpenDART page_count per symbol (default: 100)",
    )
    parser.add_argument(
        "--api-key-env",
        default=DART_DEFAULT_API_KEY_ENV,
        help="environment variable name for OpenDART API key (value never logged)",
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
        raise KrRealDartSmokeError("args", f"{flag} is required")
    return Path(value)


def _parse_as_of(value: str) -> datetime:
    try:
        return parse_timezone_aware_datetime(value, field_name="as_of")
    except ValueError as exc:
        raise KrRealDartSmokeError("args", str(exc)) from exc


def _read_api_key(env_name: str) -> str:
    if not env_name.strip():
        raise KrRealDartSmokeError("args", "api_key_env must not be blank")
    value = os.environ.get(env_name)
    if not value or not value.strip():
        raise KrRealDartSmokeError(
            "args",
            f"API key not configured (env var {env_name!r} is missing or empty)",
        )
    return value.strip()


def _validate_store_parent_exists(store_path: Path) -> None:
    if not store_path.parent.is_dir():
        raise KrRealDartSmokeError(
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
        raise KrRealDartSmokeError("args", str(exc)) from exc

    try:
        registry = load_provider_mapping_toml(provider_mapping_path)
    except (FileNotFoundError, ProviderMappingError) as exc:
        raise KrRealDartSmokeError("args", str(exc)) from exc

    return universe, registry


def _validate_dart_mapping_coverage(
    registry: ProviderMappingRegistry,
    universe: UniverseDefinition,
) -> list[tuple[str, str]]:
    try:
        validate_provider_mappings_cover_universe(
            registry,
            universe,
            require_yfinance=False,
            require_dart=True,
        )
    except ProviderMappingError as exc:
        raise KrRealDartSmokeError("mapping", str(exc)) from exc

    enabled_kr_symbols: list[tuple[str, str]] = []
    for universe_symbol in universe.enabled_symbols:
        if universe_symbol.market != "KR":
            raise KrRealDartSmokeError(
                "mapping",
                f"3E3 expects KR-only enabled symbols, got market={universe_symbol.market!r} "
                f"for symbol={universe_symbol.symbol!r}",
            )
        mapping = registry.resolve(
            symbol=universe_symbol.symbol,
            market=universe_symbol.market,
        )
        if not mapping.enabled:
            raise KrRealDartSmokeError(
                "mapping",
                f"enabled universe symbol mapped to disabled registry entry: "
                f"market={universe_symbol.market!r}, symbol={universe_symbol.symbol!r}",
            )
        if mapping.dart is None:
            raise KrRealDartSmokeError(
                "mapping",
                f"enabled universe symbol missing dart provider mapping: "
                f"market={universe_symbol.market!r}, symbol={universe_symbol.symbol!r}",
            )
        corp_code = mapping.dart.corp_code.strip()
        if not corp_code:
            raise KrRealDartSmokeError(
                "mapping",
                f"dart corp_code must not be blank for "
                f"market={universe_symbol.market!r}, symbol={universe_symbol.symbol!r}",
            )
        enabled_kr_symbols.append((universe_symbol.market, universe_symbol.symbol))

    if not enabled_kr_symbols:
        raise KrRealDartSmokeError("mapping", "no enabled KR universe symbols to fetch")

    return enabled_kr_symbols


def _fetch_dart_snapshots(
    *,
    registry: ProviderMappingRegistry,
    enabled_kr_symbols: list[tuple[str, str]],
    snapshot_dir: Path,
    fetched_at: datetime,
    bgn_de: str,
    end_de: str | None,
    page_count: int,
    api_key: str,
    api_key_env: str,
    transport: Callable[[Mapping[str, str]], Mapping[str, Any]] | None,
) -> list[_FetchedDartSnapshot]:
    from data.dart_live_client import DartLiveFetchError, fetch_live_dart_snapshot

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    fetched: list[_FetchedDartSnapshot] = []

    for market, symbol in enabled_kr_symbols:
        mapping = registry.resolve(symbol=symbol, market=market)
        assert mapping.dart is not None
        corp_code = mapping.dart.corp_code
        try:
            snapshot_path = fetch_live_dart_snapshot(
                symbol=mapping.symbol,
                corp_code=corp_code,
                api_key=api_key,
                api_key_env=api_key_env,
                snapshot_dir=snapshot_dir,
                fetched_at=fetched_at,
                bgn_de=bgn_de,
                end_de=end_de,
                page_count=page_count,
                transport=transport,
            )
        except FileExistsError as exc:
            raise KrRealDartSmokeError("snapshot", str(exc)) from exc
        except DartLiveFetchError as exc:
            raise KrRealDartSmokeError("fetch", exc.message) from exc

        fetched.append(
            _FetchedDartSnapshot(
                symbol=mapping.symbol,
                market=mapping.market,
                corp_code=corp_code,
                snapshot_path=snapshot_path,
            )
        )

    return fetched


def _disclosure_records_from_snapshots(
    *,
    fetched: list[_FetchedDartSnapshot],
    as_of: datetime,
    page_count: int,
) -> list[DisclosureRecord]:
    """snapshot → DisclosureRecord. Date-ID 할당은 combined batch에서 한 번만 수행한다."""
    records: list[DisclosureRecord] = []
    for item in fetched:
        client = DartSnapshotReplayClient(item.snapshot_path)
        adapter = DartDisclosureAdapter(client)
        try:
            symbol_records = adapter.fetch_recent_disclosures(
                item.symbol,
                as_of=as_of,
                limit=page_count,
            )
        except ValueError as exc:
            raise KrRealDartSmokeError("normalize", str(exc)) from exc
        records.extend(symbol_records)
    return records


def _allocate_source_records(
    *,
    disclosure_records: list[DisclosureRecord],
    store_path: Path,
) -> list[DateIdSourceRecord]:
    store = SQLiteDateIdSourceStore(store_path)
    try:
        date_ids = allocate_date_ids_for_records(disclosure_records, store=store)
    finally:
        store.close()
    return [
        disclosure_record_to_source_record(record, date_id)
        for record, date_id in zip(disclosure_records, date_ids, strict=True)
    ]


def _build_transport(
    *,
    transport: Callable[[Mapping[str, str]], Mapping[str, Any]] | None,
    urlopen_fn: Any | None,
) -> Callable[[Mapping[str, str]], Mapping[str, Any]]:
    if transport is not None:
        return transport

    from data.dart_http_client import fetch_opendart_list_response

    def _http_transport(params: Mapping[str, str]) -> dict[str, Any]:
        return dict(fetch_opendart_list_response(params, urlopen_fn=urlopen_fn))

    return _http_transport


def run_kr_real_dart_smoke(
    *,
    universe_path: Path,
    provider_mapping_path: Path,
    store_path: Path,
    snapshot_dir: Path,
    out_jsonl: Path,
    as_of: datetime,
    bgn_de: str,
    end_de: str | None = None,
    page_count: int = 100,
    api_key_env: str = DART_DEFAULT_API_KEY_ENV,
    force: bool = False,
    fetched_at: datetime | None = None,
    transport: Callable[[Mapping[str, str]], Mapping[str, Any]] | None = None,
    urlopen_fn: Any | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """KR real sample universe enabled symbols에 대해 live DART disclosure smoke를 실행한다."""
    if page_count <= 0:
        raise KrRealDartSmokeError("args", "--page-count must be greater than 0")
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
    enabled_kr_symbols = _validate_dart_mapping_coverage(registry, universe)

    resolved_api_key = api_key if api_key is not None else _read_api_key(api_key_env)
    effective_transport = _build_transport(transport=transport, urlopen_fn=urlopen_fn)

    fetched = _fetch_dart_snapshots(
        registry=registry,
        enabled_kr_symbols=enabled_kr_symbols,
        snapshot_dir=snapshot_dir,
        fetched_at=effective_fetched_at,
        bgn_de=bgn_de,
        end_de=end_de,
        page_count=page_count,
        api_key=resolved_api_key,
        api_key_env=api_key_env,
        transport=effective_transport,
    )
    disclosure_records = _disclosure_records_from_snapshots(
        fetched=fetched,
        as_of=aware_as_of,
        page_count=page_count,
    )
    source_records = _allocate_source_records(
        disclosure_records=disclosure_records,
        store_path=store_path,
    )

    try:
        write_date_id_source_records_jsonl(out_jsonl, source_records, force=force)
    except FileExistsError as exc:
        raise KrRealDartSmokeError("write", str(exc)) from exc

    records_by_symbol: dict[str, int] = {}
    for record in source_records:
        if record.symbol is not None:
            records_by_symbol[record.symbol] = records_by_symbol.get(record.symbol, 0) + 1

    symbol_summaries = [
        {
            "symbol": item.symbol,
            "market": item.market,
            "corp_code": item.corp_code,
            "snapshot_path": str(item.snapshot_path),
            "records_count": records_by_symbol.get(item.symbol, 0),
        }
        for item in fetched
    ]
    return {
        "status": "ok",
        "stage": "complete",
        "mode": "live-dart-smoke",
        "universe": universe.name,
        "provider_mapping": registry.name,
        "symbols_count": len(fetched),
        "records_count": len(source_records),
        "symbols": symbol_summaries,
        "out_jsonl": str(out_jsonl),
    }


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"KR real DART smoke: {status}", file=out)
    for key in (
        "mode",
        "stage",
        "universe",
        "provider_mapping",
        "symbols_count",
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
        payload = run_kr_real_dart_smoke(
            universe_path=_require_path(args.universe, flag="--universe"),
            provider_mapping_path=_require_path(args.provider_mapping, flag="--provider-mapping"),
            store_path=_require_path(args.store, flag="--store"),
            snapshot_dir=_require_path(args.snapshot_dir, flag="--snapshot-dir"),
            out_jsonl=_require_path(args.out_jsonl, flag="--out-jsonl"),
            as_of=_parse_as_of(args.as_of),
            bgn_de=args.bgn_de,
            end_de=args.end_de,
            page_count=args.page_count,
            api_key_env=args.api_key_env,
            force=args.force,
        )
    except KrRealDartSmokeError as exc:
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
