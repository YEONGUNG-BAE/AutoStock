#!/usr/bin/env python3
"""Factor input → ranked mapping workflow orchestration (3G4-2).

Chains existing 3G4-1 factor signal generator + 3G3-2 ranked mapping workflow.
network/env/API key/live OpenDART/yfinance 호출 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

from build_kr_real_ranked_mapping import (
    BuildKrRealRankedMappingError,
    run_build_kr_real_ranked_mapping,
)
from data.kr_factor_signal_generator import (
    KrFactorSignalGeneratorError,
    generate_kr_factor_signals_file,
)

StageName = Literal["args", "parse", "generate", "rank", "resolve", "write", "validate", "complete"]


class BuildKrFactorRankedMappingError(Exception):
    """build_kr_factor_ranked_mapping workflow 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build KR universe/provider mapping from factor inputs + sector pool "
            "(3G4-1 factor signals → 3G3-2 ranked mapping; local files only)."
        ),
    )
    parser.add_argument("--candidate-pool", required=True, help="sector-tagged candidate pool TOML path")
    parser.add_argument("--factor-inputs", required=True, help="local factor input TOML path")
    parser.add_argument("--corp-code-xml", help="local corp-code master XML path")
    parser.add_argument("--corp-code-zip", help="local corp-code master ZIP path")
    parser.add_argument(
        "--factor-signals-out",
        required=True,
        help="generated ranking signal TOML output path",
    )
    parser.add_argument("--ranked-out", required=True, help="ranked JSON artifact output path")
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
    parser.add_argument("--factor-output-name", required=True, help="generated ranking signal document name")
    parser.add_argument(
        "--factor-output-description",
        default=None,
        help="generated ranking signal document description",
    )
    parser.add_argument("--selection-name", required=True, help="exported 3F1 candidate TOML name")
    parser.add_argument(
        "--selection-description",
        default=None,
        help="exported 3F1 candidate TOML description",
    )
    parser.add_argument("--universe-name", required=True, help="generated universe name")
    parser.add_argument(
        "--provider-mapping-name",
        required=True,
        help="generated provider mapping registry name",
    )
    parser.add_argument(
        "--sector",
        action="append",
        default=[],
        help="sector slug filter (repeatable)",
    )
    parser.add_argument("--max-total", type=int, default=None, help="positive max pre-filter candidates")
    parser.add_argument(
        "--max-per-sector",
        type=int,
        default=None,
        help="positive max pre-filter candidates per sector",
    )
    parser.add_argument("--top-n", type=int, default=None, help="positive max ranked candidates to export")
    parser.add_argument("--force", action="store_true", help="overwrite existing output files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _contains_control_character(value: str) -> bool:
    """ASCII control(0x00–0x1F) 및 DEL(0x7F) 포함 여부."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _validate_cli_text_fields(args: argparse.Namespace) -> None:
    checks: tuple[tuple[str | None, str], ...] = (
        (args.factor_output_name, "--factor-output-name"),
        (args.factor_output_description, "--factor-output-description"),
        (args.selection_name, "--selection-name"),
        (args.selection_description, "--selection-description"),
        (args.universe_name, "--universe-name"),
        (args.provider_mapping_name, "--provider-mapping-name"),
    )
    for value, flag_name in checks:
        if value is not None and _contains_control_character(value):
            raise BuildKrFactorRankedMappingError(
                "args",
                f"{flag_name} contains a control character",
            )


def _validate_positive_int(value: int | None, *, flag_name: str) -> None:
    if value is None:
        return
    if value <= 0:
        raise BuildKrFactorRankedMappingError("args", f"{flag_name} must be a positive integer")


def _resolve_corp_code_paths(args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    corp_code_xml = Path(args.corp_code_xml) if args.corp_code_xml else None
    corp_code_zip = Path(args.corp_code_zip) if args.corp_code_zip else None
    if corp_code_xml is not None and corp_code_zip is not None:
        raise BuildKrFactorRankedMappingError(
            "args",
            "exactly one of --corp-code-xml or --corp-code-zip is required",
        )
    if corp_code_xml is None and corp_code_zip is None:
        raise BuildKrFactorRankedMappingError(
            "args",
            "exactly one of --corp-code-xml or --corp-code-zip is required",
        )
    return corp_code_xml, corp_code_zip


def run_build_kr_factor_ranked_mapping(
    *,
    candidate_pool_path: Path,
    factor_inputs_path: Path,
    corp_code_xml: Path | None,
    corp_code_zip: Path | None,
    factor_signals_out: Path,
    ranked_out: Path,
    selected_candidates_out: Path,
    universe_out: Path,
    provider_mapping_out: Path,
    factor_output_name: str,
    factor_output_description: str | None,
    selection_name: str,
    selection_description: str | None,
    universe_name: str,
    provider_mapping_name: str,
    sectors: set[str] | None = None,
    max_total: int | None = None,
    max_per_sector: int | None = None,
    top_n: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """factor input → ranking signals → ranked mapping → universe/mapping 생성 및 검증."""
    effective_factor_description = (
        factor_output_description
        if factor_output_description is not None
        else f"Generated KR ranking signals from factor inputs {factor_inputs_path.name}."
    )
    try:
        factor_payload = generate_kr_factor_signals_file(
            factor_inputs_path=factor_inputs_path,
            out_signals=factor_signals_out,
            output_name=factor_output_name,
            output_description=effective_factor_description,
            force=force,
        )
    except KrFactorSignalGeneratorError as exc:
        raise BuildKrFactorRankedMappingError(exc.stage, exc.message) from exc

    try:
        ranked_payload = run_build_kr_real_ranked_mapping(
            candidate_pool_path=candidate_pool_path,
            ranking_signals_path=factor_signals_out,
            corp_code_xml=corp_code_xml,
            corp_code_zip=corp_code_zip,
            ranked_out=ranked_out,
            selected_candidates_out=selected_candidates_out,
            universe_out=universe_out,
            provider_mapping_out=provider_mapping_out,
            selection_name=selection_name,
            selection_description=selection_description,
            universe_name=universe_name,
            provider_mapping_name=provider_mapping_name,
            sectors=sectors,
            max_total=max_total,
            max_per_sector=max_per_sector,
            top_n=top_n,
            force=force,
        )
    except BuildKrRealRankedMappingError as exc:
        raise BuildKrFactorRankedMappingError(exc.stage, exc.message) from exc

    return {
        "status": "ok",
        "stage": "complete",
        "mode": "factor-ranked-mapping-workflow",
        "factor_signals_out": str(factor_signals_out),
        "ranked_out": str(ranked_out),
        "selected_candidates_out": str(selected_candidates_out),
        "universe_out": str(universe_out),
        "provider_mapping_out": str(provider_mapping_out),
        "signals_count": factor_payload["signals_count"],
        "ranked_count": ranked_payload["ranked_count"],
        "selected_count": ranked_payload["selected_count"],
        "validation": ranked_payload["validation"],
    }


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Build KR factor ranked mapping: {status}", file=out)
    for key in (
        "stage",
        "mode",
        "factor_signals_out",
        "ranked_out",
        "selected_candidates_out",
        "universe_out",
        "provider_mapping_out",
        "signals_count",
        "ranked_count",
        "selected_count",
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
        _validate_positive_int(args.top_n, flag_name="--top-n")
        corp_code_xml, corp_code_zip = _resolve_corp_code_paths(args)
        sectors = set(args.sector) if args.sector else None
        payload = run_build_kr_factor_ranked_mapping(
            candidate_pool_path=Path(args.candidate_pool),
            factor_inputs_path=Path(args.factor_inputs),
            corp_code_xml=corp_code_xml,
            corp_code_zip=corp_code_zip,
            factor_signals_out=Path(args.factor_signals_out),
            ranked_out=Path(args.ranked_out),
            selected_candidates_out=Path(args.selected_candidates_out),
            universe_out=Path(args.universe_out),
            provider_mapping_out=Path(args.provider_mapping_out),
            factor_output_name=args.factor_output_name,
            factor_output_description=args.factor_output_description,
            selection_name=args.selection_name,
            selection_description=args.selection_description,
            universe_name=args.universe_name,
            provider_mapping_name=args.provider_mapping_name,
            sectors=sectors,
            max_total=args.max_total,
            max_per_sector=args.max_per_sector,
            top_n=args.top_n,
            force=args.force,
        )
    except BuildKrFactorRankedMappingError as exc:
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
