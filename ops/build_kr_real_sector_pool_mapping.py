#!/usr/bin/env python3
"""Operator-local KR sector pool → selected candidates → universe/mapping workflow (3G2).

Chains existing 3G1 export + 3F1 generator + provider mapping validation.
network/env/API key/live OpenDART/yfinance 호출 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

from data.kr_candidate_pool import (
    KrCandidatePoolError,
    export_selected_candidates,
    parse_kr_candidate_pool_toml,
)
from data.kr_provider_mapping_generator import (
    KrProviderMappingGeneratorError,
    generate_kr_provider_mapping_files,
)
from data.provider_mapping_registry import (
    ProviderMappingError,
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml

StageName = Literal["args", "parse", "select", "resolve", "write", "validate", "complete"]


class BuildKrRealSectorPoolMappingError(Exception):
    """build_kr_real_sector_pool_mapping workflow 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build KR universe/provider mapping from sector-tagged candidate pool "
            "(3G1 select/export → 3F1 generate → validate; local files only)."
        ),
    )
    parser.add_argument("--candidate-pool", required=True, help="sector-tagged candidate pool TOML path")
    parser.add_argument("--corp-code-xml", help="local corp-code master XML path")
    parser.add_argument("--corp-code-zip", help="local corp-code master ZIP path")
    parser.add_argument(
        "--sector",
        action="append",
        default=[],
        help="sector slug filter (repeatable)",
    )
    parser.add_argument("--max-total", type=int, default=None, help="positive max selected candidates")
    parser.add_argument(
        "--max-per-sector",
        type=int,
        default=None,
        help="positive max selected candidates per sector",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="include candidates with enabled=false",
    )
    parser.add_argument(
        "--include-ineligible",
        action="store_true",
        help="include candidates with eligible=false",
    )
    parser.add_argument(
        "--selected-candidates-out",
        required=True,
        help="exported 3F1 candidate TOML output path",
    )
    parser.add_argument("--universe-out", required=True, help="generated universe TOML output path")
    parser.add_argument(
        "--provider-mapping-out",
        required=True,
        help="generated provider mapping TOML output path",
    )
    parser.add_argument(
        "--selection-name",
        required=True,
        help="exported 3F1 candidate TOML name",
    )
    parser.add_argument(
        "--selection-description",
        required=True,
        help="exported 3F1 candidate TOML description",
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


def _contains_control_character(value: str) -> bool:
    """ASCII control(0x00–0x1F) 및 DEL(0x7F) 포함 여부."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _validate_cli_text_fields(args: argparse.Namespace) -> None:
    checks: tuple[tuple[str | None, str], ...] = (
        (args.selection_name, "--selection-name"),
        (args.selection_description, "--selection-description"),
        (args.universe_name, "--universe-name"),
        (args.provider_mapping_name, "--provider-mapping-name"),
        (args.universe_description, "--universe-description"),
        (args.provider_mapping_description, "--provider-mapping-description"),
    )
    for value, flag_name in checks:
        if value is not None and _contains_control_character(value):
            raise BuildKrRealSectorPoolMappingError(
                "args",
                f"{flag_name} contains a control character",
            )


def _validate_positive_int(value: int | None, *, flag_name: str) -> None:
    if value is None:
        return
    if value <= 0:
        raise BuildKrRealSectorPoolMappingError("args", f"{flag_name} must be a positive integer")


def _resolve_corp_code_paths(args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    corp_code_xml = Path(args.corp_code_xml) if args.corp_code_xml else None
    corp_code_zip = Path(args.corp_code_zip) if args.corp_code_zip else None
    if corp_code_xml is not None and corp_code_zip is not None:
        raise BuildKrRealSectorPoolMappingError(
            "args",
            "exactly one of --corp-code-xml or --corp-code-zip is required",
        )
    if corp_code_xml is None and corp_code_zip is None:
        raise BuildKrRealSectorPoolMappingError(
            "args",
            "exactly one of --corp-code-xml or --corp-code-zip is required",
        )
    return corp_code_xml, corp_code_zip


def run_build_kr_real_sector_pool_mapping(
    *,
    candidate_pool_path: Path,
    corp_code_xml: Path | None,
    corp_code_zip: Path | None,
    selected_candidates_out: Path,
    universe_out: Path,
    provider_mapping_out: Path,
    selection_name: str,
    selection_description: str,
    universe_name: str,
    provider_mapping_name: str,
    universe_description: str | None = None,
    provider_mapping_description: str | None = None,
    sectors: set[str] | None = None,
    max_total: int | None = None,
    max_per_sector: int | None = None,
    include_disabled: bool = False,
    include_ineligible: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """sector pool → selected candidates → universe/mapping 생성 및 검증."""
    try:
        pool = parse_kr_candidate_pool_toml(candidate_pool_path)
    except KrCandidatePoolError as exc:
        raise BuildKrRealSectorPoolMappingError(exc.stage, exc.message) from exc

    try:
        selection_payload = export_selected_candidates(
            pool,
            out_candidates=selected_candidates_out,
            export_name=selection_name,
            export_description=selection_description,
            sectors=sectors,
            max_total=max_total,
            max_per_sector=max_per_sector,
            include_disabled=include_disabled,
            include_ineligible=include_ineligible,
            force=force,
        )
    except KrCandidatePoolError as exc:
        raise BuildKrRealSectorPoolMappingError(exc.stage, exc.message) from exc

    try:
        generate_kr_provider_mapping_files(
            candidates_path=selected_candidates_out,
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
        raise BuildKrRealSectorPoolMappingError(exc.stage, exc.message) from exc

    try:
        universe = load_universe_toml(universe_out)
        registry = load_provider_mapping_toml(provider_mapping_out)
        validate_provider_mappings_cover_universe(
            registry,
            universe,
            require_yfinance=True,
            require_dart=True,
        )
    except (FileNotFoundError, ValueError, ProviderMappingError) as exc:
        raise BuildKrRealSectorPoolMappingError("validate", str(exc)) from exc

    selected_symbols = [entry["symbol"] for entry in selection_payload["selected"]]
    return {
        "status": "ok",
        "stage": "complete",
        "pool_name": pool.name,
        "selected_candidates": selection_payload["candidates_selected"],
        "selected_symbols": selected_symbols,
        "selected_sectors": selection_payload["sectors"],
        "selected_candidates_out": str(selected_candidates_out),
        "universe_out": str(universe_out),
        "provider_mapping_out": str(provider_mapping_out),
        "universe_name": universe_name,
        "provider_mapping_name": provider_mapping_name,
        "validation": {
            "require_yfinance": True,
            "require_dart": True,
            "status": "ok",
        },
    }


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Build KR real sector pool mapping: {status}", file=out)
    for key in (
        "stage",
        "pool_name",
        "selected_candidates",
        "selected_symbols",
        "selected_sectors",
        "selected_candidates_out",
        "universe_out",
        "provider_mapping_out",
        "universe_name",
        "provider_mapping_name",
        "validation",
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
        _validate_positive_int(args.max_total, flag_name="--max-total")
        _validate_positive_int(args.max_per_sector, flag_name="--max-per-sector")
        corp_code_xml, corp_code_zip = _resolve_corp_code_paths(args)
        sectors = set(args.sector) if args.sector else None
        payload = run_build_kr_real_sector_pool_mapping(
            candidate_pool_path=Path(args.candidate_pool),
            corp_code_xml=corp_code_xml,
            corp_code_zip=corp_code_zip,
            selected_candidates_out=Path(args.selected_candidates_out),
            universe_out=Path(args.universe_out),
            provider_mapping_out=Path(args.provider_mapping_out),
            selection_name=args.selection_name,
            selection_description=args.selection_description,
            universe_name=args.universe_name,
            provider_mapping_name=args.provider_mapping_name,
            universe_description=args.universe_description,
            provider_mapping_description=args.provider_mapping_description,
            sectors=sectors,
            max_total=args.max_total,
            max_per_sector=args.max_per_sector,
            include_disabled=args.include_disabled,
            include_ineligible=args.include_ineligible,
            force=args.force,
        )
    except BuildKrRealSectorPoolMappingError as exc:
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
