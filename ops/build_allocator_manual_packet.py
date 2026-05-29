#!/usr/bin/env python3
"""Foundation 8F Allocator Once manual LLM call packet builder.

Validated ScoutSummary + portfolio state + Date.md/store context로
allocator_input.json, allocator_prompt.md, allocator_packet_summary.json을 생성한다.
LLM을 호출하지 않으며 raw/validated Allocator output을 생성·검증하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TextIO

from allocator.models import ALLOCATOR_DECISION_SCHEMA, SUMMARY_ONE_LINER_MAX_LENGTH

# Prompt hardening 상수 — tests/test_allocator_manual_packet.py에서 동일 문자열을 assert한다.
PROMPT_HEADING_REQUIRED_ALLOCATOR_REASON_SCHEMA = "## Required allocator reason object schema"
PROMPT_HEADING_MINIMAL_ALLOCATOR_SKELETON = "## Minimal AllocatorDecision JSON skeleton"
PROMPT_REASONS_MUST_BE_OBJECTS = "Every reasons field must be an array of objects, never strings."
PROMPT_NEVER_OUTPUT_REASONS_AS_STRINGS = "Never output reasons as strings."
PROMPT_TOP_LEVEL_REASONS_REQUIRED = "Top-level reasons is required and must not be omitted."
PROMPT_REASON_OBJECT_FIELDS = "Each reason object must contain: reason, date_id, source_name, quote."
PROMPT_USE_ALLOWED_DATE_IDS_NO_BRACKETS = "Use only allowed Date-IDs, without brackets."
PROMPT_DO_NOT_INVENT_DATE_IDS = "Do not invent Date-IDs."
PROMPT_DECISION_ID_REQUIRED = "decision_id is required."
PROMPT_CREATED_AT_REQUIRED = "created_at is required."
PROMPT_UNIVERSE_REQUIRED = "universe is required."
PROMPT_SIGNAL_SUMMARY_REQUIRED = "signal_summary is required."
PROMPT_CASH_MANAGER_REQUIRED = "cash_manager is required."
PROMPT_ASSET_ALLOCATOR_SUMMARY_REASONS_REQUIRED = (
    "asset_allocator.summary and asset_allocator.reasons are required."
)
PROMPT_CONSISTENCY_CHECKER_SUMMARY_REASONS_REQUIRED = (
    "consistency_checker.summary and consistency_checker.reasons are required."
)
PROMPT_CASH_POLICY_RATIONALE_REASONS_REQUIRED = (
    "cash_policy.rationale and cash_policy.reasons are required."
)
PROMPT_SKELETON_SHAPE_NOTE = (
    "The JSON skeleton below is a shape example. Replace example prose and IDs with values "
    "appropriate for this packet."
)
PROMPT_DO_NOT_COPY_PLACEHOLDER_PROSE = "Do not copy placeholder prose verbatim."
PROMPT_DO_NOT_COPY_PLACEHOLDER_DECISION_ID = "Do not copy placeholder decision_id verbatim."
PROMPT_INVALID_REASONS_STRING_EXAMPLE = '"reasons": ["some text"]'
PROMPT_VALID_REASONS_OBJECT_PREFIX = '"reasons": ['
SKELETON_PLACEHOLDER_DECISION_ID = "allocator-decision-example-replace-me"
SKELETON_GOLD_POLICY_MODE = "normal"
SKELETON_TARGET_WEIGHTS: dict[str, str] = {"kr": "80", "us": "0", "gold": "20"}
SKELETON_CASH_PERCENT = "20"
PROMPT_TARGET_WEIGHTS_SUM_100 = "target_weights.kr + target_weights.us + target_weights.gold must equal 100."
PROMPT_GOLD_NORMAL_BAND = 'If gold_policy_mode is "normal", gold target must be within 18~22.'
PROMPT_GOLD_EXCEPTION_BAND = 'If gold_policy_mode is "exception", gold target must be within 15~25.'
PROMPT_GOLD_ZERO_INVALID = "gold=0 is invalid in both normal and exception modes under the current validator."
PROMPT_CASH_TARGET_EQUALS_RECOMMENDED = (
    "cash_policy.cash_target_percent must equal cash_manager.recommended_cash_percent."
)
PROMPT_CONTROLLED_KR_SYNTHETIC_SKELETON = (
    'For the controlled KR synthetic skeleton, use gold_policy_mode "normal" with '
    "target_weights kr=80, us=0, gold=20."
)
from data.date_id_store import SQLiteDateIdSourceStore
from decision.canonical_json import canonical_json_dumps
from domain._datetime import parse_timezone_aware_datetime
from domain._decimal import to_decimal
from domain._strings import normalize_required_string
from domain.enums import Market
from domain.portfolio import NavSnapshot, PortfolioSnapshot
from domain.universe import load_universe_toml
from run_date_md_smoke import SmokeError, parse_date_md_sections
from scout.models import ScoutSummary

StageName = Literal[
    "args",
    "scout_summary",
    "portfolio_state",
    "date_md",
    "store",
    "write",
    "complete",
]

OUTPUT_ALLOCATOR_INPUT = "allocator_input.json"
OUTPUT_ALLOCATOR_PROMPT = "allocator_prompt.md"
OUTPUT_PACKET_SUMMARY = "allocator_packet_summary.json"
OUTPUT_FILES = (OUTPUT_ALLOCATOR_INPUT, OUTPUT_ALLOCATOR_PROMPT, OUTPUT_PACKET_SUMMARY)


class PacketError(Exception):
    """Allocator manual packet builder 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class PortfolioStateBundle:
    """Foundation 8F local paper portfolio state bundle."""

    version: int
    portfolio_snapshot: PortfolioSnapshot
    nav_snapshot: NavSnapshot
    constraints: dict[str, Any]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "portfolio_snapshot": self.portfolio_snapshot.model_dump(mode="json"),
            "nav_snapshot": self.nav_snapshot.model_dump(mode="json"),
            "constraints": self.constraints,
            "metadata": self.metadata,
        }


