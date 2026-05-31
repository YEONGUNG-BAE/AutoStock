#!/usr/bin/env python3
"""Operator-triggered source-specific KR discovery live endpoint adapter (3G3-6).

operator-supplied endpoint URL → HTTP fetch → immutable raw source snapshot
→ 3G3-5 mapper → 3G3-4A canonical snapshot → optional 3G3-3 candidate pool replay.
env/API key 없음; endpoint hardcode 없음.
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
from data.kr_discovery_schema_mapper import (
    KrDiscoverySchemaMappingError,
    map_synthetic_provider_payload_to_transport_payload,
    parse_synthetic_provider_payload_mapping,
)
from data.kr_discovery_source_adapter import KrDiscoverySourceAdapterError, replay_kr_discovery_snapshot
from data.kr_discovery_source_payload_snapshot import (
    KrDiscoverySourcePayloadSnapshotError,
    write_source_payload_snapshot,
)
from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime

StageName = Literal[
    "args",
    "fetch",
    "parse",
    "source_snapshot",
    "map",
    "canonical_snapshot",
    "replay",
    "write",
    "complete",
]

UrlopenFn = Callable[..., Any]
EXPECTED_SOURCE_FORMAT = "synthetic-provider-v1"


class KrDiscoverySourceLiveSmokeError(Exception):
    """run_kr_discovery_source_live_smoke 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operator-triggered source-specific KR discovery live endpoint adapter — "
            "HTTP fetch → source snapshot → mapper → canonical snapshot → optional candidate pool replay."
        ),
    )
    parser.add_argument("--endpoint-url", required=True, help="operator-supplied source JSON endpoint URL")
    parser.add_argument(
        "--source-snapshot-dir",
        required=True,
        help="immutable raw source-specific payload snapshot output directory",
    )
    parser.add_argument(
        "--canonical-snapshot-dir",
        required=True,
        help="immutable canonical raw discovery snapshot output directory (3G3-4A)",
    )
    parser.add_argument(
        "--candidate-pool-out",
        default=None,
        help="optional 3G1 candidate pool TOML output path (3G3-3 replay)",
    )
    parser.add_argument("--pool-name", default=None, help="candidate pool name (required with --candidate-pool-out)")
    parser.add_argument("--pool-description", default=None, help="optional candidate pool description")
    parser.add_argument("--fetched-at", required=True, help="timezone-aware fetched_at datetime (ISO-8601)")
    parser.add_argument("--as-of", required=True, help="timezone-aware as_of datetime (ISO-8601)")
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
        help="overwrite candidate pool output only; source/canonical snapshots are never overwritten",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _validate_cli_text_fields(*values: tuple[str | None, str]) -> None:
    for text, flag_name in values:
        if text is not None and _contains_control_character(text):
            raise KrDiscoverySourceLiveSmokeError("args", f"{flag_name} contains a control character")


def _extract_source_format(payload: Mapping[str, Any]) -> str:
    source_format = payload.get("source_format")
    if not isinstance(source_format, str) or not source_format.strip():
        raise KrDiscoverySourceLiveSmokeError(
            "map",
            "source payload source_format must be a non-blank string",
        )
    return source_format.strip()


