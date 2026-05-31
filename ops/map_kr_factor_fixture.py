#!/usr/bin/env python3
"""Fixture-first KR factor source adapter CLI (3G4-4).

source-specific local factor payload → canonical 3G4-1 factor input TOML.
network/env/API key/live factor scoring 호출 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

from data.kr_factor_source_adapter import KrFactorSourceAdapterError, replay_kr_factor_source_payload

StageName = Literal["args", "parse", "map", "write", "validate", "complete"]


class MapKrFactorFixtureCliError(Exception):
    """map_kr_factor_fixture CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Map source-specific KR factor fixture payload to canonical factor input TOML "
            "(fixture-first; no live API)."
        ),
    )
    parser.add_argument("--source", required=True, help="local source-specific factor payload JSON path")
    parser.add_argument("--factor-inputs-out", required=True, help="generated canonical factor input TOML path")
    parser.add_argument("--output-name", required=True, help="generated factor input document name")
    parser.add_argument("--output-description", default=None, help="optional factor input document description")
    parser.add_argument("--factor-score-version", required=True, help="factor score version label for canonical TOML")
    parser.add_argument("--force", action="store_true", help="overwrite existing factor input output file")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _validate_cli_text_fields(args: argparse.Namespace) -> None:
    checks: tuple[tuple[str | None, str], ...] = (
        (args.output_name, "--output-name"),
        (args.output_description, "--output-description"),
        (args.factor_score_version, "--factor-score-version"),
    )
    for value, flag_name in checks:
        if value is not None and _contains_control_character(value):
            raise MapKrFactorFixtureCliError(
                "args",
                f"{flag_name} contains a control character",
            )


def run_map_kr_factor_fixture(
    *,
    source_path: Path,
    factor_inputs_out: Path,
    output_name: str,
    output_description: str | None,
    factor_score_version: str,
    force: bool = False,
) -> dict[str, Any]:
    """source payload → canonical factor input TOML replay."""
    try:
        return replay_kr_factor_source_payload(
            source_path=source_path,
            factor_inputs_out=factor_inputs_out,
            output_name=output_name,
            output_description=output_description,
            factor_score_version=factor_score_version,
            force=force,
        )
    except KrFactorSourceAdapterError as exc:
        raise MapKrFactorFixtureCliError(exc.stage, exc.message) from exc


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Map KR factor fixture: {status}", file=out)
    for key in (
        "stage",
        "mode",
        "source",
        "factor_inputs_out",
        "factors_count",
        "factor_score_version",
        "as_of",
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
        payload = run_map_kr_factor_fixture(
            source_path=Path(args.source),
            factor_inputs_out=Path(args.factor_inputs_out),
            output_name=args.output_name,
            output_description=args.output_description,
            factor_score_version=args.factor_score_version,
            force=args.force,
        )
    except MapKrFactorFixtureCliError as exc:
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
