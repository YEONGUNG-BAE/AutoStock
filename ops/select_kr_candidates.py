#!/usr/bin/env python3
"""Fixture-first sector-tagged KR candidate pool selector (3G1).

sector-tagged candidate pool TOML → selected 3F1 candidate TOML.
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
    select_candidates,
)

StageName = Literal["args", "parse", "select", "write", "validate", "complete"]


class SelectKrCandidatesCliError(Exception):
    """select_kr_candidates CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Select sector-tagged KR candidates from local pool TOML and export "
            "to 3F1 candidate TOML (fixture-first; no live API)."
        ),
    )
    parser.add_argument("--candidate-pool", required=True, help="sector-tagged candidate pool TOML path")
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
    parser.add_argument("--out-candidates", required=True, help="exported 3F1 candidate TOML output path")
    parser.add_argument(
        "--export-name",
        default=None,
        help="exported candidate TOML name (default: <pool-name>-selected)",
    )
    parser.add_argument(
        "--export-description",
        default=None,
        help="exported candidate TOML description",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing output file")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _contains_control_character(value: str) -> bool:
    """ASCII control(0x00–0x1F) 및 DEL(0x7F) 포함 여부."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _validate_cli_text_fields(args: argparse.Namespace) -> None:
    checks: tuple[tuple[str | None, str], ...] = (
        (args.export_name, "--export-name"),
        (args.export_description, "--export-description"),
    )
    for value, flag_name in checks:
        if value is not None and _contains_control_character(value):
            raise SelectKrCandidatesCliError(
                "args",
                f"{flag_name} contains a control character",
            )


def _validate_positive_int(value: int | None, *, flag_name: str) -> None:
    if value is None:
        return
    if value <= 0:
        raise SelectKrCandidatesCliError("args", f"{flag_name} must be a positive integer")


def run_select_kr_candidates(
    *,
    candidate_pool_path: Path,
    out_candidates: Path,
    sectors: set[str] | None,
    max_total: int | None,
    max_per_sector: int | None,
    include_disabled: bool,
    include_ineligible: bool,
    export_name: str | None,
    export_description: str | None,
    force: bool,
) -> dict[str, Any]:
    """sector-tagged pool에서 subset을 선택해 3F1 candidate TOML로 export한다."""
    try:
        pool = parse_kr_candidate_pool_toml(candidate_pool_path)
    except KrCandidatePoolError as exc:
        raise SelectKrCandidatesCliError(exc.stage, exc.message) from exc

    effective_export_name = export_name or f"{pool.name}-selected"
    effective_export_description = (
        export_description
        if export_description is not None
        else f"Selected KR candidates exported from pool {pool.name}."
    )

    try:
        return export_selected_candidates(
            pool,
            out_candidates=out_candidates,
            export_name=effective_export_name,
            export_description=effective_export_description,
            sectors=sectors,
            max_total=max_total,
            max_per_sector=max_per_sector,
            include_disabled=include_disabled,
            include_ineligible=include_ineligible,
            force=force,
        )
    except KrCandidatePoolError as exc:
        raise SelectKrCandidatesCliError(exc.stage, exc.message) from exc


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Select KR candidates: {status}", file=out)
    for key in (
        "stage",
        "pool_name",
        "candidates_read",
        "candidates_selected",
        "sectors",
        "out_candidates",
        "selected",
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
        sectors = set(args.sector) if args.sector else None
        payload = run_select_kr_candidates(
            candidate_pool_path=Path(args.candidate_pool),
            out_candidates=Path(args.out_candidates),
            sectors=sectors,
            max_total=args.max_total,
            max_per_sector=args.max_per_sector,
            include_disabled=args.include_disabled,
            include_ineligible=args.include_ineligible,
            export_name=args.export_name,
            export_description=args.export_description,
            force=args.force,
        )
    except SelectKrCandidatesCliError as exc:
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