def _parse_percent(value: Any, *, field_name: str) -> Decimal:
    parsed = to_decimal(value, field_name=field_name)
    if parsed < Decimal("0") or parsed > Decimal("100"):
        raise ValueError(f"{field_name} must be between 0 and 100.")
    return parsed


def load_portfolio_state(path: Path) -> PortfolioStateBundle:
    """Portfolio state JSON 파일을 로드하고 Foundation 8F convention으로 검증한다."""
    if not path.is_file():
        raise PacketError("portfolio_state", f"portfolio state not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PacketError("portfolio_state", f"invalid portfolio state JSON: {exc.msg}") from exc

    if not isinstance(raw, dict):
        raise PacketError("portfolio_state", "portfolio state root must be a JSON object")

    version = raw.get("version")
    if version != 1:
        raise PacketError("portfolio_state", "version must be exactly 1")

    try:
        portfolio_snapshot = PortfolioSnapshot.model_validate(raw.get("portfolio_snapshot"))
        nav_snapshot = NavSnapshot.model_validate(raw.get("nav_snapshot"))
    except ValueError as exc:
        raise PacketError("portfolio_state", str(exc)) from exc

    if portfolio_snapshot.total_nav_krw != nav_snapshot.total_nav_krw:
        raise PacketError(
            "portfolio_state",
            "portfolio_snapshot.total_nav_krw must equal nav_snapshot.total_nav_krw",
        )
    if portfolio_snapshot.cash_krw != nav_snapshot.cash_krw:
        raise PacketError(
            "portfolio_state",
            "portfolio_snapshot.cash_krw must equal nav_snapshot.cash_krw",
        )

    constraints_raw = raw.get("constraints")
    if not isinstance(constraints_raw, dict):
        raise PacketError("portfolio_state", "constraints must be a JSON object")

    try:
        max_position = _parse_percent(
            constraints_raw.get("max_position_weight_percent"),
            field_name="max_position_weight_percent",
        )
        max_market = _parse_percent(
            constraints_raw.get("max_single_market_weight_percent"),
            field_name="max_single_market_weight_percent",
        )
        min_cash = _parse_percent(constraints_raw.get("min_cash_percent"), field_name="min_cash_percent")
        max_cash = _parse_percent(constraints_raw.get("max_cash_percent"), field_name="max_cash_percent")
    except ValueError as exc:
        raise PacketError("portfolio_state", str(exc)) from exc

    if max_position <= Decimal("0"):
        raise PacketError("portfolio_state", "max_position_weight_percent must be > 0")
    if min_cash > max_cash:
        raise PacketError("portfolio_state", "min_cash_percent must be <= max_cash_percent")

    allowed_markets_raw = constraints_raw.get("allowed_markets")
    if not isinstance(allowed_markets_raw, list) or not allowed_markets_raw:
        raise PacketError("portfolio_state", "allowed_markets must be a non-empty list")

    allowed_markets: list[str] = []
    for index, item in enumerate(allowed_markets_raw):
        normalized = normalize_required_string(item, field_name=f"allowed_markets[{index}]")
        try:
            Market(normalized)
        except ValueError as exc:
            raise PacketError("portfolio_state", f"invalid allowed_markets value: {normalized!r}") from exc
        allowed_markets.append(normalized)

    notes = constraints_raw.get("notes")
    if notes is not None:
        notes = normalize_required_string(notes, field_name="constraints.notes")

    constraints = {
        "max_position_weight_percent": str(max_position),
        "max_single_market_weight_percent": str(max_market),
        "min_cash_percent": str(min_cash),
        "max_cash_percent": str(max_cash),
        "allowed_markets": allowed_markets,
    }
    if notes is not None:
        constraints["notes"] = notes

    metadata_raw = raw.get("metadata")
    if not isinstance(metadata_raw, dict):
        raise PacketError("portfolio_state", "metadata must be a JSON object")

    if metadata_raw.get("paper_only") is not True:
        raise PacketError("portfolio_state", "metadata.paper_only must be true")

    try:
        metadata = {
            "source": normalize_required_string(metadata_raw.get("source"), field_name="metadata.source"),
            "created_by": normalize_required_string(
                metadata_raw.get("created_by"),
                field_name="metadata.created_by",
            ),
            "paper_only": True,
        }
    except ValueError as exc:
        raise PacketError("portfolio_state", str(exc)) from exc

    meta_notes = metadata_raw.get("notes")
    if meta_notes is not None:
        metadata["notes"] = normalize_required_string(meta_notes, field_name="metadata.notes")

    return PortfolioStateBundle(
        version=1,
        portfolio_snapshot=portfolio_snapshot,
        nav_snapshot=nav_snapshot,
        constraints=constraints,
        metadata=metadata,
    )


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


def _collect_scout_cited_date_ids(summary: ScoutSummary) -> tuple[str, ...]:
    cited: set[str] = set()
    for group in (summary.positive_factors, summary.negative_factors, summary.neutral_factors):
        for factor in group:
            for reason in factor.reasons:
                cited.add(reason.date_id.value)
    return tuple(sorted(cited))


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


def _load_date_md_ids(date_md_path: Path) -> tuple[str, ...]:
    if not date_md_path.is_file():
        raise PacketError("date_md", f"Date.md not found: {date_md_path}")
    try:
        sections = parse_date_md_sections(date_md_path.read_text(encoding="utf-8"))
    except SmokeError as exc:
        raise PacketError("date_md", exc.message) from exc
    return tuple(section.date_id for section in sections)


def _load_scout_validation_summary(path: Path, *, expected_summary_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PacketError("scout_summary", f"invalid scout validation summary JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise PacketError("scout_summary", "scout validation summary must be a JSON object")
    if payload.get("status") != "ok":
        raise PacketError("scout_summary", "scout validation summary status must be ok")
    if payload.get("summary_id") != expected_summary_id:
        raise PacketError(
            "scout_summary",
            f"scout validation summary summary_id mismatch: expected {expected_summary_id!r}",
        )
    freshness = payload.get("created_at_freshness_checked")
    if freshness is not None and freshness is not False:
        raise PacketError(
            "scout_summary",
            "scout validation summary created_at_freshness_checked must be false or absent",
        )
    return payload


def _extract_example_source_name(allocator_input: dict[str, Any]) -> str | None:
    """패킷 scout_summary에서 skeleton용 source_name 후보를 추출한다. 없으면 null."""
    scout_summary = allocator_input.get("scout_summary")
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


def _build_minimal_allocator_skeleton(
    *,
    universe: str,
    example_date_id: str,
    source_name: str | None,
    created_at_example: str,
    min_cash_percent: str,
    max_cash_percent: str,
) -> str:
    """AllocatorDecision minimal JSON skeleton (shape example only)."""
    reason = lambda text: _reason_object_example(
        reason_text=text,
        date_id=example_date_id,
        source_name=source_name,
    )
    target_weights = dict(SKELETON_TARGET_WEIGHTS)
    skeleton: dict[str, Any] = {
        "decision_id": SKELETON_PLACEHOLDER_DECISION_ID,
        "created_at": created_at_example,
        "schema_name": ALLOCATOR_DECISION_SCHEMA,
        "universe": universe,
        "summary_one_liner": "Replace with a concise summary under 200 characters.",
        "gold_policy_mode": SKELETON_GOLD_POLICY_MODE,
        "signal_summary": {
            "summary": "Replace with signal summary.",
            "reasons": [reason("Replace with evidence-backed signal reason.")],
        },
        "cash_manager": {
            "summary": "Replace with cash-manager summary.",
            "recommended_cash_percent": SKELETON_CASH_PERCENT,
            "reasons": [reason("Replace with evidence-backed cash reason.")],
        },
        "asset_allocator": {
            "summary": "Replace with asset allocation summary.",
            "target_weights": dict(target_weights),
            "reasons": [reason("Replace with evidence-backed asset allocation reason.")],
        },
        "consistency_checker": {
            "passed": True,
            "summary": "Replace with consistency summary.",
            "issues": [],
            "reasons": [reason("Replace with evidence-backed consistency reason.")],
        },
        "cash_policy": {
            "cash_target_percent": SKELETON_CASH_PERCENT,
            "min_cash_percent": min_cash_percent,
            "max_cash_percent": max_cash_percent,
            "rationale": "Replace with cash policy rationale.",
            "reasons": [reason("Replace with evidence-backed cash policy reason.")],
        },
        "target_weights": dict(target_weights),
        "reasons": [reason("Replace with top-level evidence-backed allocation reason.")],
        "metadata": {
            "date_ids": [example_date_id],
            "foundation": "8F",
        },
    }
    return canonical_json_dumps(skeleton)


def _build_allocator_business_rules_section() -> list[str]:
    """Allocator validator business rules hardening 섹션."""
    return [
        "## Allocator validator business rules",
        "",
        PROMPT_TARGET_WEIGHTS_SUM_100,
        PROMPT_GOLD_NORMAL_BAND,
        PROMPT_GOLD_EXCEPTION_BAND,
        PROMPT_GOLD_ZERO_INVALID,
        PROMPT_CASH_TARGET_EQUALS_RECOMMENDED,
        PROMPT_CONTROLLED_KR_SYNTHETIC_SKELETON,
        "",
    ]


def _build_required_allocator_reason_schema_section(*, allowed_date_ids: tuple[str, ...]) -> list[str]:
    """AllocatorReason object schema hardening 섹션."""
    lines = [
        PROMPT_HEADING_REQUIRED_ALLOCATOR_REASON_SCHEMA,
        "",
        PROMPT_REASONS_MUST_BE_OBJECTS,
        PROMPT_NEVER_OUTPUT_REASONS_AS_STRINGS,
        PROMPT_TOP_LEVEL_REASONS_REQUIRED,
        PROMPT_REASON_OBJECT_FIELDS,
        PROMPT_USE_ALLOWED_DATE_IDS_NO_BRACKETS,
        PROMPT_DO_NOT_INVENT_DATE_IDS,
        "",
        PROMPT_DECISION_ID_REQUIRED,
        PROMPT_CREATED_AT_REQUIRED,
        PROMPT_UNIVERSE_REQUIRED,
        PROMPT_SIGNAL_SUMMARY_REQUIRED,
        PROMPT_CASH_MANAGER_REQUIRED,
        PROMPT_ASSET_ALLOCATOR_SUMMARY_REASONS_REQUIRED,
        PROMPT_CONSISTENCY_CHECKER_SUMMARY_REASONS_REQUIRED,
        PROMPT_CASH_POLICY_RATIONALE_REASONS_REQUIRED,
        "",
        "This applies to all reasons locations:",
        "",
        "- top-level `reasons`",
        "- `signal_summary.reasons`",
        "- `cash_manager.reasons`",
        "- `asset_allocator.reasons`",
        "- `consistency_checker.reasons`",
        "- `cash_policy.reasons`",
        "",
        "Required nested fields must not be omitted:",
        "",
        "- `signal_summary.summary`",
        "- `signal_summary.reasons`",
        "- `cash_manager.summary`",
        "- `cash_manager.recommended_cash_percent`",
        "- `cash_manager.reasons`",
        "- `asset_allocator.summary`",
        "- `asset_allocator.target_weights`",
        "- `asset_allocator.reasons`",
        "- `consistency_checker.passed`",
        "- `consistency_checker.summary`",
        "- `consistency_checker.issues`",
        "- `consistency_checker.reasons`",
        "- `cash_policy.cash_target_percent`",
        "- `cash_policy.rationale`",
        "- `cash_policy.reasons`",
        "",
        "Do **not** output:",
        "",
        f"- {PROMPT_INVALID_REASONS_STRING_EXAMPLE}",
        "",
        "Always output reason objects, for example:",
        "",
        '- `"reasons": [{"reason": "...", "date_id": "260529-1", "source_name": "operator-day1", "quote": null}]`',
        "",
        "If evidence is limited, still provide at least one reason object citing an allowed Date-ID "
        "rather than omitting the field.",
        "Do **not** use bracketed Date-IDs like `[260529-1]`.",
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


def _build_minimal_allocator_skeleton_section(
    *,
    allocator_input: dict[str, Any],
    allowed_date_ids: tuple[str, ...],
) -> list[str]:
    """Minimal AllocatorDecision JSON skeleton hardening 섹션."""
    example_date_id = allowed_date_ids[0]
    source_name = _extract_example_source_name(allocator_input)
    created_at_example = str(allocator_input.get("created_at", "2026-05-29T12:00:00+09:00"))
    constraints = allocator_input.get("constraints")
    if not isinstance(constraints, dict):
        constraints = {}
    min_cash = str(constraints.get("min_cash_percent", "10"))
    max_cash = str(constraints.get("max_cash_percent", "30"))
    skeleton_json = _build_minimal_allocator_skeleton(
        universe=str(allocator_input.get("universe", "paper-v0")),
        example_date_id=example_date_id,
        source_name=source_name,
        created_at_example=created_at_example,
        min_cash_percent=min_cash,
        max_cash_percent=max_cash,
    )
    lines = [
        PROMPT_HEADING_MINIMAL_ALLOCATOR_SKELETON,
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


def _build_allocator_prompt(
    *,
    allocator_input: dict[str, Any],
    allowed_date_ids: tuple[str, ...],
    raw_output_path: Path,
) -> str:
    allocator_input_json = canonical_json_dumps(allocator_input)
    lines = [
        "# Allocator Manual Prompt — Paper Pilot Allocation Aid (Foundation 8F)",
        "",
        "> **Paper-pilot allocation aid only.** AllocatorDecision is allocation intent only, not executable trading.",
        "> Do not produce orders, quantities, symbols to buy/sell, KIS calls, or live trading instructions.",
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
        "- If evidence is insufficient, prefer conservative/hold allocation rather than inventing facts.",
        "",
        "## AllocatorDecision schema",
        "",
        f"- schema_name must be {ALLOCATOR_DECISION_SCHEMA!r}",
        f"- summary_one_liner max {SUMMARY_ONE_LINER_MAX_LENGTH} characters",
        "- gold_policy_mode: normal or exception",
        "- target_weights: kr, us, gold must sum to 100",
        "- cash_policy.cash_target_percent must match cash_manager.recommended_cash_percent",
        "- asset_allocator.target_weights must match top-level target_weights",
        "- consistency_checker.passed must be true",
        "- all reasons must use canonical Date-ID strings without brackets",
        "",
        *_build_required_allocator_reason_schema_section(allowed_date_ids=allowed_date_ids),
        f"Allowed Date-IDs: {', '.join(allowed_date_ids) if allowed_date_ids else '(none)'}",
        "",
        *_build_allocator_business_rules_section(),
        *_build_minimal_allocator_skeleton_section(
            allocator_input=allocator_input,
            allowed_date_ids=allowed_date_ids,
        ),
        "## After manual LLM call",
        "",
        f"Save the raw JSON response manually to: `{raw_output_path}`",
        "",
        "Foundation 8F validator will validate raw JSON separately. Do not create validated output here.",
        "",
        "## allocator_input JSON",
        "",
        "```json",
        allocator_input_json,
        "```",
        "",
    ]
    return "\n".join(lines)


def _preflight_out_dir(out_dir: Path, *, force: bool) -> None:
    existing = [name for name in OUTPUT_FILES if (out_dir / name).exists()]
    if existing and not force:
        joined = ", ".join(existing)
        raise PacketError("write", f"output files already exist: {joined} (use --force to overwrite)")


def run_build_allocator_manual_packet(
    *,
    validated_scout_path: Path,
    scout_validation_summary_path: Path | None,
    portfolio_state_path: Path,
    date_md_path: Path,
    store_path: Path,
    universe_path: Path | None,
    out_dir: Path,
    now: datetime,
    force: bool,
) -> dict[str, Any]:
    """Allocator manual packet를 빌드하고 summary dict를 반환한다."""
    scout_summary = _load_scout_summary(validated_scout_path)
    portfolio_state = load_portfolio_state(portfolio_state_path)
    date_md_ids = frozenset(_load_date_md_ids(date_md_path))
    _verify_store_consistency(date_md_path=date_md_path, store_path=store_path)

    if scout_validation_summary_path is not None:
        _load_scout_validation_summary(
            scout_validation_summary_path,
            expected_summary_id=scout_summary.summary_id.value,
        )

    if universe_path is not None:
        try:
            universe = load_universe_toml(universe_path)
        except (FileNotFoundError, ValueError) as exc:
            raise PacketError("args", str(exc)) from exc
        if universe.name != scout_summary.universe:
            raise PacketError(
                "scout_summary",
                f"universe mismatch: ScoutSummary={scout_summary.universe!r}, universe TOML={universe.name!r}",
            )

    cited_date_ids = _collect_scout_cited_date_ids(scout_summary)
    for date_id in cited_date_ids:
        if date_id not in date_md_ids:
            raise PacketError("date_md", f"ScoutSummary cited date_id missing from Date.md: {date_id}")

    allowed_date_ids = tuple(sorted(set(cited_date_ids) & set(date_md_ids)))
    if not allowed_date_ids:
        raise PacketError("scout_summary", "no allowed Date-IDs after ScoutSummary/Date.md intersection")

    allocator_schema_summary = {
        "schema_name": ALLOCATOR_DECISION_SCHEMA,
        "summary_one_liner_max_length": SUMMARY_ONE_LINER_MAX_LENGTH,
        "target_weights_must_sum_to": "100",
        "gold_policy_modes": ["normal", "exception"],
        "consistency_checker_passed_required": True,
    }

    allocator_input: dict[str, Any] = {
        "created_at": now.isoformat(),
        "universe": scout_summary.universe,
        "scout_summary": scout_summary.to_canonical_dict(),
        "portfolio_state": portfolio_state.to_dict(),
        "allowed_date_ids": list(allowed_date_ids),
        "allocator_schema_summary": allocator_schema_summary,
        "constraints": portfolio_state.constraints,
        "metadata": {
            "foundation": "8F",
            "scout_summary_id": scout_summary.summary_id.value,
            "portfolio_snapshot_id": portfolio_state.portfolio_snapshot.snapshot_id,
            "nav_snapshot_id": portfolio_state.nav_snapshot.snapshot_id,
        },
    }

    _preflight_out_dir(out_dir, force=force)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_output_path = out_dir / "allocator_output.raw.json"
    output_paths = {
        "allocator_input": str(out_dir / OUTPUT_ALLOCATOR_INPUT),
        "allocator_prompt": str(out_dir / OUTPUT_ALLOCATOR_PROMPT),
        "allocator_packet_summary": str(out_dir / OUTPUT_PACKET_SUMMARY),
    }

    (out_dir / OUTPUT_ALLOCATOR_INPUT).write_text(
        canonical_json_dumps(allocator_input) + "\n",
        encoding="utf-8",
    )
    (out_dir / OUTPUT_ALLOCATOR_PROMPT).write_text(
        _build_allocator_prompt(
            allocator_input=allocator_input,
            allowed_date_ids=allowed_date_ids,
            raw_output_path=raw_output_path,
        ),
        encoding="utf-8",
    )

    packet_summary: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "universe": scout_summary.universe,
        "scout_summary_id": scout_summary.summary_id.value,
        "portfolio_snapshot_id": portfolio_state.portfolio_snapshot.snapshot_id,
        "nav_snapshot_id": portfolio_state.nav_snapshot.snapshot_id,
        "total_nav_krw": str(portfolio_state.nav_snapshot.total_nav_krw),
        "cash_krw": str(portfolio_state.nav_snapshot.cash_krw),
        "allowed_date_ids": list(allowed_date_ids),
        "allowed_date_ids_count": len(allowed_date_ids),
        "output_paths": output_paths,
        "raw_output_expected_path": str(raw_output_path),
        "created_at": now.isoformat(),
        "created_at_freshness_checked": False,
        "metadata": allocator_input["metadata"],
    }

    (out_dir / OUTPUT_PACKET_SUMMARY).write_text(
        canonical_json_dumps(packet_summary) + "\n",
        encoding="utf-8",
    )

    return {
        "status": "ok",
        "stage": "complete",
        "output_paths": output_paths,
        "scout_summary_id": scout_summary.summary_id.value,
        "allowed_date_ids_count": len(allowed_date_ids),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundation 8F Allocator Once manual LLM call packet builder.",
    )
    parser.add_argument("--validated-scout", required=True, help="validated ScoutSummary JSON path")
    parser.add_argument(
        "--scout-validation-summary",
        default=None,
        help="optional scout_validation_summary.json from Foundation 8E",
    )
    parser.add_argument("--portfolio-state", required=True, help="portfolio state JSON path")
    parser.add_argument("--date-md", required=True, help="exported Date.md path")
    parser.add_argument("--store", required=True, help="SQLiteDateIdSourceStore path")
    parser.add_argument("--universe", default=None, help="optional Universe v0 TOML path")
    parser.add_argument("--out-dir", required=True, help="output directory for allocator packet files")
    parser.add_argument("--now", default=None, help="ISO timezone-aware datetime for allocator_input.created_at")
    parser.add_argument("--force", action="store_true", help="overwrite existing allocator packet output files")
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
    print(f"Allocator manual packet: {status}", file=out)
    for key in ("stage", "output_paths", "scout_summary_id", "allowed_date_ids_count", "error"):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr

    if args.verbose:
        print(f"verbose: validated_scout={args.validated_scout}", file=stderr)
        print(f"verbose: portfolio_state={args.portfolio_state}", file=stderr)
        print(f"verbose: out_dir={args.out_dir}", file=stderr)

    try:
        payload = run_build_allocator_manual_packet(
            validated_scout_path=Path(args.validated_scout),
            scout_validation_summary_path=(
                Path(args.scout_validation_summary) if args.scout_validation_summary else None
            ),
            portfolio_state_path=Path(args.portfolio_state),
            date_md_path=Path(args.date_md),
            store_path=Path(args.store),
            universe_path=Path(args.universe) if args.universe else None,
            out_dir=Path(args.out_dir),
            now=_resolve_now(args.now),
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
