#!/usr/bin/env python3
"""Operator-triggered KR discovery HTTP live smoke (3G3-4B).

operator-supplied endpoint URL → isolated HTTP client → 3G3-4A immutable snapshot
→ optional 3G3-3 candidate pool replay. env/API key 없음; endpoint hardcode 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from data.kr_discovery_http_client import KrDiscoveryHttpError, fetch_kr_discovery_http_payload
from data.kr_discovery_live_client import KrDiscoveryLiveFetchError, fetch_live_kr_discovery_snapshot
from data.kr_discovery_source_adapter import KrDiscoverySourceAdapterError, replay_kr_discovery_snapshot
from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime

StageName = Literal["args", "fetch", "parse", "snapshot", "replay", "write", "complete"]

UrlopenFn = Callable[..., Any]


class KrDiscoveryLiveSmokeError(Exception):
    """run_kr_discovery_live_smoke 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operator-triggered KR discovery HTTP live smoke — "
            "HTTP fetch → immutable raw snapshot → optional candidate pool replay."
        ),
    )
    parser.add_argument("--endpoint-url", required=True, help="operator-supplied discovery JSON endpoint URL")
    parser.add_argument("--snapshot-dir", required=True, help="immutable raw discovery snapshot output directory")
    parser.add_argument(
        "--candidate-pool-out",
        default=None,
        help="optional 3G1 candidate pool TOML output path (3G3-3 replay)",
    )
    parser.add_argument("--pool-name", default=None, help="candidate pool name (required with --candidate-pool-out)")
    parser.add_argument("--pool-description", default=None, help="optional candidate pool description")
    parser.add_argument("--fetched-at", required=True, help="timezone-aware fetched_at datetime (ISO-8601)")
    parser.add_argument("--as-of", required=True, help="timezone-aware as_of datetime (ISO-8601)")
    parser.add_argument("--market", default="KR", help="market code (only KR supported)")
    parser.add_argument("--universe-hint", required=True, help="discovery universe hint slug")
    parser.add_argument("--external-service", required=True, help="external service label for snapshot metadata")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=15.0,
        help="HTTP timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite candidate pool output only; raw snapshots are never overwritten",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _validate_cli_text_fields(*values: tuple[str | None, str]) -> None:
    for text, flag_name in values:
        if text is not None and _contains_control_character(text):
            raise KrDiscoveryLiveSmokeError("args", f"{flag_name} contains a control character")


def run_kr_discovery_live_smoke(
    *,
    endpoint_url: str,
    snapshot_dir: Path,
    fetched_at: datetime,
    as_of: datetime,
    universe_hint: str,
    external_service: str,
    market: str = "KR",
    timeout_seconds: float = 15.0,
    candidate_pool_out: Path | None = None,
    pool_name: str | None = None,
    pool_description: str | None = None,
    force: bool = False,
    urlopen_fn: UrlopenFn | None = None,
) -> dict[str, Any]:
    """HTTP live smoke: fetch → 4A snapshot → optional 3G3-3 candidate pool replay."""
    if timeout_seconds <= 0:
        raise KrDiscoveryLiveSmokeError("args", "--timeout-seconds must be a positive number")
    if market != "KR":
        raise KrDiscoveryLiveSmokeError("args", "--market must be 'KR'")
    if candidate_pool_out is not None and pool_name is None:
        raise KrDiscoveryLiveSmokeError("args", "--pool-name is required when --candidate-pool-out is provided")

    _validate_cli_text_fields(
        (universe_hint, "--universe-hint"),
        (external_service, "--external-service"),
        (pool_name, "--pool-name"),
        (pool_description, "--pool-description"),
    )

    require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    require_timezone_aware_datetime(as_of, field_name="as_of")

    try:
        fetched_payload = fetch_kr_discovery_http_payload(
            endpoint_url=endpoint_url,
            timeout_seconds=timeout_seconds,
            urlopen_fn=urlopen_fn,
        )
    except KrDiscoveryHttpError as exc:
        raise KrDiscoveryLiveSmokeError(exc.stage, exc.safe_message) from None

    def transport(_metadata: Mapping[str, str]) -> Mapping[str, Any]:
        return fetched_payload

    try:
        snapshot_path = fetch_live_kr_discovery_snapshot(
            snapshot_dir=snapshot_dir,
            fetched_at=fetched_at,
            as_of=as_of,
            market=market,
            universe_hint=universe_hint,
            external_service=external_service,
            transport=transport,
        )
    except FileExistsError as exc:
        raise KrDiscoveryLiveSmokeError("snapshot", str(exc)) from None
    except KrDiscoveryLiveFetchError as exc:
        raise KrDiscoveryLiveSmokeError(exc.stage, exc.message) from None

    records_raw = fetched_payload.get("records")
    records_count = len(records_raw) if isinstance(records_raw, list) else 0

    result: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "mode": "live-discovery-smoke",
        "snapshot_path": str(snapshot_path),
        "records_count": records_count,
        "market": market,
        "universe_hint": universe_hint,
        "external_service": external_service,
    }

    if candidate_pool_out is not None:
        try:
            replay_kr_discovery_snapshot(
                snapshot_path=snapshot_path,
                candidate_pool_out=candidate_pool_out,
                pool_name=pool_name or "",
                pool_description=pool_description,
                force=force,
            )
        except KrDiscoverySourceAdapterError as exc:
            stage: StageName = "write" if exc.stage == "write" else "replay"
            raise KrDiscoveryLiveSmokeError(stage, exc.message) from None
        result["candidate_pool_out"] = str(candidate_pool_out)

    return result


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"KR discovery live smoke: {status}", file=out)
    for key in (
        "stage",
        "mode",
        "snapshot_path",
        "candidate_pool_out",
        "records_count",
        "market",
        "universe_hint",
        "external_service",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    try:
        fetched_at = parse_timezone_aware_datetime(args.fetched_at, field_name="--fetched-at")
        as_of = parse_timezone_aware_datetime(args.as_of, field_name="--as-of")
    except ValueError as exc:
        payload = {
            "status": "error",
            "stage": "args",
            "error": str(exc),
        }
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    try:
        payload = run_kr_discovery_live_smoke(
            endpoint_url=args.endpoint_url,
            snapshot_dir=Path(args.snapshot_dir),
            fetched_at=fetched_at,
            as_of=as_of,
            market=args.market,
            universe_hint=args.universe_hint,
            external_service=args.external_service,
            timeout_seconds=args.timeout_seconds,
            candidate_pool_out=Path(args.candidate_pool_out) if args.candidate_pool_out else None,
            pool_name=args.pool_name,
            pool_description=args.pool_description,
            force=args.force,
        )
    except KrDiscoveryLiveSmokeError as exc:
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
