#!/usr/bin/env python3
"""Foundation 8G Analysis raw JSON intake validator.

수동 저장된 raw Analysis LLM JSON을 AnalysisDecision schema +
analysis_input/Date.md membership + AnalysisDecisionValidator 검증 후
canonical validated artifact로 기록한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from analysis.models import ANALYSIS_DECISION_SCHEMA, AnalysisDecision
from analysis.validator import (
    ANALYSIS_VALIDATOR_VERSION,
    AnalysisDecisionValidator,
    extract_date_ids_from_analysis_decision,
)
from data.date_id_store import SQLiteDateIdSourceStore
from data.date_id_validator import DateIdValidator
from decision.canonical_json import canonical_json_dumps
from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime
from domain.identifiers import Percent
from domain.staleness import StalenessPolicy
from run_date_md_smoke import SmokeError, parse_date_md_sections
from validate_allocator_raw_json import ValidationError as AllocatorValidationError
from validate_allocator_raw_json import parse_strict_json_object

StageName = Literal[
    "args",
    "parse",
    "analysis_input",
    "date_md",
    "store",
    "schema",
    "membership",
    "business_rule",
    "write",
    "complete",
]


class ValidationError(Exception):
    """Analysis raw JSON validation 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def validation_output_filenames(market: str, symbol: str) -> tuple[str, str, str]:
    """8G validator 출력 파일명 3종."""
    stem = f"{market.lower()}.{symbol}"
    return (
        f"analysis_output.{stem}.validated.json",
        f"analysis_validation.{stem}.txt",
        f"analysis_validation_summary.{stem}.json",
    )


def _parse_tolerance_pair(
    *,
    allocator_target_weight_percent: Any,
    tolerance_percent: Any,
    source_label: str,
) -> tuple[Percent | None, Percent | None]:
    if allocator_target_weight_percent is None and tolerance_percent is None:
        return None, None
    if (allocator_target_weight_percent is None) != (tolerance_percent is None):
        raise ValidationError(
            "args",
            f"incomplete tolerance context in {source_label}: "
            "allocator_target_weight_percent and tolerance_percent must both be provided or both omitted",
        )
    try:
        return (
            Percent(str(allocator_target_weight_percent)),
            Percent(str(tolerance_percent)),
        )
    except ValueError as exc:
        raise ValidationError("args", str(exc)) from exc


def _resolve_tolerance_context(
    *,
    cli_target: str | None,
    cli_tolerance: str | None,
    analysis_input: dict[str, Any],
) -> tuple[Percent | None, Percent | None]:
    context = analysis_input.get("allocator_tolerance_context")
    input_pair: tuple[Percent | None, Percent | None] = (None, None)
    if context is not None:
        if not isinstance(context, dict):
            raise ValidationError("analysis_input", "allocator_tolerance_context must be a JSON object")
        input_pair = _parse_tolerance_pair(
            allocator_target_weight_percent=context.get("allocator_target_weight_percent"),
            tolerance_percent=context.get("tolerance_percent"),
            source_label="analysis_input.allocator_tolerance_context",
        )

    cli_pair = _parse_tolerance_pair(
        allocator_target_weight_percent=cli_target,
        tolerance_percent=cli_tolerance,
        source_label="CLI",
    )
    if cli_target is not None or cli_tolerance is not None:
        return cli_pair
    return input_pair


