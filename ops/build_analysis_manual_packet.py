#!/usr/bin/env python3
"""Foundation 8G Analysis Once manual LLM call packet builder.

Validated ScoutSummary + validated AllocatorDecision + portfolio state로
symbol/market별 analysis_input, analysis_prompt, analysis_packet_summary를 생성한다.
LLM을 호출하지 않으며 raw/validated Analysis output을 생성·검증하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from allocator.models import AllocatorDecision
from allocator.validator import extract_date_ids_from_allocator_decision
from build_allocator_manual_packet import PacketError as AllocatorPacketError
from build_allocator_manual_packet import load_portfolio_state
from data.date_id_store import SQLiteDateIdSourceStore
from decision.canonical_json import canonical_json_dumps
from domain._datetime import parse_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.identifiers import Percent
from domain.universe import load_universe_toml
from run_date_md_smoke import SmokeError, parse_date_md_sections
from scout.models import ScoutSummary

from analysis.models import ANALYSIS_DECISION_SCHEMA, SUMMARY_ONE_LINER_MAX_LENGTH

# Prompt hardening 상수 — tests/test_analysis_manual_packet.py에서 동일 문자열을 assert한다.
PROMPT_HEADING_REQUIRED_REASONS_SCHEMA = "## Required reasons object schema"
PROMPT_HEADING_MINIMAL_JSON_SKELETON = "## Minimal JSON skeleton"
PROMPT_REASONS_MUST_BE_OBJECTS = "Every reasons field must be an array of objects, never strings."
PROMPT_NEVER_OUTPUT_REASONS_AS_STRINGS = "Never output reasons as strings."
PROMPT_TOP_LEVEL_REASONS_REQUIRED = "Top-level reasons is required and must not be omitted."
PROMPT_REASON_OBJECT_FIELDS = "Each reason object must contain: reason, date_id, source_name, quote."
PROMPT_USE_ALLOWED_DATE_IDS_NO_BRACKETS = "Use only allowed Date-IDs, without brackets."
PROMPT_DO_NOT_INVENT_DATE_IDS = "Do not invent Date-IDs."
PROMPT_SKELETON_SHAPE_NOTE = (
    "The JSON skeleton below is a shape example. Replace example prose and IDs with values "
    "appropriate for this packet."
)
PROMPT_DO_NOT_COPY_PLACEHOLDER_PROSE = "Do not copy placeholder prose verbatim."
PROMPT_DO_NOT_COPY_PLACEHOLDER_DECISION_ID = "Do not copy placeholder decision_id verbatim."
PROMPT_INVALID_REASONS_STRING_EXAMPLE = '"reasons": ["some text"]'
PROMPT_VALID_REASONS_OBJECT_PREFIX = '"reasons": ['
SKELETON_PLACEHOLDER_DECISION_ID = "analysis-decision-example-replace-me"

StageName = Literal[
    "args",
    "scout_summary",
    "allocator_decision",
    "portfolio_state",
    "date_md",
    "store",
    "write",
    "complete",
]


class PacketError(Exception):
    """Analysis manual packet builder 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class ToleranceContext:
    """선택적 per-symbol allocator tolerance context."""

    allocator_target_weight_percent: Percent
    tolerance_percent: Percent

    def to_dict(self) -> dict[str, str]:
        return {
            "allocator_target_weight_percent": str(self.allocator_target_weight_percent.value),
            "tolerance_percent": str(self.tolerance_percent.value),
        }


def _output_stem(market: str, symbol: str) -> str:
    """파일명용 market.symbol stem (market는 lowercase)."""
    return f"{market.lower()}.{symbol}"


def output_filenames(market: str, symbol: str) -> tuple[str, str, str]:
    """8G packet builder 출력 파일명 3종."""
    stem = _output_stem(market, symbol)
    return (
        f"analysis_input.{stem}.json",
        f"analysis_prompt.{stem}.md",
        f"analysis_packet_summary.{stem}.json",
    )


