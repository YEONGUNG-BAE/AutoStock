#!/usr/bin/env python3
"""Fixture-first KR candidate ranking (3G3-1).

sector pool + local ranking signals → reviewable ranked JSON (+ optional 3F1 export).
network/env/API key/live OpenDART/yfinance 호출 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

from data.kr_candidate_ranker import KrCandidateRankerError, rank_kr_candidates

StageName = Literal["args", "parse", "rank", "write", "validate", "complete"]


class RankKrCandidatesCliError(Exception):
    """rank_kr_candidates CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rank KR candidates from local sector pool + fixture ranking signals "
            "(reviewable metadata only; no live API)."
        ),
    )
    parser.add_argument("--candidate-pool", required=True, help="sector-tagged candidate pool TOML path")
    parser.add_argument("--ranking-signals", required=True, help="local ranking signal TOML path")
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
    parser.add_argument("--ranked-out", required=True, help="ranked JSON artifact output path")
    parser.add_argument(
        "--selected-candidates-out",
        default=None,
        help="optional exported 3F1 candidate TOML output path",
    )
    parser.add_argument("--top-n", type=int, default=None, help="positive max ranked candidates to export")
    parser.add_argument(
        "--selection-name",
        default=None,
        help="exported candidate TOML name (required with --selected-candidates-out)",
    )
    parser.add_argument(
        "--selection-description",
        default=None,
        help="exported candidate TOML description",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing output files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _validate_cli_text_fields(args: argparse.Namespace) -> None:
    checks: tuple[tuple[str | None, str], ...] = (
        (args.selection_name, "--selection-name"),
        (args.selection_description, "--selection-description"),
    )
    for value, flag_name in checks:
        if value is not None and _contains_control_character(value):
            raise RankKrCandidatesCliError("args", f"{flag_name} contains a control character")


def _validate_positive_int(value: int | None, *, flag_name: str) -> None:
    if value is None:
        return
    if value <= 0:
        raise RankKrCandidatesCliError("args", f"{flag_name} must be a positive integer")


def _validate_cli_args(args: argparse.Namespace) -> None:
    _validate_cli_text_fields(args)
    _validate_positive_int(args.max_total, flag_name="--max-total")
    _validate_positive_int(args.max_per_sector, flag_name="--max-per-sector")
    _validate_positive_int(args.top_n, flag_name="--top-n")
    if args.selected_candidates_out is not None and args.selection_name is None:
        raise RankKrCandidatesCliError(
            "args",
            "--selection-name is required when --selected-candidates-out is provided",
        )


def run_rank_kr_candidates_cli(
    *,
    candidate_pool_path: Path,
    ranking_signals_path: Path,
    ranked_out: Path,
    sectors: set[str] | None,
    max_total: int | None,
    max_per_sector: int | None,
    include_disabled: bool,
    include_ineligible: bool,
    selected_candidates_out: Path | None,
    selection_name: str | None,
    selection_description: str | None,
    top_n: int | None,
    force: bool,
) -> dict[str, Any]:
    """CLI wrapper around rank_kr_candidates()."""
    try:
        return rank_kr_candidates(
            candidate_pool_path=candidate_pool_path,
            ranking_signals_path=ranking_signals_path,
            ranked_out=ranked_out,
            sectors=sectors,
            max_total=max_total,
            max_per_sector=max_per_sector,
            include_disabled=include_disabled,
            include_ineligible=include_ineligible,
            selected_candidates_out=selected_candidates_out,
            selection_name=selection_name,
            selection_description=selection_description,
            top_n=top_n,
            force=force,
        )
    except KrCandidateRankerError as exc:
        raise RankKrCandidatesCliError(exc.stage, exc.message) from exc


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Rank KR candidates: {status}", file=out)
    for key in (
        "stage",
        "pool_name",
        "signals_name",
        "ranked_count",
        "ranked_out",
        "selected_candidates_out",
        "selected_count",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    try:
        _validate_cli_args(args)
        sectors = set(args.sector) if args.sector else None
        payload = run_rank_kr_candidates_cli(
            candidate_pool_path=Path(args.candidate_pool),
            ranking_signals_path=Path(args.ranking_signals),
            ranked_out=Path(args.ranked_out),
            sectors=sectors,
            max_total=args.max_total,
            max_per_sector=args.max_per_sector,
            include_disabled=args.include_disabled,
            include_ineligible=args.include_ineligible,
            selected_candidates_out=(
                Path(args.selected_candidates_out) if args.selected_candidates_out else None
            ),
            selection_name=args.selection_name,
            selection_description=args.selection_description,
            top_n=args.top_n,
            force=args.force,
        )
    except RankKrCandidatesCliError as exc:
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
