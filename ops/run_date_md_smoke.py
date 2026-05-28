#!/usr/bin/env python3
"""Foundation 8C Date.md prompt-reference smoke.

Validates Universe v0 TOML and exported Date.md (Foundation 8B format).
Optional store consistency and symbol coverage checks.
Does not call LLM/Ollama, external APIs, KIS, brokers, or trading paths.
Does not write output files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO

from data.date_id_store import SQLiteDateIdSourceStore
from domain.identifiers import DateId
from domain.source import DateIdSourceRecord
from domain.universe import UniverseDefinition, load_universe_toml

StageName = Literal["args", "universe", "date_md", "store", "coverage", "complete"]

DATE_ID_HEADING_RE = re.compile(r"^## \[([^\]]+)\]\s*$")
FIELD_SOURCE_TIMESTAMP = "**source_timestamp:**"
FIELD_SUMMARY = "**summary:**"
FIELD_PAYLOAD_HASH = "**payload_hash:**"
SYMBOL_LINE_PREFIX = "- **symbol:**"
MARKET_LINE_PREFIX = "- **market:**"


class SmokeError(Exception):
    """Date.md smoke 실패. stage와 sanitized message를 담는다."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class DateMdSection:
    """Foundation 8B Date.md record section."""

    date_id: str
    symbol: str | None
    market: str | None
    has_source_timestamp: bool
    has_summary: bool
    has_payload_hash: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundation 8C Universe v0 + Date.md prompt-reference smoke.",
    )
    parser.add_argument(
        "--universe",
        required=True,
        help="Universe v0 TOML path",
    )
    parser.add_argument(
        "--date-md",
        required=True,
        help="exported Date.md path (Foundation 8B format)",
    )
    parser.add_argument(
        "--store",
        default=None,
        help="optional SQLiteDateIdSourceStore path for consistency/coverage",
    )
    parser.add_argument(
        "--require-symbol-coverage",
        action="store_true",
        help="fail if enabled universe symbols lack matching Date.md/store records",
    )
    parser.add_argument(
        "--max-date-md-bytes",
        type=int,
        default=60_000,
        help="maximum Date.md byte size (default: 60000)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON summary to stdout",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print non-sensitive metadata to stderr",
    )
    return parser


def _extract_field_value(line: str, prefix: str) -> str | None:
    """Foundation 8B bold field line에서 VALUE를 추출한다."""
    stripped = line.strip()
    if not stripped.startswith(prefix):
        return None
    value = stripped[len(prefix) :].strip()
    return value or None


def parse_date_md_sections(text: str) -> tuple[DateMdSection, ...]:
    """Foundation 8B Date.md format에서 record section을 deterministic하게 파싱한다."""
    if not text.strip():
        raise SmokeError("date_md", "Date.md is empty")

    sections: list[DateMdSection] = []
    seen_date_ids: set[str] = set()
    current_date_id: str | None = None
    current_symbol: str | None = None
    current_market: str | None = None
    has_source_timestamp = False
    has_summary = False
    has_payload_hash = False

    def _flush_section() -> None:
        nonlocal current_date_id, current_symbol, current_market
        nonlocal has_source_timestamp, has_summary, has_payload_hash
        if current_date_id is None:
            return
        if current_date_id in seen_date_ids:
            raise SmokeError("date_md", f"duplicate Date-ID section: {current_date_id}")
        seen_date_ids.add(current_date_id)
        if not has_source_timestamp:
            raise SmokeError("date_md", f"Date-ID section missing source_timestamp: {current_date_id}")
        if not has_summary:
            raise SmokeError("date_md", f"Date-ID section missing summary: {current_date_id}")
        if not has_payload_hash:
            raise SmokeError("date_md", f"Date-ID section missing payload_hash: {current_date_id}")
        sections.append(
            DateMdSection(
                date_id=current_date_id,
                symbol=current_symbol,
                market=current_market,
                has_source_timestamp=has_source_timestamp,
                has_summary=has_summary,
                has_payload_hash=has_payload_hash,
            )
        )
        current_date_id = None
        current_symbol = None
        current_market = None
        has_source_timestamp = False
        has_summary = False
        has_payload_hash = False

    for raw_line in text.splitlines():
        heading_match = DATE_ID_HEADING_RE.match(raw_line.strip())
        if heading_match:
            _flush_section()
            date_id_token = heading_match.group(1).strip()
            try:
                current_date_id = DateId(date_id_token).value
            except ValueError as exc:
                raise SmokeError("date_md", f"invalid Date-ID heading: {date_id_token}") from exc
            continue

        if current_date_id is None:
            continue

        if FIELD_SOURCE_TIMESTAMP in raw_line:
            has_source_timestamp = True
        if FIELD_SUMMARY in raw_line:
            has_summary = True
        if FIELD_PAYLOAD_HASH in raw_line:
            has_payload_hash = True

        symbol_value = _extract_field_value(raw_line, SYMBOL_LINE_PREFIX)
        if symbol_value is not None:
            current_symbol = symbol_value
        market_value = _extract_field_value(raw_line, MARKET_LINE_PREFIX)
        if market_value is not None:
            current_market = market_value

    _flush_section()

    if not sections:
        raise SmokeError("date_md", "Date.md contains no Date-ID sections (expected ## [YYMMDD-N])")

    return tuple(sections)


