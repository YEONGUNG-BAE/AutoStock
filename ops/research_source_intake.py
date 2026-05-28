#!/usr/bin/env python3
"""Manual/file-based research source intake and Date.md export.

Layer A infrastructure only. Not an external API collector, LLM runner, or trading path.
Does not call external APIs, LLM/Ollama, KIS, or broker execution paths.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

from pydantic import ValidationError

from data.date_id_store import DuplicateDateIdError, SQLiteDateIdSourceStore
from decision.canonical_json import payload_sha256
from domain._datetime import parse_timezone_aware_datetime
from domain.source import DateIdSourceRecord

ModeName = Literal["validate-only", "normal", "export-only"]


class IntakeError(Exception):
    """research source intake 실패. stage와 sanitized message를 담는다."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual research source JSONL intake + Date.md export (Layer A only).",
    )
    parser.add_argument(
        "--source-jsonl",
        default=None,
        help="operator-prepared DateIdSourceRecord JSONL input path",
    )
    parser.add_argument(
        "--store",
        default=None,
        help="SQLiteDateIdSourceStore SQLite path",
    )
    parser.add_argument(
        "--date-md-out",
        default=None,
        help="Date.md export output path (read-only prompt reference)",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate JSONL only; do not write store or Date.md",
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="export Date.md from existing store; do not read JSONL",
    )
    parser.add_argument(
        "--force-date-md",
        action="store_true",
        help="overwrite existing Date.md output if present",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON summary",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print non-sensitive metadata only",
    )
    return parser


def _resolve_mode(args: argparse.Namespace) -> ModeName:
    if args.validate_only:
        return "validate-only"
    if args.export_only:
        return "export-only"
    return "normal"


def _validate_mode_flags(args: argparse.Namespace) -> None:
    if args.validate_only and args.export_only:
        raise IntakeError(
            "args",
            "--validate-only and --export-only are mutually exclusive",
        )


def _require_path(value: str | None, *, flag: str) -> Path:
    if not value:
        raise IntakeError("args", f"{flag} is required for this mode")
    return Path(value)


def _coerce_record_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """JSONL object의 ISO datetime string을 DateIdSourceRecord validation용으로 변환한다."""
    coerced = dict(raw)
    for field in ("source_timestamp", "created_at"):
        if field in coerced:
            coerced[field] = parse_timezone_aware_datetime(coerced[field], field_name=field)
    return coerced


def parse_jsonl_records(path: Path) -> tuple[tuple[DateIdSourceRecord, ...], int]:
    """JSONL 파일을 읽어 DateIdSourceRecord tuple과 non-empty line count를 반환한다."""
    if not path.is_file():
        raise IntakeError("parse", f"source JSONL not found: {path}")

    records: list[DateIdSourceRecord] = []
    seen_date_ids: set[str] = set()
    lines_read = 0

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        lines_read += 1

        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise IntakeError("parse", f"line {line_no}: invalid JSON: {exc.msg}") from exc

        if not isinstance(raw, dict):
            raise IntakeError("parse", f"line {line_no}: JSON value must be an object")

        try:
            record = DateIdSourceRecord.model_validate(_coerce_record_payload(raw))
        except ValidationError as exc:
            detail = _summarize_validation_error(exc)
            raise IntakeError("validate", f"line {line_no}: {detail}") from exc
        except ValueError as exc:
            raise IntakeError("validate", f"line {line_no}: {exc}") from exc

        date_id = record.date_id.value
        if date_id in seen_date_ids:
            raise IntakeError(
                "validate",
                f"line {line_no}: duplicate date_id in input: {date_id}",
            )
        seen_date_ids.add(date_id)
        records.append(record)

    return tuple(records), lines_read


def _summarize_validation_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "validation failed"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    msg = first.get("msg", "invalid value")
    return f"{loc}: {msg}" if loc else str(msg)