def _parse_tolerance_args(
    *,
    allocator_target_weight_percent: str | None,
    tolerance_percent: str | None,
) -> ToleranceContext | None:
    if allocator_target_weight_percent is None and tolerance_percent is None:
        return None
    if (allocator_target_weight_percent is None) != (tolerance_percent is None):
        raise PacketError(
            "args",
            "allocator-target-weight-percent and tolerance-percent must both be provided or both omitted",
        )
    try:
        return ToleranceContext(
            allocator_target_weight_percent=Percent(allocator_target_weight_percent),
            tolerance_percent=Percent(tolerance_percent),
        )
    except ValueError as exc:
        raise PacketError("args", str(exc)) from exc


def _load_scout_summary(path: Path) -> ScoutSummary:
    if not path.is_file():
        raise PacketError("scout_summary", f"validated scout not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PacketError("scout_summary", f"invalid validated scout JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise PacketError("scout_summary", "validated scout must be a JSON object")
    try:
        return ScoutSummary.model_validate(payload)
    except ValueError as exc:
        raise PacketError("scout_summary", str(exc)) from exc


def _load_allocator_decision(path: Path) -> AllocatorDecision:
    if not path.is_file():
        raise PacketError("allocator_decision", f"validated allocator not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PacketError("allocator_decision", f"invalid validated allocator JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise PacketError("allocator_decision", "validated allocator must be a JSON object")
    try:
        return AllocatorDecision.model_validate(payload)
    except ValueError as exc:
        raise PacketError("allocator_decision", str(exc)) from exc


def _collect_scout_cited_date_ids(summary: ScoutSummary) -> tuple[str, ...]:
    cited: set[str] = set()
    for group in (summary.positive_factors, summary.negative_factors, summary.neutral_factors):
        for factor in group:
            for reason in factor.reasons:
                cited.add(reason.date_id.value)
    return tuple(sorted(cited))


def _collect_allocator_cited_date_ids(decision: AllocatorDecision) -> tuple[str, ...]:
    return tuple(sorted({date_id.value for date_id in extract_date_ids_from_allocator_decision(decision)}))


def _load_date_md_ids(date_md_path: Path) -> frozenset[str]:
    if not date_md_path.is_file():
        raise PacketError("date_md", f"Date.md not found: {date_md_path}")
    try:
        sections = parse_date_md_sections(date_md_path.read_text(encoding="utf-8"))
    except SmokeError as exc:
        raise PacketError("date_md", exc.message) from exc
    return frozenset(section.date_id for section in sections)


def _verify_store_consistency(*, date_md_path: Path, store_path: Path) -> None:
    if not store_path.is_file():
        raise PacketError("store", f"store not found: {store_path}")
    date_md_ids = set(_load_date_md_ids(date_md_path))
    store = SQLiteDateIdSourceStore(store_path)
    try:
        store_records = store.list_records()
    finally:
        store.close()
    store_ids = {record.date_id.value for record in store_records}
    missing = sorted(date_md_ids - store_ids)
    if missing:
        raise PacketError("store", f"Date.md date_id missing from store: {', '.join(missing)}")


def _load_allocator_validation_summary(path: Path, *, expected_decision_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PacketError("allocator_decision", f"invalid allocator validation summary JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise PacketError("allocator_decision", "allocator validation summary must be a JSON object")
    if payload.get("status") != "ok":
        raise PacketError("allocator_decision", "allocator validation summary status must be ok")
    if payload.get("decision_id") != expected_decision_id:
        raise PacketError(
            "allocator_decision",
            f"allocator validation summary decision_id mismatch: expected {expected_decision_id!r}",
        )
    freshness = payload.get("created_at_freshness_checked")
    if freshness is not None and freshness is not False:
        raise PacketError(
            "allocator_decision",
            "allocator validation summary created_at_freshness_checked must be false or absent",
        )
    return payload


def _require_universe_symbol_enabled(*, universe_path: Path, market: str, symbol: str, universe_name: str) -> None:
    try:
        universe = load_universe_toml(universe_path)
    except (FileNotFoundError, ValueError) as exc:
        raise PacketError("args", str(exc)) from exc
    if universe.name != universe_name:
        raise PacketError(
            "scout_summary",
            f"universe mismatch: context={universe_name!r}, universe TOML={universe.name!r}",
        )
    for entry in universe.enabled_symbols:
        if entry.market == market and entry.symbol == symbol:
            return
    raise PacketError(
        "args",
        f"universe missing enabled symbol: market={market!r}, symbol={symbol!r}",
    )


def _verify_cited_in_date_md(
    *,
    cited: tuple[str, ...],
    date_md_ids: frozenset[str],
    source_label: str,
) -> None:
    for date_id in cited:
        if date_id not in date_md_ids:
            raise PacketError("date_md", f"{source_label} cited date_id missing from Date.md: {date_id}")


def _extract_example_source_name(analysis_input: dict[str, Any]) -> str | None:
    """패킷 scout_summary에서 skeleton용 source_name 후보를 추출한다. 없으면 null."""
    scout_summary = analysis_input.get("scout_summary")
    if not isinstance(scout_summary, dict):
        return None
    for group_key in ("positive_factors", "negative_factors", "neutral_factors"):
        group = scout_summary.get(group_key)
        if not isinstance(group, list):
            continue
        for factor in group:
            if not isinstance(factor, dict):
                continue
            reasons = factor.get("reasons")
            if not isinstance(reasons, list):
                continue
            for reason in reasons:
                if not isinstance(reason, dict):
                    continue
                source_name = reason.get("source_name")
                if isinstance(source_name, str) and source_name.strip():
                    return source_name
    return None


def _reason_object_example(
    *,
    reason_text: str,
    date_id: str,
    source_name: str | None,
) -> dict[str, Any]:
    return {
        "reason": reason_text,
        "date_id": date_id,
        "source_name": source_name,
        "quote": None,
    }


def _build_minimal_analysis_skeleton(
    *,
    universe: str,
    market: str,
    symbol: str,
    example_date_id: str,
    source_name: str | None,
    created_at_example: str,
    allowed_date_ids: tuple[str, ...],
) -> str:
    """AnalysisDecision minimal JSON skeleton (shape example only)."""
    reason = lambda text: _reason_object_example(
        reason_text=text,
        date_id=example_date_id,
        source_name=source_name,
    )
    skeleton: dict[str, Any] = {
        "decision_id": SKELETON_PLACEHOLDER_DECISION_ID,
        "created_at": created_at_example,
        "schema_name": ANALYSIS_DECISION_SCHEMA,
        "universe": universe,
        "symbol": symbol,
        "market": market,
        "summary_one_liner": "Replace with a concise summary under 200 characters.",
        "bear": {
            "summary": "Replace with bear-case summary.",
            "risks": ["Replace with risk."],
            "reasons": [reason("Replace with evidence-backed bear reason.")],
        },
        "bull": {
            "summary": "Replace with bull-case summary.",
            "catalysts": ["Replace with catalyst."],
            "reasons": [reason("Replace with evidence-backed bull reason.")],
        },
        "risk_manager": {
            "summary": "Replace with risk-manager summary.",
            "risk_flags": ["Replace with risk flag."],
            "max_weight_percent": 25,
            "reasons": [reason("Replace with evidence-backed risk reason.")],
        },
        "fund_manager": {
            "action": "hold",
            "target_weight_percent": 10,
            "rationale": "Replace with rationale.",
            "reasons": [reason("Replace with evidence-backed fund-manager reason.")],
        },
        "reasons": [reason("Replace with top-level evidence-backed reason.")],
        "metadata": {
            "date_ids": [example_date_id],
            "foundation": "8G",
        },
    }
    return canonical_json_dumps(skeleton)


def _build_required_reasons_schema_section(*, allowed_date_ids: tuple[str, ...]) -> list[str]:
    """AnalysisReason object schema hardening 섹션."""
    lines = [
        PROMPT_HEADING_REQUIRED_REASONS_SCHEMA,
        "",
        PROMPT_REASONS_MUST_BE_OBJECTS,
        PROMPT_NEVER_OUTPUT_REASONS_AS_STRINGS,
        PROMPT_TOP_LEVEL_REASONS_REQUIRED,
        PROMPT_REASON_OBJECT_FIELDS,
        PROMPT_USE_ALLOWED_DATE_IDS_NO_BRACKETS,
        PROMPT_DO_NOT_INVENT_DATE_IDS,
        "",
        "This applies to all five locations:",
        "",
        "- top-level `reasons`",
        "- `bear.reasons`",
        "- `bull.reasons`",
        "- `risk_manager.reasons`",
        "- `fund_manager.reasons`",
        "",
        "Each reason object fields:",
        "",
        "- `reason`: string",
        "- `date_id`: string, must be one of the allowed Date-IDs, no brackets",
        "- `source_name`: string or null",
        "- `quote`: string or null",
        "",
        "Do **not** output:",
        "",
        f"- {PROMPT_INVALID_REASONS_STRING_EXAMPLE}",
        "",
        "Always output reason objects, for example:",
        "",
        '- `"reasons": [{"reason": "...", "date_id": "260528-1", "source_name": "operator-smoke", "quote": null}]`',
        "",
        "Role-level reasons are required: `bear.reasons`, `bull.reasons`, `risk_manager.reasons`, "
        "`fund_manager.reasons`.",
        "If evidence is limited, still provide at least one reason object citing an allowed Date-ID "
        "rather than omitting the field.",
        "Do **not** use bracketed Date-IDs like `[260528-1]`.",
        "",
    ]
    if len(allowed_date_ids) > 1:
        lines.extend(
            [
                "All `date_id` values in reason objects must be selected only from `allowed_date_ids` "
                "listed below.",
                "",
            ]
        )
    return lines


def _build_minimal_skeleton_section(
    *,
    analysis_input: dict[str, Any],
    allowed_date_ids: tuple[str, ...],
    market: str,
    symbol: str,
) -> list[str]:
    """Minimal JSON skeleton hardening 섹션."""
    example_date_id = allowed_date_ids[0]
    source_name = _extract_example_source_name(analysis_input)
    created_at_example = str(analysis_input.get("created_at", "2026-05-29T12:00:00+09:00"))
    skeleton_json = _build_minimal_analysis_skeleton(
        universe=str(analysis_input.get("universe", "paper-v0")),
        market=market,
        symbol=symbol,
        example_date_id=example_date_id,
        source_name=source_name,
        created_at_example=created_at_example,
        allowed_date_ids=allowed_date_ids,
    )
    lines = [
        PROMPT_HEADING_MINIMAL_JSON_SKELETON,
        "",
        PROMPT_SKELETON_SHAPE_NOTE,
        PROMPT_DO_NOT_COPY_PLACEHOLDER_PROSE,
        PROMPT_DO_NOT_COPY_PLACEHOLDER_DECISION_ID,
        "",
        "```json",
        skeleton_json,
        "```",
        "",
    ]
    return lines


def _build_analysis_prompt(
    *,
    analysis_input: dict[str, Any],
    allowed_date_ids: tuple[str, ...],
    market: str,
    symbol: str,
    raw_output_path: Path,
) -> str:
    analysis_input_json = canonical_json_dumps(analysis_input)
    lines = [
        f"# Analysis Manual Prompt — Paper Pilot Per-Symbol Aid (Foundation 8G: {market}/{symbol})",
        "",
        "> **Paper-pilot analysis aid only.** AnalysisDecision is per-symbol analysis intent only, not executable trading.",
        "> Do not produce orders, quantities, KIS calls, broker calls, or live trading instructions.",
        "",
        "## Output format (strict)",
        "",
        "- Respond with **JSON only**.",
        "- Do **not** wrap JSON in markdown fences.",
        "- Do **not** include prose outside the JSON object.",
        "",
        "## Evidence rules",
        "",
        "- Cite **only** Date-IDs listed in allowed_date_ids below.",
        "- Do **not** invent sources, facts, or date_id values.",
        "- If evidence is insufficient, prefer conservative **hold** rather than inventing facts.",
        "- All reasons must use canonical Date-ID strings **without brackets**.",
        "",
        "## AnalysisDecision schema",
        "",
        f"- schema_name must be {ANALYSIS_DECISION_SCHEMA!r}",
        f"- summary_one_liner max {SUMMARY_ONE_LINER_MAX_LENGTH} characters",
        "- Top-level fields: decision_id, created_at, schema_name, universe, symbol, market,",
        "  summary_one_liner, bear, bull, risk_manager, fund_manager, reasons, metadata",
        "- action must be one of: buy, sell, hold",
        "- bear must include: summary, risks, reasons",
        "- bull must include: summary, catalysts, reasons",
        "- risk_manager must include: summary, risk_flags, max_weight_percent (optional), reasons",
        "- fund_manager must include: action, target_weight_percent, rationale, reasons",
        "",
        *_build_required_reasons_schema_section(allowed_date_ids=allowed_date_ids),
        f"Allowed Date-IDs: {', '.join(allowed_date_ids) if allowed_date_ids else '(none)'}",
        "",
        *_build_minimal_skeleton_section(
            analysis_input=analysis_input,
            allowed_date_ids=allowed_date_ids,
            market=market,
            symbol=symbol,
        ),
        "## After manual LLM call",
        "",
        f"Save the raw JSON response manually to: `{raw_output_path}`",
        "",
        "Foundation 8G validator will validate raw JSON separately. Do not create validated output here.",
        "",
        "## analysis_input JSON",
        "",
        "```json",
        analysis_input_json,
        "```",
        "",
    ]
    return "\n".join(lines)


def _preflight_out_dir(out_dir: Path, *, filenames: tuple[str, str, str], force: bool) -> None:
    existing = [name for name in filenames if (out_dir / name).exists()]
    if existing and not force:
        joined = ", ".join(existing)
        raise PacketError("write", f"output files already exist: {joined} (use --force to overwrite)")


def run_build_analysis_manual_packet(
    *,
    validated_scout_path: Path,
    validated_allocator_path: Path,
    allocator_validation_summary_path: Path | None,
    portfolio_state_path: Path,
    date_md_path: Path,
    store_path: Path,
    universe_path: Path | None,
    market: str,
    symbol: str,
    out_dir: Path,
    now: datetime,
    tolerance: ToleranceContext | None,
    force: bool,
) -> dict[str, Any]:
    """Analysis manual packet를 빌드하고 summary dict를 반환한다."""
    try:
        market_norm = normalize_required_string(market, field_name="market")
        symbol_norm = normalize_required_string(symbol, field_name="symbol")
    except ValueError as exc:
        raise PacketError("args", str(exc)) from exc

    scout_summary = _load_scout_summary(validated_scout_path)
    allocator_decision = _load_allocator_decision(validated_allocator_path)

    try:
        portfolio_state = load_portfolio_state(portfolio_state_path)
    except AllocatorPacketError as exc:
        raise PacketError("portfolio_state", exc.message) from exc

    date_md_ids = _load_date_md_ids(date_md_path)
    _verify_store_consistency(date_md_path=date_md_path, store_path=store_path)

    if scout_summary.universe != allocator_decision.universe:
        raise PacketError(
            "scout_summary",
            "universe mismatch between ScoutSummary and AllocatorDecision: "
            f"{scout_summary.universe!r} vs {allocator_decision.universe!r}",
        )

    if allocator_validation_summary_path is not None:
        _load_allocator_validation_summary(
            allocator_validation_summary_path,
            expected_decision_id=allocator_decision.decision_id.value,
        )

    if universe_path is not None:
        _require_universe_symbol_enabled(
            universe_path=universe_path,
            market=market_norm,
            symbol=symbol_norm,
            universe_name=scout_summary.universe,
        )

    scout_cited = _collect_scout_cited_date_ids(scout_summary)
    allocator_cited = _collect_allocator_cited_date_ids(allocator_decision)
    _verify_cited_in_date_md(cited=scout_cited, date_md_ids=date_md_ids, source_label="ScoutSummary")
    _verify_cited_in_date_md(
        cited=allocator_cited,
        date_md_ids=date_md_ids,
        source_label="AllocatorDecision",
    )

    allowed_date_ids = tuple(sorted(set(scout_cited) | set(allocator_cited)))
    if not allowed_date_ids:
        raise PacketError(
            "scout_summary",
            "no allowed Date-IDs after ScoutSummary/AllocatorDecision citation union",
        )

    analysis_schema_summary = {
        "schema_name": ANALYSIS_DECISION_SCHEMA,
        "summary_one_liner_max_length": SUMMARY_ONE_LINER_MAX_LENGTH,
        "actions": ["buy", "sell", "hold"],
        "roles": ["bear", "bull", "risk_manager", "fund_manager"],
    }

    analysis_input: dict[str, Any] = {
        "created_at": now.isoformat(),
        "universe": scout_summary.universe,
        "market": market_norm,
        "symbol": symbol_norm,
        "scout_summary": scout_summary.to_canonical_dict(),
        "allocator_decision": allocator_decision.to_canonical_dict(),
        "portfolio_state": portfolio_state.to_dict(),
        "allowed_date_ids": list(allowed_date_ids),
        "analysis_schema_summary": analysis_schema_summary,
        "metadata": {
            "foundation": "8G",
            "scout_summary_id": scout_summary.summary_id.value,
            "allocator_decision_id": allocator_decision.decision_id.value,
            "portfolio_snapshot_id": portfolio_state.portfolio_snapshot.snapshot_id,
            "nav_snapshot_id": portfolio_state.nav_snapshot.snapshot_id,
        },
    }
    if tolerance is not None:
        analysis_input["allocator_tolerance_context"] = tolerance.to_dict()

    input_name, prompt_name, summary_name = output_filenames(market_norm, symbol_norm)
    _preflight_out_dir(out_dir, filenames=(input_name, prompt_name, summary_name), force=force)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = _output_stem(market_norm, symbol_norm)
    raw_output_path = out_dir / f"analysis_output.{stem}.raw.json"
    output_paths = {
        "analysis_input": str(out_dir / input_name),
        "analysis_prompt": str(out_dir / prompt_name),
        "analysis_packet_summary": str(out_dir / summary_name),
    }

    (out_dir / input_name).write_text(
        canonical_json_dumps(analysis_input) + "\n",
        encoding="utf-8",
    )
    (out_dir / prompt_name).write_text(
        _build_analysis_prompt(
            analysis_input=analysis_input,
            allowed_date_ids=allowed_date_ids,
            market=market_norm,
            symbol=symbol_norm,
            raw_output_path=raw_output_path,
        ),
        encoding="utf-8",
    )

    packet_summary: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "universe": scout_summary.universe,
        "market": market_norm,
        "symbol": symbol_norm,
        "scout_summary_id": scout_summary.summary_id.value,
        "allocator_decision_id": allocator_decision.decision_id.value,
        "allowed_date_ids": list(allowed_date_ids),
        "allowed_date_ids_count": len(allowed_date_ids),
        "output_paths": output_paths,
        "raw_output_expected_path": str(raw_output_path),
        "created_at": now.isoformat(),
        "created_at_freshness_checked": False,
        "metadata": analysis_input["metadata"],
    }
    if tolerance is not None:
        packet_summary["allocator_target_weight_percent"] = str(
            tolerance.allocator_target_weight_percent.value,
        )
        packet_summary["tolerance_percent"] = str(tolerance.tolerance_percent.value)

    (out_dir / summary_name).write_text(
        canonical_json_dumps(packet_summary) + "\n",
        encoding="utf-8",
    )

    return {
        "status": "ok",
        "stage": "complete",
        "output_paths": output_paths,
        "scout_summary_id": scout_summary.summary_id.value,
        "allocator_decision_id": allocator_decision.decision_id.value,
        "allowed_date_ids_count": len(allowed_date_ids),
        "market": market_norm,
        "symbol": symbol_norm,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundation 8G Analysis Once manual LLM call packet builder.",
    )
    parser.add_argument("--validated-scout", required=True, help="validated ScoutSummary JSON path")
    parser.add_argument("--validated-allocator", required=True, help="validated AllocatorDecision JSON path")
    parser.add_argument(
        "--allocator-validation-summary",
        default=None,
        help="optional allocator_validation_summary.json from Foundation 8F",
    )
    parser.add_argument("--portfolio-state", required=True, help="portfolio state JSON path")
    parser.add_argument("--date-md", required=True, help="exported Date.md path")
    parser.add_argument("--store", required=True, help="SQLiteDateIdSourceStore path")
    parser.add_argument("--universe", default=None, help="optional Universe v0 TOML path")
    parser.add_argument("--market", required=True, help="target market for per-symbol analysis")
    parser.add_argument("--symbol", required=True, help="target symbol for per-symbol analysis")
    parser.add_argument("--out-dir", required=True, help="output directory for analysis packet files")
    parser.add_argument("--now", default=None, help="ISO timezone-aware datetime for analysis_input.created_at")
    parser.add_argument(
        "--allocator-target-weight-percent",
        default=None,
        help="optional per-symbol allocator target weight (requires --tolerance-percent)",
    )
    parser.add_argument(
        "--tolerance-percent",
        default=None,
        help="optional allocator tolerance band percent (requires --allocator-target-weight-percent)",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing analysis packet output files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON summary to stdout")
    parser.add_argument("--verbose", action="store_true", help="print non-sensitive metadata to stderr")
    return parser


def _resolve_now(raw_now: str | None) -> datetime:
    if raw_now is None:
        return datetime.now(tz=UTC)
    try:
        return parse_timezone_aware_datetime(raw_now, field_name="now")
    except ValueError as exc:
        raise PacketError("args", str(exc)) from exc


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Analysis manual packet: {status}", file=out)
    for key in (
        "stage",
        "output_paths",
        "scout_summary_id",
        "allocator_decision_id",
        "allowed_date_ids_count",
        "market",
        "symbol",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr

    if args.verbose:
        print(f"verbose: validated_scout={args.validated_scout}", file=stderr)
        print(f"verbose: validated_allocator={args.validated_allocator}", file=stderr)
        print(f"verbose: market={args.market} symbol={args.symbol}", file=stderr)
        print(f"verbose: out_dir={args.out_dir}", file=stderr)

    try:
        payload = run_build_analysis_manual_packet(
            validated_scout_path=Path(args.validated_scout),
            validated_allocator_path=Path(args.validated_allocator),
            allocator_validation_summary_path=(
                Path(args.allocator_validation_summary) if args.allocator_validation_summary else None
            ),
            portfolio_state_path=Path(args.portfolio_state),
            date_md_path=Path(args.date_md),
            store_path=Path(args.store),
            universe_path=Path(args.universe) if args.universe else None,
            market=args.market,
            symbol=args.symbol,
            out_dir=Path(args.out_dir),
            now=_resolve_now(args.now),
            tolerance=_parse_tolerance_args(
                allocator_target_weight_percent=args.allocator_target_weight_percent,
                tolerance_percent=args.tolerance_percent,
            ),
            force=args.force,
        )
    except PacketError as exc:
        payload = {"status": "error", "stage": exc.stage, "error": exc.message}
        _emit_result(payload, as_json=args.json, out=stdout)
        return 1

    _emit_result(payload, as_json=args.json, out=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