def _load_analysis_input(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError("analysis_input", f"analysis_input not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError("analysis_input", f"invalid analysis_input JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("analysis_input", "analysis_input must be a JSON object")

    for field in ("universe", "market", "symbol"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("analysis_input", f"analysis_input.{field} must be a non-blank string")

    allowed_date_ids = payload.get("allowed_date_ids")
    if not isinstance(allowed_date_ids, list) or not allowed_date_ids:
        raise ValidationError("analysis_input", "analysis_input.allowed_date_ids must be a non-empty list")
    for index, item in enumerate(allowed_date_ids):
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(
                "analysis_input",
                f"analysis_input.allowed_date_ids[{index}] must be a non-blank string",
            )

    for field in ("scout_summary", "allocator_decision", "portfolio_state"):
        if not isinstance(payload.get(field), dict):
            raise ValidationError("analysis_input", f"analysis_input.{field} must be a JSON object")

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


def _collect_cited_date_id_values(decision: AnalysisDecision) -> tuple[str, ...]:
    return tuple(sorted({date_id.value for date_id in extract_date_ids_from_analysis_decision(decision)}))


def _validate_membership(
    *,
    decision: AnalysisDecision,
    analysis_input: dict[str, Any],
    date_md_ids: frozenset[str],
) -> tuple[str, ...]:
    if decision.universe != analysis_input["universe"]:
        raise ValidationError(
            "membership",
            f"universe mismatch: AnalysisDecision={decision.universe!r}, "
            f"analysis_input={analysis_input['universe']!r}",
        )
    if decision.market != analysis_input["market"]:
        raise ValidationError(
            "membership",
            f"market mismatch: AnalysisDecision={decision.market!r}, analysis_input={analysis_input['market']!r}",
        )
    if decision.symbol != analysis_input["symbol"]:
        raise ValidationError(
            "membership",
            f"symbol mismatch: AnalysisDecision={decision.symbol!r}, analysis_input={analysis_input['symbol']!r}",
        )

    allowed = frozenset(str(item) for item in analysis_input["allowed_date_ids"])
    cited = _collect_cited_date_id_values(decision)

    for date_id in cited:
        if date_id.startswith("[") or date_id.endswith("]"):
            raise ValidationError("membership", f"cited date_id must not use brackets: {date_id}")
        if date_id not in allowed:
            raise ValidationError(
                "membership",
                f"cited date_id missing from analysis_input.allowed_date_ids: {date_id}",
            )
        if date_id not in date_md_ids:
            raise ValidationError("membership", f"cited date_id missing from Date.md: {date_id}")

    return cited


def _preflight_out_dir(out_dir: Path, *, filenames: tuple[str, str, str], force: bool) -> None:
    existing = [name for name in filenames if (out_dir / name).exists()]
    if existing and not force:
        joined = ", ".join(existing)
        raise ValidationError(
            "write",
            f"output files already exist: {joined} (use --force to overwrite)",
        )


def _build_validation_txt(
    *,
    raw_json_path: Path,
    analysis_input_path: Path,
    date_md_path: Path,
    store_path: Path,
    decision: AnalysisDecision,
    cited_date_ids: tuple[str, ...],
    allocator_target_weight: Percent | None,
    tolerance_percent: Percent | None,
    output_paths: dict[str, str],
) -> str:
    lines = [
        "Analysis raw JSON validation log (Foundation 8G)",
        "",
        "status: ok",
        f"raw_json: {raw_json_path}",
        f"analysis_input: {analysis_input_path}",
        f"date_md: {date_md_path}",
        f"store: {store_path}",
        f"universe: {decision.universe}",
        f"market: {decision.market}",
        f"symbol: {decision.symbol}",
        f"decision_id: {decision.decision_id.value}",
        f"cited_date_ids: {', '.join(cited_date_ids) if cited_date_ids else '(none)'}",
        f"fund_manager.action: {decision.fund_manager.action.value}",
        f"fund_manager.target_weight_percent: {decision.fund_manager.target_weight_percent.value}",
    ]
    if allocator_target_weight is not None:
        lines.append(f"allocator_target_weight_percent: {allocator_target_weight.value}")
    if tolerance_percent is not None:
        lines.append(f"tolerance_percent: {tolerance_percent.value}")
    lines.extend(
        [
            "",
            "checks:",
            "  AnalysisDecision schema: PASS",
            "  analysis_input membership: PASS",
            "  Date.md membership: PASS",
            "  Store consistency: PASS",
            "  AnalysisDecisionValidator: PASS",
            "  created_at freshness ordering: NOT CHECKED",
            "",
            "output files:",
        ]
    )
    for key, value in output_paths.items():
        lines.append(f"  {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def run_validate_analysis_raw_json(
    *,
    raw_json_path: Path,
    analysis_input_path: Path,
    date_md_path: Path,
    out_dir: Path,
    store_path: Path,
    now: datetime,
    cli_allocator_target_weight_percent: str | None,
    cli_tolerance_percent: str | None,
    force: bool,
) -> dict[str, Any]:
    """Analysis raw JSON validation을 실행하고 summary dict를 반환한다."""
    try:
        require_timezone_aware_datetime(now, field_name="now")
    except ValueError as exc:
        raise ValidationError("args", str(exc)) from exc

    if not raw_json_path.is_file():
        raise ValidationError("parse", f"raw JSON not found: {raw_json_path}")

    try:
        raw_object = parse_strict_json_object(raw_json_path.read_text(encoding="utf-8"))
    except AllocatorValidationError as exc:
        raise ValidationError(exc.stage, exc.message) from exc
    analysis_input = _load_analysis_input(analysis_input_path)
    date_md_ids = _load_date_md_ids(date_md_path)

    store = _verify_store_consistency(date_md_path=date_md_path, store_path=store_path)
    try:
        try:
            decision = AnalysisDecision.model_validate(raw_object)
        except ValueError as exc:
            raise ValidationError("schema", str(exc)) from exc

        cited_date_ids = _validate_membership(
            decision=decision,
            analysis_input=analysis_input,
            date_md_ids=date_md_ids,
        )

        allocator_target_weight, tolerance_percent = _resolve_tolerance_context(
            cli_target=cli_allocator_target_weight_percent,
            cli_tolerance=cli_tolerance_percent,
            analysis_input=analysis_input,
        )

        validator = AnalysisDecisionValidator(DateIdValidator(store, StalenessPolicy()))
        result = validator.validate(
            decision,
            now=now,
            allocator_target_weight=allocator_target_weight,
            tolerance_percent=tolerance_percent,
        )
        if not result.passed:
            first_issue = result.issues[0] if result.issues else None
            message = first_issue.message if first_issue is not None else "AnalysisDecisionValidator failed"
            raise ValidationError("business_rule", message)

        market = str(analysis_input["market"])
        symbol = str(analysis_input["symbol"])
        validated_name, txt_name, summary_name = validation_output_filenames(market, symbol)
        _preflight_out_dir(
            out_dir,
            filenames=(validated_name, txt_name, summary_name),
            force=force,
        )
        out_dir.mkdir(parents=True, exist_ok=True)

        output_paths = {
            "validated": str(out_dir / validated_name),
            "validation_txt": str(out_dir / txt_name),
            "validation_summary": str(out_dir / summary_name),
        }

        (out_dir / validated_name).write_text(
            canonical_json_dumps(decision.to_canonical_dict()) + "\n",
            encoding="utf-8",
        )
        (out_dir / txt_name).write_text(
            _build_validation_txt(
                raw_json_path=raw_json_path,
                analysis_input_path=analysis_input_path,
                date_md_path=date_md_path,
                store_path=store_path,
                decision=decision,
                cited_date_ids=cited_date_ids,
                allocator_target_weight=allocator_target_weight,
                tolerance_percent=tolerance_percent,
                output_paths=output_paths,
            ),
            encoding="utf-8",
        )

        validation_summary: dict[str, Any] = {
            "status": "ok",
            "stage": "complete",
            "decision_id": decision.decision_id.value,
            "universe": decision.universe,
            "market": decision.market,
            "symbol": decision.symbol,
            "created_at": decision.created_at.isoformat(),
            "cited_date_ids": list(cited_date_ids),
            "cited_date_ids_count": len(cited_date_ids),
            "action": decision.fund_manager.action.value,
            "target_weight_percent": str(decision.fund_manager.target_weight_percent.value),
            "output_paths": output_paths,
            "raw_json": str(raw_json_path),
            "analysis_input": str(analysis_input_path),
            "date_md": str(date_md_path),
            "store": str(store_path),
            "validator_version": result.validator_version,
            "created_at_freshness_checked": False,
        }
        if allocator_target_weight is not None:
            validation_summary["allocator_target_weight_percent"] = str(allocator_target_weight.value)
        if tolerance_percent is not None:
            validation_summary["tolerance_percent"] = str(tolerance_percent.value)

        (out_dir / summary_name).write_text(
            canonical_json_dumps(validation_summary) + "\n",
            encoding="utf-8",
        )

        return {
            "status": "ok",
            "stage": "complete",
            "output_paths": output_paths,
            "decision_id": decision.decision_id.value,
            "cited_date_ids_count": len(cited_date_ids),
            "market": decision.market,
            "symbol": decision.symbol,
        }
    finally:
        store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundation 8G Analysis raw JSON intake validator.",
    )
    parser.add_argument("--raw-json", required=True, help="manual raw Analysis LLM JSON path")
    parser.add_argument("--analysis-input", required=True, help="analysis_input JSON path from 8G packet")
    parser.add_argument("--date-md", required=True, help="exported Date.md path")
    parser.add_argument("--out-dir", required=True, help="output directory for validated artifacts")
    parser.add_argument("--store", required=True, help="SQLiteDateIdSourceStore path")
    parser.add_argument("--now", default=None, help="ISO timezone-aware datetime for DateIdValidator")
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
    print(f"Analysis raw JSON validation: {status}", file=out)
    for key in (
        "stage",
        "output_paths",
        "decision_id",
        "cited_date_ids_count",
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
        print(f"verbose: raw_json={args.raw_json}", file=stderr)
        print(f"verbose: analysis_input={args.analysis_input}", file=stderr)
        print(f"verbose: out_dir={args.out_dir}", file=stderr)

    try:
        payload = run_validate_analysis_raw_json(
            raw_json_path=Path(args.raw_json),
            analysis_input_path=Path(args.analysis_input),
            date_md_path=Path(args.date_md),
            out_dir=Path(args.out_dir),
            store_path=Path(args.store),
            now=_resolve_now(args.now),
            cli_allocator_target_weight_percent=args.allocator_target_weight_percent,
            cli_tolerance_percent=args.tolerance_percent,
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
