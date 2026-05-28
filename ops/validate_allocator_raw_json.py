#!/usr/bin/env python3
"""Foundation 8F Allocator raw JSON intake validator.

수동 저장된 raw Allocator LLM JSON을 AllocatorDecision schema +
allocator_input/Date.md membership + AllocatorDecisionValidator 검증 후
canonical validated artifact로 기록한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from allocator.models import ALLOCATOR_DECISION_SCHEMA, AllocatorDecision
from allocator.validator import (
    ALLOCATOR_VALIDATOR_VERSION,
    AllocatorDecisionValidator,
    extract_date_ids_from_allocator_decision,
)
from data.date_id_store import SQLiteDateIdSourceStore
from data.date_id_validator import DateIdValidator
from decision.canonical_json import canonical_json_dumps
from domain._datetime import parse_timezone_aware_datetime
from domain.staleness import StalenessPolicy
from run_date_md_smoke import SmokeError, parse_date_md_sections

StageName = Literal[
    "args",
    "parse",
    "allocator_input",
    "date_md",
    "store",
    "schema",
    "membership",
    "business_rule",
    "write",
    "complete",
]

OUTPUT_VALIDATED = "allocator_output.validated.json"
OUTPUT_VALIDATION_TXT = "allocator_validation.txt"
OUTPUT_VALIDATION_SUMMARY = "allocator_validation_summary.json"
OUTPUT_FILES = (OUTPUT_VALIDATED, OUTPUT_VALIDATION_TXT, OUTPUT_VALIDATION_SUMMARY)


class ValidationError(Exception):
    """Allocator raw JSON validation 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def parse_strict_json_object(text: str) -> dict[str, Any]:
    """raw 파일 전체가 단일 JSON object인지 strict하게 검증한다."""
    if not text.strip():
        raise ValidationError("parse", "raw JSON file is empty")
    if "```" in text:
        raise ValidationError("parse", "raw JSON must not contain markdown fences")
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        raise ValidationError("parse", "raw JSON must be a single JSON object without prose")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValidationError("parse", f"invalid JSON: {exc.msg}") from exc
    if isinstance(parsed, bool) or parsed is None or isinstance(parsed, (str, int, float, list)):
        raise ValidationError("parse", "raw JSON must be a single JSON object")
    if not isinstance(parsed, dict):
        raise ValidationError("parse", "raw JSON must be a single JSON object")
    return parsed


