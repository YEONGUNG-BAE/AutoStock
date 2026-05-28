#!/usr/bin/env python3
"""Foundation 8D Scout Once manual LLM call packet builder.

Universe + Date.md/store smoke(8C) 후 ScoutInput JSON, scout_prompt.md,
scout_packet_summary.json을 생성한다. LLM을 호출하지 않으며 raw/validated Scout
출력을 생성·검증하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from data.date_id_store import SQLiteDateIdSourceStore
from decision.canonical_json import canonical_json_dumps
from domain._datetime import parse_timezone_aware_datetime
from domain.source import DateIdSourceRecord, FactType, parse_fact_type
from domain.universe import UniverseDefinition, load_universe_toml
from run_date_md_smoke import SmokeError, parse_date_md_sections, run_date_md_smoke
from scout.input_builder import ScoutInputBuilder
from scout.models import SUMMARY_ONE_LINER_MAX_LENGTH, ScoutInput

StageName = Literal[
    "args",
    "universe",
    "date_md",
    "store",
    "scout_input",
    "write",
    "complete",
]

MarketScope = Literal["KR", "US", "BOTH"]

OUTPUT_SCOUT_INPUT = "scout_input.json"
OUTPUT_SCOUT_PROMPT = "scout_prompt.md"
OUTPUT_PACKET_SUMMARY = "scout_packet_summary.json"
OUTPUT_FILES = (OUTPUT_SCOUT_INPUT, OUTPUT_SCOUT_PROMPT, OUTPUT_PACKET_SUMMARY)


class PacketError(Exception):
    """Scout manual packet builder 실패. stage와 sanitized message를 담는다."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class _FilteredStoreReader:
    """ScoutInputBuilder용 read-only filtered store adapter."""

    records: tuple[DateIdSourceRecord, ...]

    def list_records(self, fact_type: FactType | None = None) -> tuple[DateIdSourceRecord, ...]:
        if fact_type is None:
            return self.records
        return tuple(record for record in self.records if record.fact_type == fact_type)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundation 8D Scout Once manual LLM call packet builder.",
    )
    parser.add_argument("--universe", required=True, help="Universe v0 TOML path")
    parser.add_argument("--date-md", required=True, help="exported Date.md path (Foundation 8B)")
    parser.add_argument("--store", required=True, help="SQLiteDateIdSourceStore path")
    parser.add_argument("--out-dir", required=True, help="output directory for scout packet files")
    parser.add_argument("--now", default=None, help="ISO timezone-aware datetime for ScoutInput.created_at")
    parser.add_argument(
        "--market-scope",
        choices=["KR", "US", "BOTH"],
        default=None,
        help="market scope filter (default: universe.base_market)",
    )
    parser.add_argument(
        "--fact-type",
        action="append",
        default=[],
        dest="fact_types",
        help="include only matching fact_type (repeatable)",
    )
    parser.add_argument("--max-records", type=int, default=None, help="positive max ScoutInput records")
    parser.add_argument(
        "--require-symbol-coverage",
        action="store_true",
        help="fail if enabled universe symbols lack store/Date.md coverage (8C smoke)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing scout packet output files",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON summary to stdout")
    parser.add_argument("--verbose", action="store_true", help="print non-sensitive metadata to stderr")
    return parser


def _resolve_market_scope(universe: UniverseDefinition, cli_scope: str | None) -> MarketScope:
    if cli_scope is not None:
        return cli_scope  # type: ignore[return-value]
    return universe.base_market


def _enabled_pairs_for_scope(
    universe: UniverseDefinition,
    market_scope: MarketScope,
) -> set[tuple[str, str]]:
    enabled = universe.enabled_symbols
    if market_scope == "KR":
        return {(entry.market, entry.symbol) for entry in enabled if entry.market == "KR"}
    if market_scope == "US":
        return {(entry.market, entry.symbol) for entry in enabled if entry.market == "US"}
    return {(entry.market, entry.symbol) for entry in enabled}


def _select_store_records(
    store_records: tuple[DateIdSourceRecord, ...],
    *,
    date_ids: frozenset[str],
    enabled_pairs: set[tuple[str, str]],
) -> tuple[DateIdSourceRecord, ...]:
    """Date.md date_id + universe (market,symbol) + global record 필터."""
    selected: list[DateIdSourceRecord] = []
    for record in store_records:
        if record.date_id.value not in date_ids:
            continue
        if record.symbol is None and record.market is None:
            selected.append(record)
            continue
        if record.symbol is None or record.market is None:
            continue
        if (record.market, record.symbol) not in enabled_pairs:
            continue
        selected.append(record)
    return tuple(selected)


def _parse_fact_types(raw_values: list[str]) -> tuple[FactType, ...] | None:
    if not raw_values:
        return None
    try:
        return tuple(parse_fact_type(value) for value in raw_values)
    except ValueError as exc:
        raise PacketError("args", str(exc)) from exc


def _resolve_now(raw_now: str | None) -> datetime:
    if raw_now is None:
        return datetime.now(tz=UTC)
    try:
        return parse_timezone_aware_datetime(raw_now, field_name="now")
    except ValueError as exc:
        raise PacketError("args", str(exc)) from exc


def _preflight_out_dir(out_dir: Path, *, force: bool) -> None:
    existing = [name for name in OUTPUT_FILES if (out_dir / name).exists()]
    if existing and not force:
        joined = ", ".join(existing)
        raise PacketError("write", f"output files already exist: {joined} (use --force to overwrite)")


def _build_scout_prompt(
    *,
    scout_input: ScoutInput,
    date_md_text: str,
    market_scope: MarketScope,
    raw_output_path: Path,
) -> str:
    """수동 LLM copy/paste용 Scout prompt markdown을 생성한다."""
    scout_input_json = canonical_json_dumps(scout_input.to_canonical_dict())
    date_id_list = sorted({record.date_id.value for record in scout_input.records})

    lines = [
        "# Scout Manual Prompt — Paper Pilot Analysis Aid (Foundation 8D)",
        "",
        "> **Paper-pilot analysis aid only.** This is not an execution command.",
        "> Do not produce orders, trading actions, KIS calls, or live trading instructions.",
        "",
        "## Output format (strict)",
        "",
        "- Respond with **JSON only**.",
        "- Do **not** wrap JSON in markdown fences.",
        "- Do **not** include prose outside the JSON object.",
        "",
        "## Evidence rules",
        "",
        "- Cite **only** Date-IDs present in the ScoutInput records and Date.md below.",
        "- Do **not** invent sources, facts, or date_id values.",
        "- If evidence is insufficient, prefer `neutral_factors` rather than inventing facts.",
        "",
        "## ScoutSummary schema",
        "",
        "Top-level fields:",
        "",
        "- `summary_id` (string)",
        "- `created_at` (ISO timezone-aware datetime string)",
        "- `universe` (string)",
        f"- `summary_one_liner` (string, max {SUMMARY_ONE_LINER_MAX_LENGTH} characters; "
        f"downstream Scout validation enforces `SUMMARY_ONE_LINER_MAX_LENGTH`)",
        "- `positive_factors` (array)",
        "- `negative_factors` (array)",
        "- `neutral_factors` (array)",
        "- `metadata` (object)",
        "",
        "Each factor object:",
        "",
        "- `name` (string)",
        "- `summary` (string)",
        "- `reasons` (non-empty array)",
        "- `strength` (optional number)",
        "",
        "Each reason object:",
        "",
        "- `reason` (string)",
        "- `date_id` (string; must be one of the allowed Date-IDs below)",
        "- `source_name` (optional string)",
        "- `quote` (optional string)",
        "",
        f"Allowed Date-IDs for this packet: {', '.join(date_id_list) if date_id_list else '(none)'}",
        "",
        f"Market scope: {market_scope}",
        "",
        "## After manual LLM call",
        "",
        f"Save the raw JSON response manually to: `{raw_output_path}`",
        "",
        "Do **not** create validated output in this step. Foundation 8E will validate raw JSON later.",
        "",
        "## ScoutInput JSON",
        "",
        "```json",
        scout_input_json,
        "```",
        "",
        "## Date.md reference",
        "",
        date_md_text.rstrip(),
        "",
    ]
    return "\n".join(lines)


def _count_global_records(records: tuple[DateIdSourceRecord, ...]) -> int:
    return sum(1 for record in records if record.symbol is None and record.market is None)


def run_build_scout_manual_packet(
    *,
    universe_path: Path,
    date_md_path: Path,
    store_path: Path,
    out_dir: Path,
    now: datetime,
    market_scope: MarketScope | None,
    fact_types: tuple[FactType, ...] | None,
    max_records: int | None,
    require_symbol_coverage: bool,
    force: bool,
) -> dict[str, Any]:
    """Scout manual packet를 빌드하고 summary dict를 반환한다."""
    if max_records is not None:
        if not isinstance(max_records, int) or isinstance(max_records, bool):
            raise PacketError("args", "max_records must be a positive integer")
        if max_records < 1:
            raise PacketError("args", "max_records must be a positive integer")

    try:
        universe = load_universe_toml(universe_path)
    except FileNotFoundError as exc:
        raise PacketError("universe", str(exc)) from exc
    except ValueError as exc:
        raise PacketError("universe", str(exc)) from exc

    resolved_scope = _resolve_market_scope(universe, market_scope)
    enabled_pairs = _enabled_pairs_for_scope(universe, resolved_scope)

    try:
        smoke_summary = run_date_md_smoke(
            universe_path=universe_path,
            date_md_path=date_md_path,
            store_path=store_path,
            require_symbol_coverage=require_symbol_coverage,
            max_date_md_bytes=60_000,
        )
    except SmokeError as exc:
        mapped_stage: StageName = (
            exc.stage if exc.stage in {"args", "universe", "date_md", "store"} else "date_md"
        )
        raise PacketError(mapped_stage, exc.message) from exc

    if not store_path.is_file():
        raise PacketError("store", f"store not found: {store_path}")

    date_md_text = date_md_path.read_text(encoding="utf-8")
    sections = parse_date_md_sections(date_md_text)
    date_ids = frozenset(section.date_id for section in sections)

    store = SQLiteDateIdSourceStore(store_path)
    try:
        store_records = store.list_records()
    finally:
        store.close()

    filtered_records = _select_store_records(
        store_records,
        date_ids=date_ids,
        enabled_pairs=enabled_pairs,
    )

    reader = _FilteredStoreReader(records=filtered_records)
    builder = ScoutInputBuilder(reader)
    try:
        scout_input = builder.build_input(
            universe=universe.name,
            now=now,
            fact_types=fact_types,
            symbols=None,
            max_records=max_records,
            metadata={
                "foundation": "8D",
                "market_scope": resolved_scope,
                "date_ids": sorted(date_ids),
            },
        )
    except ValueError as exc:
        raise PacketError("scout_input", str(exc)) from exc

    if not scout_input.records:
        raise PacketError("scout_input", "no ScoutInput records matched Date.md/universe filters")

    _preflight_out_dir(out_dir, force=force)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_output_path = out_dir / f"scout_output.{resolved_scope.lower()}.raw.json"
    output_paths = {
        "scout_input": str(out_dir / OUTPUT_SCOUT_INPUT),
        "scout_prompt": str(out_dir / OUTPUT_SCOUT_PROMPT),
        "scout_packet_summary": str(out_dir / OUTPUT_PACKET_SUMMARY),
    }

    scout_input_path = out_dir / OUTPUT_SCOUT_INPUT
    scout_prompt_path = out_dir / OUTPUT_SCOUT_PROMPT
    summary_path = out_dir / OUTPUT_PACKET_SUMMARY

    scout_input_path.write_text(
        canonical_json_dumps(scout_input.to_canonical_dict()) + "\n",
        encoding="utf-8",
    )
    scout_prompt_path.write_text(
        _build_scout_prompt(
            scout_input=scout_input,
            date_md_text=date_md_text,
            market_scope=resolved_scope,
            raw_output_path=raw_output_path,
        ),
        encoding="utf-8",
    )

    packet_summary: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "universe_name": universe.name,
        "base_market": universe.base_market,
        "market_scope": resolved_scope,
        "records_count": len(scout_input.records),
        "date_ids_count": len({record.date_id.value for record in scout_input.records}),
        "enabled_symbols_count": len(enabled_pairs),
        "global_records_count": _count_global_records(scout_input.records),
        "output_paths": output_paths,
        "raw_output_expected_path": str(raw_output_path),
        "created_at": scout_input.created_at.isoformat(),
        "missing_symbols": smoke_summary.get("missing_symbols", []),
    }
    if fact_types is not None:
        packet_summary["fact_types"] = [fact_type.value for fact_type in fact_types]
    if max_records is not None:
        packet_summary["max_records"] = max_records

    summary_path.write_text(
        canonical_json_dumps(packet_summary) + "\n",
        encoding="utf-8",
    )

    return {
        "status": "ok",
        "stage": "complete",
        "output_paths": output_paths,
        "records_count": len(scout_input.records),
    }


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return

    status = payload.get("status", "error")
    print(f"Scout manual packet: {status}", file=out)
    for key in ("stage", "output_paths", "records_count", "error"):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr
    stage: StageName = "args"

    if args.verbose:
        print(f"verbose: universe={args.universe}", file=stderr)
        print(f"verbose: date_md={args.date_md}", file=stderr)
        print(f"verbose: store={args.store}", file=stderr)
        print(f"verbose: out_dir={args.out_dir}", file=stderr)

    try:
        fact_types = _parse_fact_types(args.fact_types)
        now = _resolve_now(args.now)
        payload = run_build_scout_manual_packet(
            universe_path=Path(args.universe),
            date_md_path=Path(args.date_md),
            store_path=Path(args.store),
            out_dir=Path(args.out_dir),
            now=now,
            market_scope=args.market_scope,
            fact_types=fact_types,
            max_records=args.max_records,
            require_symbol_coverage=args.require_symbol_coverage,
            force=args.force,
        )
    except PacketError as exc:
        stage = exc.stage
        payload = {
            "status": "error",
            "stage": stage,
            "error": exc.message,
        }
        _emit_result(payload, as_json=as_json, out=stdout)
        return 1

    _emit_result(payload, as_json=as_json, out=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
