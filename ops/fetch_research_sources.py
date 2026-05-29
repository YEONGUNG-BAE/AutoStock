#!/usr/bin/env python3
"""Real Research Source Intake ops entrypoint (1A: replay/fixture-only).

Layer A read-only staging only. Does not call live FRED HTTP, LLM, broker APIs, or paper execution runners.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from data.research_source_fetcher import (
    UnsupportedSourceError,
    get_source_fetcher,
    write_date_id_source_records_jsonl,
)
from domain._datetime import parse_timezone_aware_datetime

ModeName = Literal["dry-run", "replay"]


class FetchResearchSourcesError(Exception):
    """fetch_research_sources 실패. stage와 sanitized message를 담는다."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Real research source intake — replay/fixture-only staging (1A).",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="source registry key (e.g. fred)",
    )
    parser.add_argument(
        "--series-id",
        default=None,
        help="requested series identifier (required for --replay)",
    )
    parser.add_argument(
        "--date-id",
        default=None,
        help="Date-ID token YYMMDD-N (required for --replay)",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="timezone-aware ISO datetime for record created_at (required for --replay)",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="local FRED-like snapshot JSON path (required for --replay)",
    )
    parser.add_argument(
        "--out-jsonl",
        required=True,
        help="staged DateIdSourceRecord JSONL output path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan replay staging only; no snapshot read and no output write",
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="normalize snapshot to DateIdSourceRecord JSONL (no network)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing --out-jsonl if present",
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
    return "replay"


def _validate_mode_flags(args: argparse.Namespace) -> None:
    if args.dry_run and args.replay:
        raise FetchResearchSourcesError(
            "args",
            "--dry-run and --replay are mutually exclusive",
        )
    if not args.dry_run and not args.replay:
        raise FetchResearchSourcesError("args", "exactly one of --dry-run or --replay is required")


def _require_replay_value(value: str | None, *, flag: str) -> str:
    if not value:
        raise FetchResearchSourcesError("args", f"{flag} is required for --replay")
    return value


def _require_replay_path(value: str | None, *, flag: str) -> Path:
    if not value:
        raise FetchResearchSourcesError("args", f"{flag} is required for --replay")
    return Path(value)


def _parse_as_of(value: str) -> datetime:
    try:
        return parse_timezone_aware_datetime(value, field_name="as_of")
    except ValueError as exc:
        raise FetchResearchSourcesError("args", str(exc)) from exc


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
        "records_count",
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
    records_count: int | None = None,
    out_jsonl: Path,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "stage": stage,
        "mode": mode,
        "source": source,
        "out_jsonl": str(out_jsonl),
    }
    if series_id is not None:
        payload["series_id"] = series_id
    if records_count is not None:
        payload["records_count"] = records_count
    return payload


def _error_payload(*, stage: str, error: str) -> dict[str, Any]:
    return {
        "status": "error",
        "stage": stage,
        "error": error,
    }


def run_dry_run(*, source: str, series_id: str | None, out_jsonl: Path) -> dict[str, Any]:
    try:
        get_source_fetcher(source)
    except UnsupportedSourceError as exc:
        raise FetchResearchSourcesError("args", str(exc)) from exc
    return _success_payload(
        mode="dry-run",
        stage="dry-run",
        source=source.strip().lower(),
        series_id=series_id,
        out_jsonl=out_jsonl,
    )


def run_replay(
    *,
    source: str,
    series_id: str,
    date_id: str,
    as_of: datetime,
    snapshot_path: Path,
    out_jsonl: Path,
    force: bool,
) -> dict[str, Any]:
    try:
        fetcher = get_source_fetcher(source)
    except UnsupportedSourceError as exc:
        raise FetchResearchSourcesError("args", str(exc)) from exc

    try:
        records = fetcher.normalize_snapshot(
            snapshot_path,
            series_id=series_id,
            as_of=as_of,
            date_id=date_id,
        )
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
        series_id=series_id,
        records_count=len(records),
        out_jsonl=out_jsonl,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout
    stage = "args"

    if args.verbose:
        print(f"verbose: dry_run={'yes' if args.dry_run else 'no'}", file=sys.stderr)
        print(f"verbose: replay={'yes' if args.replay else 'no'}", file=sys.stderr)
        print(f"verbose: source={args.source!r}", file=sys.stderr)

    try:
        _validate_mode_flags(args)
        mode = _resolve_mode(args)
        out_jsonl = Path(args.out_jsonl)

        if mode == "dry-run":
            payload = run_dry_run(
                source=args.source,
                series_id=args.series_id,
                out_jsonl=out_jsonl,
            )
        else:
            series_id = _require_replay_value(args.series_id, flag="--series-id")
            date_id = _require_replay_value(args.date_id, flag="--date-id")
            as_of_raw = _require_replay_value(args.as_of, flag="--as-of")
            snapshot_path = _require_replay_path(args.snapshot, flag="--snapshot")
            as_of = _parse_as_of(as_of_raw)
            payload = run_replay(
                source=args.source,
                series_id=series_id,
                date_id=date_id,
                as_of=as_of,
                snapshot_path=snapshot_path,
                out_jsonl=out_jsonl,
                force=args.force,
            )
    except FetchResearchSourcesError as exc:
        stage = exc.stage
        payload = _error_payload(stage=stage, error=exc.message)
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    _emit_result(payload, as_json=as_json, out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