def _preflight_date_md_out(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise IntakeError(
            "export",
            f"Date.md output already exists: {path} (use --force-date-md to overwrite)",
        )


def render_date_md(records: tuple[DateIdSourceRecord, ...]) -> str:
    """store record 목록으로 deterministic Date.md markdown을 생성한다."""
    lines = [
        "# Date.md — Read-only Prompt Reference",
        "",
        "> SQLiteDateIdSourceStore is the canonical store.",
        "> This file is generated for LLM prompts only. Do not treat it as canonical storage.",
        "",
    ]

    for record in records:
        lines.extend(
            [
                f"## [{record.date_id.value}]",
                "",
                f"- **fact_type:** {record.fact_type.value}",
                f"- **source_name:** {record.source_name}",
                f"- **source_timestamp:** {record.source_timestamp.isoformat()}",
                f"- **summary:** {record.summary}",
            ]
        )
        if record.symbol is not None:
            lines.append(f"- **symbol:** {record.symbol}")
        if record.market is not None:
            lines.append(f"- **market:** {record.market}")
        if record.source_url is not None:
            lines.append(f"- **source_url:** {record.source_url}")
        lines.append(f"- **payload_hash:** {payload_sha256(record.payload)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def export_date_md(records: tuple[DateIdSourceRecord, ...], output_path: Path) -> None:
    """Date.md를 deterministic markdown으로 export한다. raw payload dump 금지."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_date_md(records), encoding="utf-8")


def _save_records_to_store(store_path: Path, records: tuple[DateIdSourceRecord, ...]) -> int:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteDateIdSourceStore(store_path)
    try:
        with store.transaction():
            for record in records:
                store.save_record(record)
    except DuplicateDateIdError as exc:
        raise IntakeError("store", str(exc)) from exc
    finally:
        store.close()
    return len(records)


def _load_records_from_store(store_path: Path) -> tuple[DateIdSourceRecord, ...]:
    if not store_path.is_file():
        raise IntakeError("export", f"store not found: {store_path}")
    store = SQLiteDateIdSourceStore(store_path)
    try:
        return store.list_records()
    finally:
        store.close()


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return

    status = payload.get("status", "error")
    print(f"Research source intake: {status}", file=out)
    for key in (
        "mode",
        "stage",
        "records_read",
        "records_valid",
        "records_saved",
        "store",
        "date_md_out",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def _success_payload(
    *,
    mode: ModeName,
    stage: str = "complete",
    records_read: int = 0,
    records_valid: int = 0,
    records_saved: int | None = None,
    store: Path | None = None,
    date_md_out: Path | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "mode": mode,
        "stage": stage,
        "records_read": records_read,
        "records_valid": records_valid,
    }
    if records_saved is not None:
        payload["records_saved"] = records_saved
    if store is not None:
        payload["store"] = str(store)
    if date_md_out is not None:
        payload["date_md_out"] = str(date_md_out)
    return payload


def _error_payload(*, mode: ModeName, stage: str, error: str) -> dict[str, Any]:
    return {
        "status": "error",
        "mode": mode,
        "stage": stage,
        "error": error,
    }


def run_validate_only(source_jsonl: Path) -> dict[str, Any]:
    records, lines_read = parse_jsonl_records(source_jsonl)
    return _success_payload(
        mode="validate-only",
        records_read=lines_read,
        records_valid=len(records),
    )


def run_normal(
    *,
    source_jsonl: Path,
    store_path: Path,
    date_md_out: Path,
    force_date_md: bool,
) -> dict[str, Any]:
    records, lines_read = parse_jsonl_records(source_jsonl)
    _preflight_date_md_out(date_md_out, force=force_date_md)
    records_saved = _save_records_to_store(store_path, records)
    saved_records = _load_records_from_store(store_path)
    export_date_md(saved_records, date_md_out)
    return _success_payload(
        mode="normal",
        records_read=lines_read,
        records_valid=len(records),
        records_saved=records_saved,
        store=store_path,
        date_md_out=date_md_out,
    )


def run_export_only(*, store_path: Path, date_md_out: Path, force_date_md: bool) -> dict[str, Any]:
    _preflight_date_md_out(date_md_out, force=force_date_md)
    records = _load_records_from_store(store_path)
    export_date_md(records, date_md_out)
    return _success_payload(
        mode="export-only",
        records_read=0,
        records_valid=len(records),
        store=store_path,
        date_md_out=date_md_out,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout
    mode: ModeName = "normal"

    if args.verbose:
        print(f"verbose: validate_only={'yes' if args.validate_only else 'no'}", file=sys.stderr)
        print(f"verbose: export_only={'yes' if args.export_only else 'no'}", file=sys.stderr)

    try:
        _validate_mode_flags(args)
        mode = _resolve_mode(args)

        if mode == "validate-only":
            source_jsonl = _require_path(args.source_jsonl, flag="--source-jsonl")
            payload = run_validate_only(source_jsonl)
        elif mode == "export-only":
            store_path = _require_path(args.store, flag="--store")
            date_md_out = _require_path(args.date_md_out, flag="--date-md-out")
            payload = run_export_only(
                store_path=store_path,
                date_md_out=date_md_out,
                force_date_md=args.force_date_md,
            )
        else:
            source_jsonl = _require_path(args.source_jsonl, flag="--source-jsonl")
            store_path = _require_path(args.store, flag="--store")
            date_md_out = _require_path(args.date_md_out, flag="--date-md-out")
            payload = run_normal(
                source_jsonl=source_jsonl,
                store_path=store_path,
                date_md_out=date_md_out,
                force_date_md=args.force_date_md,
            )
    except IntakeError as exc:
        payload = _error_payload(mode=mode, stage=exc.stage, error=exc.message)
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    _emit_result(payload, as_json=as_json, out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
