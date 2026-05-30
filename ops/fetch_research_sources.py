#!/usr/bin/env python3
"""Real Research Source Intake ops entrypoint (1A replay + 1B live-smoke).

Layer A read-only staging only. stdlib HTTP is isolated in data.fred_http_client.
Does not call LLM, broker APIs, or paper execution runners.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from data.fred_http_client import DEFAULT_API_KEY_ENV, FredHttpError
from data.fred_source_fetcher import fetch_live_snapshot
from data.date_id_store import SQLiteDateIdSourceStore
from data.research_source_fetcher import (
    UnsupportedSourceError,
    get_source_fetcher,
    write_date_id_source_records_jsonl,
)
from domain._datetime import parse_timezone_aware_datetime

ModeName = Literal["dry-run", "replay", "live-smoke"]

# DART live-smoke 전용 env 이름. parser default는 FRED용이므로 분기에서 치환한다.
DART_DEFAULT_API_KEY_ENV = "DART_API_KEY"


class FetchResearchSourcesError(Exception):
    """fetch_research_sources 실패. stage와 sanitized message를 담는다."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Real research source intake — replay/fixture and FRED live-smoke staging.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="source registry key (e.g. fred)",
    )
    parser.add_argument(
        "--series-id",
        default=None,
        help="requested series identifier (required for --replay --source fred and --live-smoke)",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="requested symbol (required for --replay --source price/dart)",
    )
    parser.add_argument(
        "--market",
        default=None,
        help="requested market (required for --replay/--live-smoke --source price)",
    )
    parser.add_argument(
        "--provider-symbol",
        default=None,
        help="yfinance provider ticker (required for --live-smoke --source price)",
    )
    parser.add_argument(
        "--currency",
        default=None,
        help="optional currency override for --live-smoke --source price",
    )
    parser.add_argument(
        "--date-id",
        default=None,
        help="Date-ID token YYMMDD-N (required for fred/price replay and live-smoke; not used for dart replay)",
    )
    parser.add_argument(
        "--store",
        default=None,
        help="SQLite Date-ID store for --replay --source dart Date-ID allocation",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="max disclosures for --replay --source dart (default: 10)",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="timezone-aware ISO datetime for record created_at (required for --replay/--live-smoke)",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="local FRED-like snapshot JSON path (required for --replay)",
    )
    parser.add_argument(
        "--snapshot-dir",
        default=None,
        help="directory for immutable live-smoke snapshot output (required for --live-smoke)",
    )
    parser.add_argument(
        "--corp-code",
        default=None,
        help="OpenDART provider corp code (required for --live-smoke --source dart)",
    )
    parser.add_argument(
        "--bgn-de",
        default=None,
        help="OpenDART search start date YYYYMMDD (required for --live-smoke --source dart)",
    )
    parser.add_argument(
        "--end-de",
        default=None,
        help="optional OpenDART search end date YYYYMMDD (--live-smoke --source dart)",
    )
    parser.add_argument(
        "--page-count",
        type=int,
        default=100,
        help="OpenDART page_count for --live-smoke --source dart (default: 100)",
    )
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help="environment variable name for API key (FRED/DART live-smoke; value never logged)",
    )
    parser.add_argument(
        "--out-jsonl",
        default=None,
        help="staged DateIdSourceRecord JSONL output path (required for --replay/--live-smoke)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan staging only; no snapshot read/write and no JSONL write",
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="normalize local snapshot to DateIdSourceRecord JSONL (no network)",
    )
    parser.add_argument(
        "--live-smoke",
        action="store_true",
        help="fetch live data via stdlib HTTP (fred/dart) or yfinance (price), write snapshot + JSONL",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing --out-jsonl if present; raw live snapshots are never overwritten",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON summary to stdout",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print non-sensitive metadata to stderr",
    )
    return parser


def _resolve_mode(args: argparse.Namespace) -> ModeName:
    if args.dry_run:
        return "dry-run"
    if args.live_smoke:
        return "live-smoke"
    return "replay"


def _validate_mode_flags(args: argparse.Namespace) -> None:
    selected = int(args.dry_run) + int(args.replay) + int(args.live_smoke)
    if selected != 1:
        raise FetchResearchSourcesError(
            "args",
            "exactly one of --dry-run, --replay, or --live-smoke is required",
        )


def _require_value(value: str | None, *, flag: str) -> str:
    if not value:
        raise FetchResearchSourcesError("args", f"{flag} is required for this mode")
    return value