def _symbol_pairs_from_sections(sections: tuple[DateMdSection, ...]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for section in sections:
        if section.symbol is not None and section.market is not None:
            pairs.add((section.market, section.symbol))
    return pairs


def _symbol_pairs_from_store_records(
    records: tuple[DateIdSourceRecord, ...],
) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for record in records:
        if record.symbol is None or record.market is None:
            continue
        pairs.add((record.market, record.symbol))
    return pairs


def _enabled_universe_pairs(universe: UniverseDefinition) -> set[tuple[str, str]]:
    return {(entry.market, entry.symbol) for entry in universe.enabled_symbols}


def _compute_missing_symbols(
    *,
    required: set[tuple[str, str]],
    referenced: set[tuple[str, str]],
) -> list[str]:
    missing = sorted(required - referenced)
    return [f"{market}:{symbol}" for market, symbol in missing]


def run_date_md_smoke(
    *,
    universe_path: Path,
    date_md_path: Path,
    store_path: Path | None,
    require_symbol_coverage: bool,
    max_date_md_bytes: int,
) -> dict[str, Any]:
    """Universe + Date.md (+ optional store) smoke를 실행하고 summary dict를 반환한다."""
    if max_date_md_bytes <= 0:
        raise SmokeError("args", "--max-date-md-bytes must be a positive integer")

    try:
        universe = load_universe_toml(universe_path)
    except FileNotFoundError as exc:
        raise SmokeError("universe", str(exc)) from exc
    except ValueError as exc:
        raise SmokeError("universe", str(exc)) from exc

    if not date_md_path.is_file():
        raise SmokeError("date_md", f"Date.md not found: {date_md_path}")

    date_md_bytes = date_md_path.read_bytes()
    if len(date_md_bytes) > max_date_md_bytes:
        raise SmokeError(
            "date_md",
            f"Date.md exceeds max size: {len(date_md_bytes)} > {max_date_md_bytes} bytes",
        )

    sections = parse_date_md_sections(date_md_path.read_text(encoding="utf-8"))
    date_ids = {section.date_id for section in sections}

    store_records: tuple[DateIdSourceRecord, ...] = ()
    if store_path is not None:
        if not store_path.is_file():
            raise SmokeError("store", f"store not found: {store_path}")
        store = SQLiteDateIdSourceStore(store_path)
        try:
            store_records = store.list_records()
        finally:
            store.close()

        store_date_ids = {record.date_id.value for record in store_records}
        missing_in_store = sorted(date_ids - store_date_ids)
        if missing_in_store:
            raise SmokeError(
                "store",
                f"Date.md date_id missing from store: {', '.join(missing_in_store)}",
            )

    if store_path is not None:
        referenced_pairs = _symbol_pairs_from_store_records(store_records)
    else:
        referenced_pairs = _symbol_pairs_from_sections(sections)

    required_pairs = _enabled_universe_pairs(universe)
    missing_symbols = _compute_missing_symbols(required=required_pairs, referenced=referenced_pairs)

    if require_symbol_coverage and missing_symbols:
        raise SmokeError(
            "coverage",
            f"missing symbol coverage for enabled universe symbols: {', '.join(missing_symbols)}",
        )

    covered_symbols_count = len(required_pairs & referenced_pairs)
    return {
        "status": "ok",
        "stage": "complete",
        "universe_name": universe.name,
        "base_market": universe.base_market,
        "enabled_symbols_count": len(required_pairs),
        "date_ids_count": len(date_ids),
        "date_md_bytes": len(date_md_bytes),
        "store_records_count": len(store_records) if store_path is not None else None,
        "covered_symbols_count": covered_symbols_count,
        "missing_symbols": missing_symbols,
    }


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return

    status = payload.get("status", "error")
    print(f"Date.md smoke: {status}", file=out)
    for key in (
        "stage",
        "universe_name",
        "base_market",
        "enabled_symbols_count",
        "date_ids_count",
        "date_md_bytes",
        "store_records_count",
        "covered_symbols_count",
        "missing_symbols",
        "error",
    ):
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
        if args.store:
            print(f"verbose: store={args.store}", file=stderr)
        print(
            f"verbose: require_symbol_coverage={'yes' if args.require_symbol_coverage else 'no'}",
            file=stderr,
        )

    try:
        payload = run_date_md_smoke(
            universe_path=Path(args.universe),
            date_md_path=Path(args.date_md),
            store_path=Path(args.store) if args.store else None,
            require_symbol_coverage=args.require_symbol_coverage,
            max_date_md_bytes=args.max_date_md_bytes,
        )
    except SmokeError as exc:
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