def _load_allocator_input(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError("allocator_input", f"allocator_input not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError("allocator_input", f"invalid allocator_input JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("allocator_input", "allocator_input must be a JSON object")

    universe = payload.get("universe")
    if not isinstance(universe, str) or not universe.strip():
        raise ValidationError("allocator_input", "allocator_input.universe must be a non-blank string")

    allowed_date_ids = payload.get("allowed_date_ids")
    if not isinstance(allowed_date_ids, list) or not allowed_date_ids:
        raise ValidationError("allocator_input", "allocator_input.allowed_date_ids must be a non-empty list")
    for index, item in enumerate(allowed_date_ids):
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(
                "allocator_input",
                f"allocator_input.allowed_date_ids[{index}] must be a non-blank string",
            )

    if not isinstance(payload.get("scout_summary"), dict):
        raise ValidationError("allocator_input", "allocator_input.scout_summary must be a JSON object")
    if not isinstance(payload.get("portfolio_state"), dict):
        raise ValidationError("allocator_input", "allocator_input.portfolio_state must be a JSON object")

    return payload


def _load_date_md_ids(date_md_path: Path) -> frozenset[str]:
    if not date_md_path.is_file():
        raise ValidationError("date_md", f"Date.md not found: {date_md_path}")
    try:
        sections = parse_date_md_sections(date_md_path.read_text(encoding="utf-8"))
    except SmokeError as exc:
        raise ValidationError("date_md", exc.message) from exc
    return frozenset(section.date_id for section in sections)


def _verify_store_consistency(*, date_md_path: Path, store_path: Path) -> SQLiteDateIdSourceStore:
    if not store_path.is_file():
        raise ValidationError("store", f"store not found: {store_path}")
    date_md_ids = _load_date_md_ids(date_md_path)
    store = SQLiteDateIdSourceStore(store_path)
    store_records = store.list_records()
    store_ids = {record.date_id.value for record in store_records}
    missing = sorted(date_md_ids - store_ids)
    if missing:
        store.close()
        raise ValidationError("store", f"Date.md date_id missing from store: {', '.join(missing)}")
    return store


def _collect_cited_date_id_values(decision: AllocatorDecision) -> tuple[str, ...]:
    return tuple(sorted({date_id.value for date_id in extract_date_ids_from_allocator_decision(decision)}))


def _validate_membership(
    *,
    decision: AllocatorDecision,
    allocator_input: dict[str, Any],
    date_md_ids: frozenset[str],
) -> tuple[str, ...]:
    if decision.universe != allocator_input["universe"]:
        raise ValidationError(
            "membership",
            f"universe mismatch: AllocatorDecision={decision.universe!r}, allocator_input={allocator_input['universe']!r}",
        )

    allowed = frozenset(str(item) for item in allocator_input["allowed_date_ids"])
    cited = _collect_cited_date_id_values(decision)

    for date_id in cited:
        if date_id.startswith("[") or date_id.endswith("]"):
            raise ValidationError("membership", f"cited date_id must not use brackets: {date_id}")
        if date_id not in allowed:
            raise ValidationError(
                "membership",
                f"cited date_id missing from allocator_input.allowed_date_ids: {date_id}",
            )
        if date_id not in date_md_ids:
            raise ValidationError("membership", f"cited date_id missing from Date.md: {date_id}")

    return cited


def _preflight_out_dir(out_dir: Path, *, force: bool) -> None:
    existing = [name for name in OUTPUT_FILES if (out_dir / name).exists()]
    if existing and not force:
        joined = ", ".join(existing)
        raise ValidationError(
            "write",
            f"output files already exist: {joined} (use --force to overwrite)",
        )


def _build_validation_txt(
    *,
    raw_json_path: Path,
    allocator_input_path: Path,
    date_md_path: Path,
    store_path: Path,
    decision: AllocatorDecision,
    cited_date_ids: tuple[str, ...],
    output_paths: dict[str, str],
) -> str:
    lines = [
        "Allocator raw JSON validation log (Foundation 8F)",
        "",
        "status: ok",
        f"raw_json: {raw_json_path}",
        f"allocator_input: {allocator_input_path}",
        f"date_md: {date_md_path}",
        f"store: {store_path}",
        f"universe: {decision.universe}",
        f"decision_id: {decision.decision_id.value}",
        f"cited_date_ids: {', '.join(cited_date_ids) if cited_date_ids else '(none)'}",
        f"target_weights: kr={decision.target_weights.kr.value}, us={decision.target_weights.us.value}, gold={decision.target_weights.gold.value}",
        f"cash_target_percent: {decision.cash_policy.cash_target_percent.value}",
        "",
        "checks:",
        "  AllocatorDecision schema: PASS",
        "  allocator_input membership: PASS",
        "  Date.md membership: PASS",
        "  Store consistency: PASS",
        "  AllocatorDecisionValidator: PASS",
        "  created_at freshness ordering: NOT CHECKED",
        "",
        "output files:",
    ]
    for key, value in output_paths.items():
        lines.append(f"  {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def run_validate_allocator_raw_json(
    *,
    raw_json_path: Path,
    allocator_input_path: Path,
    date_md_path: Path,
    out_dir: Path,
    store_path: Path,
    now: datetime,
    force: bool,
) -> dict[str, Any]:
    """Allocator raw JSON validation을 실행하고 summary dict를 반환한다."""
    if not raw_json_path.is_file():
        raise ValidationError("parse", f"raw JSON not found: {raw_json_path}")

    raw_object = parse_strict_json_object(raw_json_path.read_text(encoding="utf-8"))
    allocator_input = _load_allocator_input(allocator_input_path)
    date_md_ids = _load_date_md_ids(date_md_path)

    store = _verify_store_consistency(date_md_path=date_md_path, store_path=store_path)
    try:
        try:
            decision = AllocatorDecision.model_validate(raw_object)
        except ValueError as exc:
            raise ValidationError("schema", str(exc)) from exc

        cited_date_ids = _validate_membership(
            decision=decision,
            allocator_input=allocator_input,
            date_md_ids=date_md_ids,
        )

        validator = AllocatorDecisionValidator(DateIdValidator(store, StalenessPolicy()))
        result = validator.validate(decision, now=now)
        if not result.passed:
            first_issue = result.issues[0] if result.issues else None
            message = first_issue.message if first_issue is not None else "AllocatorDecisionValidator failed"
            raise ValidationError("business_rule", message)

        _preflight_out_dir(out_dir, force=force)
        out_dir.mkdir(parents=True, exist_ok=True)

        output_paths = {
            "validated": str(out_dir / OUTPUT_VALIDATED),
            "validation_txt": str(out_dir / OUTPUT_VALIDATION_TXT),
            "validation_summary": str(out_dir / OUTPUT_VALIDATION_SUMMARY),
        }

        (out_dir / OUTPUT_VALIDATED).write_text(
            canonical_json_dumps(decision.to_canonical_dict()) + "\n",
            encoding="utf-8",
        )
        (out_dir / OUTPUT_VALIDATION_TXT).write_text(
            _build_validation_txt(
                raw_json_path=raw_json_path,
                allocator_input_path=allocator_input_path,
                date_md_path=date_md_path,
                store_path=store_path,
                decision=decision,
                cited_date_ids=cited_date_ids,
                output_paths=output_paths,
            ),
            encoding="utf-8",
        )

        validation_summary: dict[str, Any] = {
            "status": "ok",
            "stage": "complete",
            "decision_id": decision.decision_id.value,
            "universe": decision.universe,
            "created_at": decision.created_at.isoformat(),
            "cited_date_ids": list(cited_date_ids),
            "cited_date_ids_count": len(cited_date_ids),
            "target_weights": {
                "kr": str(decision.target_weights.kr.value),
                "us": str(decision.target_weights.us.value),
                "gold": str(decision.target_weights.gold.value),
            },
            "cash_target_percent": str(decision.cash_policy.cash_target_percent.value),
            "gold_policy_mode": decision.gold_policy_mode.value,
            "output_paths": output_paths,
            "raw_json": str(raw_json_path),
            "allocator_input": str(allocator_input_path),
            "date_md": str(date_md_path),
            "store": str(store_path),
            "validator_version": result.validator_version,
            "created_at_freshness_checked": False,
        }

        (out_dir / OUTPUT_VALIDATION_SUMMARY).write_text(
            canonical_json_dumps(validation_summary) + "\n",
            encoding="utf-8",
        )

        return {
            "status": "ok",
            "stage": "complete",
            "output_paths": output_paths,
            "decision_id": decision.decision_id.value,
            "cited_date_ids_count": len(cited_date_ids),
        }
    finally:
        store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundation 8F Allocator raw JSON intake validator.",
    )
    parser.add_argument("--raw-json", required=True, help="manual raw Allocator LLM JSON path")
    parser.add_argument("--allocator-input", required=True, help="allocator_input JSON path from 8F packet")
    parser.add_argument("--date-md", required=True, help="exported Date.md path")
    parser.add_argument("--out-dir", required=True, help="output directory for validated artifacts")
    parser.add_argument("--store", required=True, help="SQLiteDateIdSourceStore path (required for AllocatorDecisionValidator)")
    parser.add_argument("--now", default=None, help="ISO timezone-aware datetime for DateIdValidator")
    parser.add_argument("--force", action="store_true", help="overwrite existing validation output files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON summary to stdout")
    parser.add_argument("--verbose", action="store_true", help="print non-sensitive metadata to stderr")
    return parser


def _resolve_now(raw_now: str | None) -> datetime:
    if raw_now is None:
        return datetime.now(tz=UTC)
    try:
        return parse_timezone_aware_datetime(raw_now, field_name="now")
    except ValueError as exc:
        raise ValidationError("args", str(exc)) from exc


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Allocator raw JSON validation: {status}", file=out)
    for key in ("stage", "output_paths", "decision_id", "cited_date_ids_count", "error"):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr

    if args.verbose:
        print(f"verbose: raw_json={args.raw_json}", file=stderr)
        print(f"verbose: allocator_input={args.allocator_input}", file=stderr)
        print(f"verbose: out_dir={args.out_dir}", file=stderr)

    try:
        payload = run_validate_allocator_raw_json(
            raw_json_path=Path(args.raw_json),
            allocator_input_path=Path(args.allocator_input),
            date_md_path=Path(args.date_md),
            out_dir=Path(args.out_dir),
            store_path=Path(args.store),
            now=_resolve_now(args.now),
            force=args.force,
        )
    except ValidationError as exc:
        payload = {"status": "error", "stage": exc.stage, "error": exc.message}
        _emit_result(payload, as_json=args.json, out=stdout)
        return 1

    _emit_result(payload, as_json=args.json, out=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
