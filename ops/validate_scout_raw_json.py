#!/usr/bin/env python3
"""Foundation 8E Scout raw JSON intake validator.

수동 저장된 raw Scout LLM JSON을 ScoutSummary schema + ScoutInput/Date.md
membership 검증 후 canonical validated artifact로 기록한다.
LLM을 호출하지 않으며 Allocator/Analysis validation은 포함하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

from data.date_id_store import SQLiteDateIdSourceStore
from decision.canonical_json import canonical_json_dumps
from run_date_md_smoke import SmokeError, parse_date_md_sections
from scout.models import ScoutInput, ScoutSummary

StageName = Literal[
    "args",
    "parse",
    "scout_input",
    "date_md",
    "store",
    "schema",
    "membership",
    "write",
    "complete",
]

OUTPUT_VALIDATED = "scout_output.validated.json"
OUTPUT_VALIDATION_TXT = "scout_validation.txt"
OUTPUT_VALIDATION_SUMMARY = "scout_validation_summary.json"
OUTPUT_FILES = (OUTPUT_VALIDATED, OUTPUT_VALIDATION_TXT, OUTPUT_VALIDATION_SUMMARY)


class ValidationError(Exception):
    """Scout raw JSON validation 실패. stage와 sanitized message를 담는다."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundation 8E Scout raw JSON intake validator.",
    )
    parser.add_argument("--raw-json", required=True, help="manual raw Scout LLM JSON path")
    parser.add_argument("--scout-input", required=True, help="ScoutInput JSON path from 8D packet")
    parser.add_argument("--date-md", required=True, help="exported Date.md path (Foundation 8B)")
    parser.add_argument("--out-dir", required=True, help="output directory for validated artifacts")
    parser.add_argument(
        "--store",
        default=None,
        help="optional SQLiteDateIdSourceStore path for Date.md/store consistency",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing validation output files",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON summary to stdout")
    parser.add_argument("--verbose", action="store_true", help="print non-sensitive metadata to stderr")
    return parser


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


