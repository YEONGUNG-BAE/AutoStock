#!/usr/bin/env python3
"""KR real combined FRED + PRICE + DART context smoke helper (3E4).

operator가 concat한 combined JSONL을 8B(capped Date.md) → 8C → Scout packet까지
검증하는 orchestration helper. live API/broker/PaperLoop 호출 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from build_scout_manual_packet import run_build_scout_manual_packet
from data.source_record_context_selector import KR_REAL_SMOKE_CONTEXT_BUDGET
from domain._datetime import parse_timezone_aware_datetime
from research_source_intake import run_normal, run_validate_only
from run_date_md_smoke import run_date_md_smoke

StageName = Literal["args", "validate", "intake", "smoke", "scout", "complete"]


class CombinedContextSmokeError(Exception):
    """build_kr_real_combined_context_smoke CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "KR real combined FRED+PRICE+DART context smoke — "
            "8B capped Date.md → 8C symbol coverage → Scout packet."
        ),
    )
    parser.add_argument("--universe", required=True, help="KR real sample universe TOML path")
    parser.add_argument("--source-jsonl", required=True, help="combined DateIdSourceRecord JSONL")
    parser.add_argument("--store", required=True, help="SQLite Date-ID store path")
    parser.add_argument("--date-md-out", required=True, help="capped Date.md export path")
    parser.add_argument("--scout-out-dir", required=True, help="Scout packet output directory")
    parser.add_argument(
        "--as-of",
        default=None,
        help="timezone-aware datetime for ScoutInput.created_at (default: now UTC)",
    )
    parser.add_argument(
        "--context-budget-profile",
        choices=["kr-real-smoke"],
        default="kr-real-smoke",
        help="Date.md export context cap profile (default: kr-real-smoke)",
    )
    parser.add_argument("--force-date-md", action="store_true", help="overwrite existing Date.md")
    parser.add_argument("--force-scout", action="store_true", help="overwrite existing scout outputs")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def run_kr_real_combined_context_smoke(
    *,
    universe_path: Path,
    source_jsonl: Path,
    store_path: Path,
    date_md_out: Path,
    scout_out_dir: Path,
    now: datetime | None = None,
    context_budget_profile: str = "kr-real-smoke",
    force_date_md: bool = False,
    force_scout: bool = False,
) -> dict[str, Any]:
    """combined JSONL → capped 8B → 8C(require coverage) → Scout packet smoke."""
    if context_budget_profile != "kr-real-smoke":
        raise CombinedContextSmokeError(
            "args",
            f"unsupported context budget profile: {context_budget_profile!r}",
        )

    validate_payload = run_validate_only(source_jsonl)
    intake_payload = run_normal(
        source_jsonl=source_jsonl,
        store_path=store_path,
        date_md_out=date_md_out,
        force_date_md=force_date_md,
        context_budget_caps=KR_REAL_SMOKE_CONTEXT_BUDGET,
        context_budget_profile=context_budget_profile,
    )
    smoke_payload = run_date_md_smoke(
        universe_path=universe_path,
        date_md_path=date_md_out,
        store_path=store_path,
        require_symbol_coverage=True,
        max_date_md_bytes=60_000,
    )
    scout_payload = run_build_scout_manual_packet(
        universe_path=universe_path,
        date_md_path=date_md_out,
        store_path=store_path,
        out_dir=scout_out_dir,
        now=now or datetime.now(tz=UTC),
        market_scope="KR",
        fact_types=None,
        max_records=None,
        require_symbol_coverage=True,
        force=force_scout,
    )

    date_md_bytes = date_md_out.read_bytes()
    return {
        "status": "ok",
        "stage": "complete",
        "mode": "kr-real-combined-context-smoke",
        "context_budget_profile": context_budget_profile,
        "records_valid": validate_payload["records_valid"],
        "records_saved": intake_payload["records_saved"],
        "records_exported": intake_payload["records_exported"],
        "date_md_bytes": len(date_md_bytes),
        "date_md_out": str(date_md_out),
        "store": str(store_path),
        "scout_out_dir": str(scout_out_dir),
        "scout_records_count": scout_payload["records_count"],
        "missing_symbols": smoke_payload.get("missing_symbols", []),
        "caps": {
            "max_global_per_fact_type_source": KR_REAL_SMOKE_CONTEXT_BUDGET.max_global_per_fact_type_source,
            "max_price_per_symbol_source": KR_REAL_SMOKE_CONTEXT_BUDGET.max_price_per_symbol_source,
            "max_disclosure_per_symbol_source": KR_REAL_SMOKE_CONTEXT_BUDGET.max_disclosure_per_symbol_source,
        },
    }


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"KR real combined context smoke: {status}", file=out)
    for key in (
        "stage",
        "mode",
        "context_budget_profile",
        "records_valid",
        "records_saved",
        "records_exported",
        "date_md_bytes",
        "scout_records_count",
        "missing_symbols",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    now: datetime | None = None
    if args.as_of is not None:
        try:
            now = parse_timezone_aware_datetime(args.as_of, field_name="as_of")
        except ValueError as exc:
            payload = {"status": "error", "stage": "args", "error": str(exc)}
            _emit_result(payload, as_json=as_json, out=out)
            return 1

    try:
        payload = run_kr_real_combined_context_smoke(
            universe_path=Path(args.universe),
            source_jsonl=Path(args.source_jsonl),
            store_path=Path(args.store),
            date_md_out=Path(args.date_md_out),
            scout_out_dir=Path(args.scout_out_dir),
            now=now,
            context_budget_profile=args.context_budget_profile,
            force_date_md=args.force_date_md,
            force_scout=args.force_scout,
        )
    except CombinedContextSmokeError as exc:
        payload = {"status": "error", "stage": exc.stage, "error": exc.message}
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    _emit_result(payload, as_json=as_json, out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
