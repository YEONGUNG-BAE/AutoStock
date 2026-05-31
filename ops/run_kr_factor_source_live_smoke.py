#!/usr/bin/env python3
"""Operator-triggered KR factor source live endpoint smoke (3G4-5).

operator-supplied endpoint URL → HTTP fetch → immutable raw source payload snapshot
→ optional 3G4-4 replay → canonical factor input TOML.
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

from data.kr_factor_source_adapter import KrFactorSourceAdapterError, replay_kr_factor_source_payload
from data.kr_factor_source_http_client import (
    KrFactorSourceHttpError,
    fetch_kr_factor_source_http_payload,
    validate_factor_source_endpoint_url_for_cli,
)
from data.kr_factor_source_payload_snapshot import (
    KrFactorSourceSnapshotError,
    write_immutable_factor_source_snapshot,
)
from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime

StageName = Literal["args", "fetch", "parse", "snapshot", "replay", "write", "validate", "complete"]

UrlopenFn = Callable[..., Any]
DEFAULT_TIMEOUT_SECONDS = 15.0


class KrFactorSourceLiveSmokeError(ValueError):
    """run_kr_factor_source_live_smoke 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Operator-triggered KR factor source live smoke — "
            "HTTP fetch → immutable raw source snapshot → optional canonical factor input TOML replay."
        ),
    )
    parser.add_argument("--endpoint-url", required=True, help="operator-supplied source JSON endpoint URL")
    parser.add_argument(
        "--snapshot-dir",
        required=True,
        help="immutable raw source-specific factor payload snapshot output directory",
    )
    parser.add_argument("--fetched-at", required=True, help="timezone-aware fetched_at datetime (ISO-8601)")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS:g})",
    )
    parser.add_argument(
        "--factor-inputs-out",
        default=None,
        help="optional 3G4-4 canonical factor input TOML output path",
    )
    parser.add_argument("--output-name", default=None, help="factor input document name (required with replay)")
    parser.add_argument("--output-description", default=None, help="optional factor input document description")
    parser.add_argument(
        "--factor-score-version",
        default=None,
        help="factor score version label (required with replay)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite factor input output only; raw source snapshots are never overwritten",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _validate_cli_text_fields(*values: tuple[str | None, str]) -> None:
    for text, flag_name in values:
        if text is not None and _contains_control_character(text):
            raise KrFactorSourceLiveSmokeError("args", f"{flag_name} contains a control character")


def _validate_endpoint_url_for_cli(endpoint_url: str) -> None:
    try:
        validate_factor_source_endpoint_url_for_cli(endpoint_url)
    except ValueError as exc:
        message = str(exc)
        if message.startswith("endpoint_url"):
            message = f"--endpoint-url: {message.split(':', 1)[-1].strip()}"
        raise KrFactorSourceLiveSmokeError("args", message) from None


def _map_adapter_error(exc: KrFactorSourceAdapterError) -> KrFactorSourceLiveSmokeError:
    if exc.stage in {"parse", "map"}:
        return KrFactorSourceLiveSmokeError("replay", exc.message)
    if exc.stage == "write":
        return KrFactorSourceLiveSmokeError("write", exc.message)
    if exc.stage == "validate":
        return KrFactorSourceLiveSmokeError("validate", exc.message)
    return KrFactorSourceLiveSmokeError("replay", exc.message)


def run_kr_factor_source_live_smoke(
    *,
    endpoint_url: str,
    snapshot_dir: Path,
    fetched_at: datetime,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    factor_inputs_out: Path | None = None,
    output_name: str | None = None,
    output_description: str | None = None,
    factor_score_version: str | None = None,
    force: bool = False,
    urlopen_fn: UrlopenFn | None = None,
) -> dict[str, Any]:
    """KR factor source live smoke: HTTP → raw snapshot → optional 3G4-4 replay."""
    if timeout_seconds <= 0:
        raise KrFactorSourceLiveSmokeError("args", "--timeout-seconds must be a positive number")

    _validate_endpoint_url_for_cli(endpoint_url)

    if factor_inputs_out is not None:
        if output_name is None:
            raise KrFactorSourceLiveSmokeError(
                "args",
                "--output-name is required when --factor-inputs-out is provided",
            )
        if factor_score_version is None:
            raise KrFactorSourceLiveSmokeError(
                "args",
                "--factor-score-version is required when --factor-inputs-out is provided",
            )

    _validate_cli_text_fields(
        (output_name, "--output-name"),
        (output_description, "--output-description"),
        (factor_score_version, "--factor-score-version"),
    )

    try:
        require_timezone_aware_datetime(fetched_at, field_name="fetched_at")
    except ValueError as exc:
        raise KrFactorSourceLiveSmokeError("args", str(exc)) from None

    try:
        fetched_payload = fetch_kr_factor_source_http_payload(
            endpoint_url,
            timeout_seconds,
            urlopen_fn=urlopen_fn,
        )
    except KrFactorSourceHttpError as exc:
        raise KrFactorSourceLiveSmokeError(exc.stage, exc.safe_message) from None

    try:
        snapshot_path = write_immutable_factor_source_snapshot(
            fetched_payload,
            snapshot_dir,
            fetched_at=fetched_at,
        )
    except FileExistsError as exc:
        raise KrFactorSourceLiveSmokeError("snapshot", str(exc)) from None
    except KrFactorSourceSnapshotError as exc:
        raise KrFactorSourceLiveSmokeError(exc.stage, exc.message) from None

    result: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "mode": "kr-factor-source-live-smoke",
        "snapshot_path": str(snapshot_path),
        "replayed": False,
    }

    if factor_inputs_out is not None:
        try:
            replay_result = replay_kr_factor_source_payload(
                source_path=snapshot_path,
                factor_inputs_out=factor_inputs_out,
                output_name=output_name or "",
                output_description=output_description,
                factor_score_version=factor_score_version or "",
                force=force,
            )
        except KrFactorSourceAdapterError as exc:
            raise _map_adapter_error(exc) from None
        result["replayed"] = True
        result["factor_inputs_out"] = str(replay_result["factor_inputs_out"])
        result["factors_count"] = replay_result["factors_count"]
        result["factor_score_version"] = replay_result["factor_score_version"]

    return result


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"KR factor source live smoke: {status}", file=out)
    for key in (
        "stage",
        "mode",
        "snapshot_path",
        "replayed",
        "factor_inputs_out",
        "factors_count",
        "factor_score_version",
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
    except ValueError as exc:
        payload = {
            "status": "error",
            "stage": "args",
            "error": str(exc),
        }
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    try:
        payload = run_kr_factor_source_live_smoke(
            endpoint_url=args.endpoint_url,
            snapshot_dir=Path(args.snapshot_dir),
            fetched_at=fetched_at,
            timeout_seconds=args.timeout_seconds,
            factor_inputs_out=Path(args.factor_inputs_out) if args.factor_inputs_out else None,
            output_name=args.output_name,
            output_description=args.output_description,
            factor_score_version=args.factor_score_version,
            force=args.force,
        )
    except KrFactorSourceLiveSmokeError as exc:
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