def _load_scout_input(path: Path) -> ScoutInput:
    if not path.is_file():
        raise ValidationError("scout_input", f"scout_input not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError("scout_input", f"invalid scout_input JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("scout_input", "scout_input must be a JSON object")
    try:
        return ScoutInput.model_validate(payload)
    except ValueError as exc:
        raise ValidationError("scout_input", str(exc)) from exc


def _load_date_md_sections(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        raise ValidationError("date_md", f"Date.md not found: {path}")
    try:
        sections = parse_date_md_sections(path.read_text(encoding="utf-8"))
    except SmokeError as exc:
        raise ValidationError("date_md", exc.message) from exc
    return tuple(section.date_id for section in sections)


def _verify_store_consistency(*, date_md_path: Path, store_path: Path) -> None:
    if not store_path.is_file():
        raise ValidationError("store", f"store not found: {store_path}")

    date_md_ids = set(_load_date_md_sections(date_md_path))
    store = SQLiteDateIdSourceStore(store_path)
    try:
        store_records = store.list_records()
    finally:
        store.close()

    store_ids = {record.date_id.value for record in store_records}
    missing = sorted(date_md_ids - store_ids)
    if missing:
        raise ValidationError(
            "store",
            f"Date.md date_id missing from store: {', '.join(missing)}",
        )


def _collect_cited_date_ids(summary: ScoutSummary) -> tuple[str, ...]:
    cited: set[str] = set()
    for group in (
        summary.positive_factors,
        summary.negative_factors,
        summary.neutral_factors,
    ):
        for factor in group:
            for reason in factor.reasons:
                cited.add(reason.date_id.value)
    return tuple(sorted(cited))


def _validate_membership(
    *,
    summary: ScoutSummary,
    scout_input: ScoutInput,
    date_md_date_ids: frozenset[str],
) -> tuple[str, ...]:
    if summary.universe != scout_input.universe:
        raise ValidationError(
            "membership",
            f"universe mismatch: ScoutSummary={summary.universe!r}, ScoutInput={scout_input.universe!r}",
        )

    scout_input_ids = {record.date_id.value for record in scout_input.records}
    cited = _collect_cited_date_ids(summary)

    for date_id in cited:
        if date_id not in scout_input_ids:
            raise ValidationError(
                "membership",
                f"cited date_id missing from ScoutInput.records: {date_id}",
            )
        if date_id not in date_md_date_ids:
            raise ValidationError(
                "membership",
                f"cited date_id missing from Date.md: {date_id}",
            )

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
    scout_input_path: Path,
    date_md_path: Path,
    store_path: Path | None,
    summary: ScoutSummary,
    cited_date_ids: tuple[str, ...],
    scout_input: ScoutInput,
    date_md_date_ids: frozenset[str],
    store_checked: bool,
    output_paths: dict[str, str],
) -> str:
    lines = [
        "Scout raw JSON validation log (Foundation 8E)",
        "",
        f"status: ok",
        f"raw_json: {raw_json_path}",
        f"scout_input: {scout_input_path}",
        f"date_md: {date_md_path}",
        f"store: {store_path if store_path is not None else '(not provided)'}",
        f"universe: {summary.universe}",
        f"summary_id: {summary.summary_id.value}",
        f"created_at: {summary.created_at.isoformat()}",
        f"cited_date_ids: {', '.join(cited_date_ids) if cited_date_ids else '(none)'}",
        f"allowed_scout_input_date_ids_count: {len(scout_input.records)}",
        f"allowed_date_md_date_ids_count: {len(date_md_date_ids)}",
        "",
        "checks:",
        "  ScoutSummary schema: PASS",
        "  ScoutInput membership: PASS",
        "  Date.md membership: PASS",
        f"  Store consistency: {'PASS' if store_checked else 'SKIPPED'}",
        "  created_at freshness ordering: NOT CHECKED",
        "",
        "output files:",
    ]
    for key, value in output_paths.items():
        lines.append(f"  {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def run_validate_scout_raw_json(
    *,
    raw_json_path: Path,
    scout_input_path: Path,
    date_md_path: Path,
    out_dir: Path,
    store_path: Path | None,
    force: bool,
) -> dict[str, Any]:
    """Scout raw JSON validation을 실행하고 summary dict를 반환한다."""
    if not raw_json_path.is_file():
        raise ValidationError("parse", f"raw JSON not found: {raw_json_path}")

    raw_text = raw_json_path.read_text(encoding="utf-8")
    raw_object = parse_strict_json_object(raw_text)

    scout_input = _load_scout_input(scout_input_path)
    date_md_ids = frozenset(_load_date_md_sections(date_md_path))

    store_checked = False
    if store_path is not None:
        _verify_store_consistency(date_md_path=date_md_path, store_path=store_path)
        store_checked = True

    try:
        summary = ScoutSummary.model_validate(raw_object)
    except ValueError as exc:
        raise ValidationError("schema", str(exc)) from exc

    cited_date_ids = _validate_membership(
        summary=summary,
        scout_input=scout_input,
        date_md_date_ids=date_md_ids,
    )

    _preflight_out_dir(out_dir, force=force)
    out_dir.mkdir(parents=True, exist_ok=True)

    output_paths = {
        "validated": str(out_dir / OUTPUT_VALIDATED),
        "validation_txt": str(out_dir / OUTPUT_VALIDATION_TXT),
        "validation_summary": str(out_dir / OUTPUT_VALIDATION_SUMMARY),
    }

    validated_path = out_dir / OUTPUT_VALIDATED
    validation_txt_path = out_dir / OUTPUT_VALIDATION_TXT
    summary_json_path = out_dir / OUTPUT_VALIDATION_SUMMARY

    validated_path.write_text(
        canonical_json_dumps(summary.to_canonical_dict()) + "\n",
        encoding="utf-8",
    )
    validation_txt_path.write_text(
        _build_validation_txt(
            raw_json_path=raw_json_path,
            scout_input_path=scout_input_path,
            date_md_path=date_md_path,
            store_path=store_path,
            summary=summary,
            cited_date_ids=cited_date_ids,
            scout_input=scout_input,
            date_md_date_ids=date_md_ids,
            store_checked=store_checked,
            output_paths=output_paths,
        ),
        encoding="utf-8",
    )

    validation_summary: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "summary_id": summary.summary_id.value,
        "universe": summary.universe,
        "created_at": summary.created_at.isoformat(),
        "records_count": len(scout_input.records),
        "scout_input_date_ids_count": len(scout_input.records),
        "date_md_date_ids_count": len(date_md_ids),
        "cited_date_ids": list(cited_date_ids),
        "cited_date_ids_count": len(cited_date_ids),
        "factor_counts": {
            "positive": len(summary.positive_factors),
            "negative": len(summary.negative_factors),
            "neutral": len(summary.neutral_factors),
        },
        "output_paths": output_paths,
        "raw_json": str(raw_json_path),
        "scout_input": str(scout_input_path),
        "date_md": str(date_md_path),
        "created_at_freshness_checked": False,
    }
    if store_path is not None:
        validation_summary["store"] = str(store_path)

    summary_json_path.write_text(
        canonical_json_dumps(validation_summary) + "\n",
        encoding="utf-8",
    )

    return {
        "status": "ok",
        "stage": "complete",
        "output_paths": output_paths,
        "summary_id": summary.summary_id.value,
        "cited_date_ids_count": len(cited_date_ids),
    }


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return

    status = payload.get("status", "error")
    print(f"Scout raw JSON validation: {status}", file=out)
    for key in ("stage", "output_paths", "summary_id", "cited_date_ids_count", "error"):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr

    if args.verbose:
        print(f"verbose: raw_json={args.raw_json}", file=stderr)
        print(f"verbose: scout_input={args.scout_input}", file=stderr)
        print(f"verbose: date_md={args.date_md}", file=stderr)
        print(f"verbose: out_dir={args.out_dir}", file=stderr)
        if args.store:
            print(f"verbose: store={args.store}", file=stderr)

    try:
        payload = run_validate_scout_raw_json(
            raw_json_path=Path(args.raw_json),
            scout_input_path=Path(args.scout_input),
            date_md_path=Path(args.date_md),
            out_dir=Path(args.out_dir),
            store_path=Path(args.store) if args.store else None,
            force=args.force,
        )
    except ValidationError as exc:
        payload = {
            "status": "error",
            "stage": exc.stage,
            "error": exc.message,
        }
        _emit_result(payload, as_json=as_json, out=stdout)
        return 1

    _emit_result(payload, as_json=as_json, out=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
