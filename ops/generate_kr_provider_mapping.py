#!/usr/bin/env python3
"""Fixture-first KR universe/provider mapping generator (3F1).

operator-curated candidate TOML + local corp-code XML/ZIP → universe/mapping TOML.
network/env/API key/live OpenDART/yfinance 호출 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

from data.kr_provider_mapping_generator import (
    KrProviderMappingGeneratorError,
    generate_kr_provider_mapping_files,
)

StageName = Literal["args", "parse", "resolve", "write", "validate", "complete"]


class GenerateKrProviderMappingCliError(Exception):
    """generate_kr_provider_mapping CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate KR universe + provider mapping TOML from local candidates "
            "and corp-code snapshot (fixture-first; no live API)."
        ),
    )
    parser.add_argument("--candidates", required=True, help="KR candidate TOML path")
    corp_source = parser.add_mutually_exclusive_group(required=True)
    corp_source.add_argument("--corp-code-xml", help="local corp-code master XML path")
    corp_source.add_argument("--corp-code-zip", help="local corp-code master ZIP path")
    parser.add_argument("--universe-out", required=True, help="generated universe TOML output path")
    parser.add_argument(
        "--provider-mapping-out",
        required=True,
        help="generated provider mapping TOML output path",
    )
    parser.add_argument("--universe-name", required=True, help="generated universe name")
    parser.add_argument(
        "--provider-mapping-name",
        required=True,
        help="generated provider mapping registry name",
    )
    parser.add_argument("--universe-description", default=None, help="optional universe description")
    parser.add_argument(
        "--provider-mapping-description",
        default=None,
        help="optional provider mapping description",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing output files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _resolve_corp_code_paths(args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    corp_code_xml = Path(args.corp_code_xml) if args.corp_code_xml else None
    corp_code_zip = Path(args.corp_code_zip) if args.corp_code_zip else None
    if corp_code_xml is None and corp_code_zip is None:
        raise GenerateKrProviderMappingCliError(
            "args",
            "exactly one of --corp-code-xml or --corp-code-zip is required",
        )
    return corp_code_xml, corp_code_zip


def run_generate_kr_provider_mapping(
    *,
    candidates_path: Path,
    corp_code_xml: Path | None,
    corp_code_zip: Path | None,
    universe_out: Path,
    provider_mapping_out: Path,
    universe_name: str,
    provider_mapping_name: str,
    universe_description: str | None = None,
    provider_mapping_description: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """KR candidate + corp-code snapshot으로 universe/mapping TOML을 생성한다."""
    try:
        return generate_kr_provider_mapping_files(
            candidates_path=candidates_path,
            corp_code_xml=corp_code_xml,
            corp_code_zip=corp_code_zip,
            universe_out=universe_out,
            provider_mapping_out=provider_mapping_out,
            universe_name=universe_name,
            provider_mapping_name=provider_mapping_name,
            universe_description=universe_description,
            provider_mapping_description=provider_mapping_description,
            force=force,
        )
    except KrProviderMappingGeneratorError as exc:
        raise GenerateKrProviderMappingCliError(exc.stage, exc.message) from exc


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Generate KR provider mapping: {status}", file=out)
    for key in (
        "stage",
        "candidates_read",
        "enabled_symbols",
        "universe_out",
        "provider_mapping_out",
        "resolved",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    try:
        corp_code_xml, corp_code_zip = _resolve_corp_code_paths(args)
        payload = run_generate_kr_provider_mapping(
            candidates_path=Path(args.candidates),
            corp_code_xml=corp_code_xml,
            corp_code_zip=corp_code_zip,
            universe_out=Path(args.universe_out),
            provider_mapping_out=Path(args.provider_mapping_out),
            universe_name=args.universe_name,
            provider_mapping_name=args.provider_mapping_name,
            universe_description=args.universe_description,
            provider_mapping_description=args.provider_mapping_description,
            force=args.force,
        )
    except GenerateKrProviderMappingCliError as exc:
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