def run_kr_discovery_source_live_smoke(
    *,
    endpoint_url: str,
    source_snapshot_dir: Path,
    canonical_snapshot_dir: Path,
    fetched_at: datetime,
    as_of: datetime,
    universe_hint: str,
    external_service: str,
    timeout_seconds: float = 15.0,
    candidate_pool_out: Path | None = None,
    pool_name: str | None = None,
    pool_description: str | None = None,
    force: bool = False,
    urlopen_fn: UrlopenFn | None = None,
) -> dict[str, Any]:
    """source-specific live smoke: HTTP → source snapshot → map → 4A → optional 3G3-3 replay."""
    if timeout_seconds <= 0:
        raise KrDiscoverySourceLiveSmokeError("args", "--timeout-seconds must be a positive number")
    if candidate_pool_out is not None and pool_name is None:
        raise KrDiscoverySourceLiveSmokeError(
            "args",
            "--pool-name is required when --candidate-pool-out is provided",
        )

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
        raise KrDiscoverySourceLiveSmokeError(exc.stage, exc.safe_message) from None

    source_format = _extract_source_format(fetched_payload)

    try:
        source_snapshot_path = write_source_payload_snapshot(
            payload=fetched_payload,
            snapshot_dir=source_snapshot_dir,
            fetched_at=fetched_at,
            external_service=external_service,
            source_format=source_format,
        )
    except FileExistsError as exc:
        raise KrDiscoverySourceLiveSmokeError("source_snapshot", str(exc)) from None
    except KrDiscoverySourcePayloadSnapshotError as exc:
        raise KrDiscoverySourceLiveSmokeError(exc.stage, exc.message) from None

    try:
        parsed_payload = parse_synthetic_provider_payload_mapping(fetched_payload)
        transport_payload = map_synthetic_provider_payload_to_transport_payload(parsed_payload)
    except KrDiscoverySchemaMappingError as exc:
        raise KrDiscoverySourceLiveSmokeError("map", exc.message) from None

    def transport(_metadata: Mapping[str, str]) -> Mapping[str, Any]:
        return transport_payload

    try:
        canonical_snapshot_path = fetch_live_kr_discovery_snapshot(
            snapshot_dir=canonical_snapshot_dir,
            fetched_at=fetched_at,
            as_of=as_of,
            market=parsed_payload.market,
            universe_hint=universe_hint,
            external_service=external_service,
            transport=transport,
        )
    except FileExistsError as exc:
        raise KrDiscoverySourceLiveSmokeError("canonical_snapshot", str(exc)) from None
    except KrDiscoveryLiveFetchError as exc:
        raise KrDiscoverySourceLiveSmokeError("canonical_snapshot", exc.message) from None

    records_raw = transport_payload.get("records")
    records_count = len(records_raw) if isinstance(records_raw, list) else 0

    result: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "mode": "source-live-discovery-smoke",
        "source_snapshot_path": str(source_snapshot_path),
        "canonical_snapshot_path": str(canonical_snapshot_path),
        "records_count": records_count,
        "market": parsed_payload.market,
        "source_format": EXPECTED_SOURCE_FORMAT,
        "universe_hint": universe_hint,
        "external_service": external_service,
    }

    if candidate_pool_out is not None:
        try:
            replay_kr_discovery_snapshot(
                snapshot_path=canonical_snapshot_path,
                candidate_pool_out=candidate_pool_out,
                pool_name=pool_name or "",
                pool_description=pool_description,
                force=force,
            )
        except KrDiscoverySourceAdapterError as exc:
            stage: StageName = "write" if exc.stage == "write" else "replay"
            raise KrDiscoverySourceLiveSmokeError(stage, exc.message) from None
        result["candidate_pool_out"] = str(candidate_pool_out)

    return result


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"KR discovery source live smoke: {status}", file=out)
    for key in (
        "stage",
        "mode",
        "source_snapshot_path",
        "canonical_snapshot_path",
        "candidate_pool_out",
        "records_count",
        "market",
        "source_format",
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
        payload = run_kr_discovery_source_live_smoke(
            endpoint_url=args.endpoint_url,
            source_snapshot_dir=Path(args.source_snapshot_dir),
            canonical_snapshot_dir=Path(args.canonical_snapshot_dir),
            fetched_at=fetched_at,
            as_of=as_of,
            universe_hint=args.universe_hint,
            external_service=args.external_service,
            timeout_seconds=args.timeout_seconds,
            candidate_pool_out=Path(args.candidate_pool_out) if args.candidate_pool_out else None,
            pool_name=args.pool_name,
            pool_description=args.pool_description,
            force=args.force,
        )
    except KrDiscoverySourceLiveSmokeError as exc:
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