def _require_path(value: str | None, *, flag: str) -> Path:
    if not value:
        raise FetchResearchSourcesError("args", f"{flag} is required for this mode")
    return Path(value)


def _parse_as_of(value: str) -> datetime:
    try:
        return parse_timezone_aware_datetime(value, field_name="as_of")
    except ValueError as exc:
        raise FetchResearchSourcesError("args", str(exc)) from exc


def _read_api_key(env_name: str) -> str:
    if not env_name.strip():
        raise FetchResearchSourcesError("args", "api_key_env must not be blank")
    value = os.environ.get(env_name)
    if not value or not value.strip():
        raise FetchResearchSourcesError(
            "args",
            f"API key not configured (env var {env_name!r} is missing or empty)",
        )
    return value.strip()


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        import json

        print(json.dumps(payload, ensure_ascii=False), file=out)
        return

    status = payload.get("status", "error")
    print(f"Fetch research sources: {status}", file=out)
    for key in (
        "mode",
        "stage",
        "source",
        "series_id",
        "symbol",
        "market",
        "provider_symbol",
        "corp_code",
        "records_count",
        "snapshot_path",
        "out_jsonl",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def _success_payload(
    *,
    mode: ModeName,
    stage: str,
    source: str,
    series_id: str | None = None,
    symbol: str | None = None,
    market: str | None = None,
    provider_symbol: str | None = None,
    corp_code: str | None = None,
    records_count: int | None = None,
    snapshot_path: Path | None = None,
    out_jsonl: Path | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "stage": stage,
        "mode": mode,
        "source": source,
    }
    if series_id is not None:
        payload["series_id"] = series_id
    if symbol is not None:
        payload["symbol"] = symbol
    if market is not None:
        payload["market"] = market
    if provider_symbol is not None:
        payload["provider_symbol"] = provider_symbol
    if corp_code is not None:
        payload["corp_code"] = corp_code
    if records_count is not None:
        payload["records_count"] = records_count
    if snapshot_path is not None:
        payload["snapshot_path"] = str(snapshot_path)
    if out_jsonl is not None:
        payload["out_jsonl"] = str(out_jsonl)
    return payload


def _error_payload(*, stage: str, error: str) -> dict[str, Any]:
    return {
        "status": "error",
        "stage": stage,
        "error": error,
    }


def run_dry_run(
    *,
    source: str,
    series_id: str | None = None,
    symbol: str | None = None,
    market: str | None = None,
    out_jsonl: Path | None = None,
) -> dict[str, Any]:
    try:
        get_source_fetcher(source)
    except UnsupportedSourceError as exc:
        raise FetchResearchSourcesError("args", str(exc)) from exc
    return _success_payload(
        mode="dry-run",
        stage="dry-run",
        source=source.strip().lower(),
        series_id=series_id,
        symbol=symbol,
        market=market,
        out_jsonl=out_jsonl,
    )


def _validate_store_parent_exists(store_path: Path) -> None:
    if not store_path.parent.is_dir():
        raise FetchResearchSourcesError(
            "args",
            f"store parent directory does not exist: {store_path.parent}",
        )


def run_replay_dart(
    *,
    symbol: str,
    as_of: datetime,
    snapshot_path: Path,
    out_jsonl: Path,
    force: bool,
    store_path: Path,
    limit: int,
) -> dict[str, Any]:
    if limit <= 0:
        raise FetchResearchSourcesError("args", "--limit must be greater than 0")
    _validate_store_parent_exists(store_path)

    try:
        fetcher = get_source_fetcher("dart")
    except UnsupportedSourceError as exc:
        raise FetchResearchSourcesError("args", str(exc)) from exc

    store = SQLiteDateIdSourceStore(store_path)
    try:
        try:
            records = fetcher.normalize_snapshot(
                snapshot_path,
                symbol=symbol,
                as_of=as_of,
                store=store,
                limit=limit,
            )
        except FileNotFoundError as exc:
            raise FetchResearchSourcesError("snapshot", str(exc)) from exc
        except ValueError as exc:
            raise FetchResearchSourcesError("normalize", str(exc)) from exc
    finally:
        store.close()

    try:
        write_date_id_source_records_jsonl(out_jsonl, records, force=force)
    except FileExistsError as exc:
        raise FetchResearchSourcesError("write", str(exc)) from exc

    return _success_payload(
        mode="replay",
        stage="complete",
        source=fetcher.source_key,
        symbol=symbol,
        records_count=len(records),
        snapshot_path=snapshot_path,
        out_jsonl=out_jsonl,
    )


def run_replay(
    *,
    source: str,
    date_id: str,
    as_of: datetime,
    snapshot_path: Path,
    out_jsonl: Path,
    force: bool,
    series_id: str | None = None,
    symbol: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    normalized_source = source.strip().lower()
    try:
        fetcher = get_source_fetcher(source)
    except UnsupportedSourceError as exc:
        raise FetchResearchSourcesError("args", str(exc)) from exc

    try:
        if normalized_source == "fred":
            if not series_id:
                raise FetchResearchSourcesError("args", "--series-id is required for --replay --source fred")
            records = fetcher.normalize_snapshot(
                snapshot_path,
                series_id=series_id,
                as_of=as_of,
                date_id=date_id,
            )
        elif normalized_source == "price":
            if not symbol:
                raise FetchResearchSourcesError("args", "--symbol is required for --replay --source price")
            if not market:
                raise FetchResearchSourcesError("args", "--market is required for --replay --source price")
            records = fetcher.normalize_snapshot(
                snapshot_path,
                symbol=symbol,
                market=market,
                as_of=as_of,
                date_id=date_id,
            )
        else:
            raise FetchResearchSourcesError("args", f"replay unsupported for source: {source!r}")
    except FileNotFoundError as exc:
        raise FetchResearchSourcesError("snapshot", str(exc)) from exc
    except ValueError as exc:
        raise FetchResearchSourcesError("normalize", str(exc)) from exc

    try:
        write_date_id_source_records_jsonl(out_jsonl, records, force=force)
    except FileExistsError as exc:
        raise FetchResearchSourcesError("write", str(exc)) from exc

    return _success_payload(
        mode="replay",
        stage="complete",
        source=fetcher.source_key,
        series_id=series_id if normalized_source == "fred" else None,
        symbol=symbol if normalized_source == "price" else None,
        market=market if normalized_source == "price" else None,
        records_count=len(records),
        snapshot_path=snapshot_path,
        out_jsonl=out_jsonl,
    )


def run_live_smoke_fred(
    *,
    series_id: str,
    date_id: str,
    as_of: datetime,
    snapshot_dir: Path,
    api_key_env: str,
    out_jsonl: Path,
    force: bool,
    fetched_at: datetime,
    urlopen_fn: Any | None = None,
) -> dict[str, Any]:
    api_key = _read_api_key(api_key_env)

    try:
        snapshot_path = fetch_live_snapshot(
            series_id=series_id,
            api_key=api_key,
            snapshot_dir=snapshot_dir,
            fetched_at=fetched_at,
            api_key_env=api_key_env,
            urlopen_fn=urlopen_fn,
        )
    except FileExistsError as exc:
        raise FetchResearchSourcesError("snapshot", str(exc)) from exc
    except FredHttpError as exc:
        raise FetchResearchSourcesError("fetch", exc.message) from exc

    fetcher = get_source_fetcher("fred")
    try:
        records = fetcher.normalize_snapshot(
            snapshot_path,
            series_id=series_id,
            as_of=as_of,
            date_id=date_id,
        )
    except ValueError as exc:
        raise FetchResearchSourcesError("normalize", str(exc)) from exc

    try:
        write_date_id_source_records_jsonl(out_jsonl, records, force=force)
    except FileExistsError as exc:
        raise FetchResearchSourcesError("write", str(exc)) from exc

    return _success_payload(
        mode="live-smoke",
        stage="complete",
        source=fetcher.source_key,
        series_id=series_id,
        records_count=len(records),
        snapshot_path=snapshot_path,
        out_jsonl=out_jsonl,
    )


def run_live_smoke_price(
    *,
    symbol: str,
    market: str,
    provider_symbol: str,
    currency: str | None,
    date_id: str,
    as_of: datetime,
    snapshot_dir: Path,
    out_jsonl: Path,
    force: bool,
    fetched_at: datetime,
    ticker_factory: Any | None = None,
) -> dict[str, Any]:
    # yfinance는 price_live_client 내부 lazy import (R1). replay/FRED 경로는 이 모듈을 import하지 않는다.
    from data.price_live_client import PriceLiveFetchError, fetch_live_price_snapshot

    try:
        snapshot_path = fetch_live_price_snapshot(
            provider_symbol=provider_symbol,
            symbol=symbol,
            market=market,
            currency=currency,
            snapshot_dir=snapshot_dir,
            fetched_at=fetched_at,
            ticker_factory=ticker_factory,
        )
    except FileExistsError as exc:
        raise FetchResearchSourcesError("snapshot", str(exc)) from exc
    except PriceLiveFetchError as exc:
        raise FetchResearchSourcesError("fetch", exc.message) from exc

    fetcher = get_source_fetcher("price")
    try:
        records = fetcher.normalize_snapshot(
            snapshot_path,
            symbol=symbol,
            market=market,
            as_of=as_of,
            date_id=date_id,
        )
    except ValueError as exc:
        raise FetchResearchSourcesError("normalize", str(exc)) from exc

    try:
        write_date_id_source_records_jsonl(out_jsonl, records, force=force)
    except FileExistsError as exc:
        raise FetchResearchSourcesError("write", str(exc)) from exc

    return _success_payload(
        mode="live-smoke",
        stage="complete",
        source=fetcher.source_key,
        symbol=symbol,
        market=market,
        provider_symbol=provider_symbol,
        records_count=len(records),
        snapshot_path=snapshot_path,
        out_jsonl=out_jsonl,
    )


def run_live_smoke_dart(
    *,
    symbol: str,
    corp_code: str,
    bgn_de: str,
    end_de: str | None,
    page_count: int,
    as_of: datetime,
    snapshot_dir: Path,
    api_key_env: str,
    out_jsonl: Path,
    force: bool,
    store_path: Path,
    fetched_at: datetime,
    transport: Any | None = None,
    urlopen_fn: Any | None = None,
) -> dict[str, Any]:
    """OpenDART live-smoke: HTTP → immutable snapshot → 3A replay → JSONL. store write는 8B만."""
    if page_count <= 0:
        raise FetchResearchSourcesError("args", "--page-count must be greater than 0")
    _validate_store_parent_exists(store_path)

    api_key = _read_api_key(api_key_env)

    effective_transport = transport
    if effective_transport is None:
        from data.dart_http_client import fetch_opendart_list_response

        def _http_transport(params: dict[str, str]) -> dict[str, Any]:
            return dict(fetch_opendart_list_response(params, urlopen_fn=urlopen_fn))

        effective_transport = _http_transport

    from data.dart_live_client import DartLiveFetchError, fetch_live_dart_snapshot
    from data.dart_source_fetcher import DartDisclosureSnapshotReplayFetcher

    try:
        snapshot_path = fetch_live_dart_snapshot(
            symbol=symbol,
            corp_code=corp_code,
            api_key=api_key,
            api_key_env=api_key_env,
            snapshot_dir=snapshot_dir,
            fetched_at=fetched_at,
            bgn_de=bgn_de,
            end_de=end_de,
            page_count=page_count,
            transport=effective_transport,
        )
    except FileExistsError as exc:
        raise FetchResearchSourcesError("snapshot", str(exc)) from exc
    except DartLiveFetchError as exc:
        raise FetchResearchSourcesError("fetch", exc.message) from exc

    store = SQLiteDateIdSourceStore(store_path)
    try:
        try:
            records = DartDisclosureSnapshotReplayFetcher().normalize_snapshot(
                snapshot_path,
                symbol=symbol,
                as_of=as_of,
                store=store,
                limit=page_count,
            )
        except FileNotFoundError as exc:
            raise FetchResearchSourcesError("snapshot", str(exc)) from exc
        except ValueError as exc:
            raise FetchResearchSourcesError("normalize", str(exc)) from exc
    finally:
        store.close()

    try:
        write_date_id_source_records_jsonl(out_jsonl, records, force=force)
    except FileExistsError as exc:
        raise FetchResearchSourcesError("write", str(exc)) from exc

    return _success_payload(
        mode="live-smoke",
        stage="complete",
        source="dart",
        symbol=symbol,
        corp_code=corp_code,
        records_count=len(records),
        snapshot_path=snapshot_path,
        out_jsonl=out_jsonl,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    if args.verbose:
        print(f"verbose: dry_run={'yes' if args.dry_run else 'no'}", file=sys.stderr)
        print(f"verbose: replay={'yes' if args.replay else 'no'}", file=sys.stderr)
        print(f"verbose: live_smoke={'yes' if args.live_smoke else 'no'}", file=sys.stderr)
        print(f"verbose: source={args.source!r}", file=sys.stderr)

    try:
        _validate_mode_flags(args)
        mode = _resolve_mode(args)

        if mode == "dry-run":
            out_jsonl = Path(args.out_jsonl) if args.out_jsonl else None
            payload = run_dry_run(
                source=args.source,
                series_id=args.series_id,
                symbol=args.symbol,
                market=args.market,
                out_jsonl=out_jsonl,
            )
        elif mode == "live-smoke":
            normalized_source = args.source.strip().lower()
            as_of = _parse_as_of(_require_value(args.as_of, flag="--as-of"))
            snapshot_dir = _require_path(args.snapshot_dir, flag="--snapshot-dir")
            out_jsonl = _require_path(args.out_jsonl, flag="--out-jsonl")
            fetched_at = datetime.now().astimezone()
            if normalized_source == "fred":
                date_id = _require_value(args.date_id, flag="--date-id")
                series_id = _require_value(args.series_id, flag="--series-id")
                payload = run_live_smoke_fred(
                    series_id=series_id,
                    date_id=date_id,
                    as_of=as_of,
                    snapshot_dir=snapshot_dir,
                    api_key_env=args.api_key_env,
                    out_jsonl=out_jsonl,
                    force=args.force,
                    fetched_at=fetched_at,
                )
            elif normalized_source == "price":
                date_id = _require_value(args.date_id, flag="--date-id")
                symbol = _require_value(args.symbol, flag="--symbol")
                market = _require_value(args.market, flag="--market")
                provider_symbol = _require_value(args.provider_symbol, flag="--provider-symbol")
                payload = run_live_smoke_price(
                    symbol=symbol,
                    market=market,
                    provider_symbol=provider_symbol,
                    currency=args.currency,
                    date_id=date_id,
                    as_of=as_of,
                    snapshot_dir=snapshot_dir,
                    out_jsonl=out_jsonl,
                    force=args.force,
                    fetched_at=fetched_at,
                )
            elif normalized_source == "dart":
                if args.date_id:
                    raise FetchResearchSourcesError(
                        "args",
                        "--date-id is not supported for --live-smoke --source dart",
                    )
                symbol = _require_value(args.symbol, flag="--symbol")
                corp_code = _require_value(args.corp_code, flag="--corp-code")
                bgn_de = _require_value(args.bgn_de, flag="--bgn-de")
                store_path = _require_path(args.store, flag="--store")
                dart_api_key_env = args.api_key_env
                if dart_api_key_env == DEFAULT_API_KEY_ENV:
                    # Source-specific default: parser-level default is FRED_API_KEY,
                    # but DART must default to DART_API_KEY to avoid cross-source secret use.
                    # Known limitation: an operator explicitly passing --api-key-env FRED_API_KEY
                    # is indistinguishable from argparse's default and will be source-defaulted.
                    dart_api_key_env = DART_DEFAULT_API_KEY_ENV
                payload = run_live_smoke_dart(
                    symbol=symbol,
                    corp_code=corp_code,
                    bgn_de=bgn_de,
                    end_de=args.end_de,
                    page_count=args.page_count,
                    as_of=as_of,
                    snapshot_dir=snapshot_dir,
                    api_key_env=dart_api_key_env,
                    out_jsonl=out_jsonl,
                    force=args.force,
                    store_path=store_path,
                    fetched_at=fetched_at,
                )
            else:
                raise FetchResearchSourcesError(
                    "args",
                    f"live-smoke unsupported for source: {args.source!r}",
                )
        else:
            as_of = _parse_as_of(_require_value(args.as_of, flag="--as-of"))
            snapshot_path = _require_path(args.snapshot, flag="--snapshot")
            out_jsonl = _require_path(args.out_jsonl, flag="--out-jsonl")
            normalized_source = args.source.strip().lower()
            if normalized_source == "dart":
                if args.date_id:
                    raise FetchResearchSourcesError(
                        "args",
                        "--date-id is not supported for --replay --source dart",
                    )
                symbol = _require_value(args.symbol, flag="--symbol")
                store_path = _require_path(args.store, flag="--store")
                payload = run_replay_dart(
                    symbol=symbol,
                    as_of=as_of,
                    snapshot_path=snapshot_path,
                    out_jsonl=out_jsonl,
                    force=args.force,
                    store_path=store_path,
                    limit=args.limit,
                )
            else:
                date_id = _require_value(args.date_id, flag="--date-id")
                payload = run_replay(
                    source=args.source,
                    date_id=date_id,
                    as_of=as_of,
                    snapshot_path=snapshot_path,
                    out_jsonl=out_jsonl,
                    force=args.force,
                    series_id=args.series_id if normalized_source == "fred" else None,
                    symbol=args.symbol if normalized_source == "price" else None,
                    market=args.market if normalized_source == "price" else None,
                )
    except FetchResearchSourcesError as exc:
        payload = _error_payload(stage=exc.stage, error=exc.message)
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    _emit_result(payload, as_json=as_json, out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
