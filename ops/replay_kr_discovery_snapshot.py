#!/usr/bin/env python3
"""Fixture-first KR discovery snapshot replay adapter (3G3-3).

local discovery snapshot JSON → 3G1 sector-tagged candidate pool TOML.
network/env/API key/live discovery transport 호출 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

from data.kr_discovery_source_adapter import KrDiscoverySourceAdapterError, replay_kr_discovery_snapshot

StageName = Literal["args", "parse", "write", "validate", "complete"]


class ReplayKrDiscoverySnapshotCliError(Exception):
    """replay_kr_discovery_snapshot CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay KR discovery snapshot JSON into sector-tagged candidate pool TOML "
            "(fixture-first; no live API)."
        ),
    )
    parser.add_argument("--snapshot", required=True, help="local discovery snapshot JSON path")
    parser.add_argument(
        "--candidate-pool-out",
        required=True,
        help="generated 3G1 candidate pool TOML output path",
    )
    parser.add_argument("--pool-name", required=True, help="generated candidate pool name")
    parser.add_argument("--pool-description", default=None, help="optional candidate pool description")
    parser.add_argument("--force", action="store_true", help="overwrite existing output file")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _validate_cli_text_fields(args: argparse.Namespace) -> None:
    checks: tuple[tuple[str | None, str], ...] = (
        (args.pool_name, "--pool-name"),
        (args.pool_description, "--pool-description"),
    )
    for value, flag_name in checks:
        if value is not None and _contains_control_character(value):
            raise ReplayKrDiscoverySnapshotCliError(
                "args",
                f"{flag_name} contains a control character",
            )


def run_replay_kr_discovery_snapshot_cli(
    *,
    snapshot_path: Path,
    candidate_pool_out: Path,
    pool_name: str,
    pool_description: str | None,
    force: bool,
) -> dict[str, Any]:
    """CLI wrapper around replay_kr_discovery_snapshot()."""
    try:
        return replay_kr_discovery_snapshot(
            snapshot_path=snapshot_path,
            candidate_pool_out=candidate_pool_out,
            pool_name=pool_name,
            pool_description=pool_description,
            force=force,
        )
    except KrDiscoverySourceAdapterError as exc:
        raise ReplayKrDiscoverySnapshotCliError(exc.stage, exc.message) from exc


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Replay KR discovery snapshot: {status}", file=out)
    for key in (
        "stage",
        "snapshot",
        "candidate_pool_out",
        "records_read",
        "candidates_written",
        "pool_name",
        "market",
        "sectors",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    try:
        _validate_cli_text_fields(args)
        payload = run_replay_kr_discovery_snapshot_cli(
            snapshot_path=Path(args.snapshot),
            candidate_pool_out=Path(args.candidate_pool_out),
            pool_name=args.pool_name,
            pool_description=args.pool_description,
            force=args.force,
        )
    except ReplayKrDiscoverySnapshotCliError as exc:
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
