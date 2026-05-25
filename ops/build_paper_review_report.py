#!/usr/bin/env python3
"""PaperReviewInput JSON으로 Phase 16 paper review report를 수동 생성한다.

Collector가 아니다. LLM/Ollama/KIS/PaperBroker/ledger store 호출 없음.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError

from paper_review import (
    PaperReviewInput,
    PaperReviewReport,
    PaperReviewReportStore,
    build_paper_review_report,
    render_paper_review_markdown,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build Phase 16 paper review report from PaperReviewInput JSON.",
    )
    parser.add_argument(
        "--review-input",
        required=True,
        help="PaperReviewInput-compatible JSON file path",
    )
    parser.add_argument(
        "--store",
        default=None,
        help="optional PaperReviewReportStore JSONL path (append-only save)",
    )
    parser.add_argument(
        "--markdown-out",
        default=None,
        help="optional rendered markdown report output path",
    )
    parser.add_argument(
        "--stdout-markdown",
        action="store_true",
        help="write rendered markdown to stdout (summary goes to stderr)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON summary to stdout",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print non-sensitive metadata (no raw input payload)",
    )
    return parser


def _fail(stage: str, reason: str, *, as_json: bool = False, out: TextIO = sys.stdout) -> int:
    payload = {
        "outcome": "FAIL",
        "stage": stage,
        "reason": reason,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
    else:
        print("Paper review: FAIL", file=out)
        print(f"stage: {stage}", file=out)
        print(f"reason: {reason}", file=out)
    return 1


def _input_fingerprint(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return f"len={path.stat().st_size} sha256={digest}"


def _summarize_validation_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Pydantic validation failed"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    msg = first.get("msg", "invalid value")
    if len(errors) == 1:
        return f"{loc}: {msg}" if loc else str(msg)
    return f"{len(errors)} validation errors (first: {loc}: {msg})"


def _build_summary(
    *,
    review_input_path: Path,
    report: PaperReviewReport,
    store_saved: bool,
    markdown_written: bool,
) -> dict[str, Any]:
    period = report.period
    performance = report.performance_metrics
    return {
        "outcome": "PASS",
        "review_input": str(review_input_path),
        "review_id": report.review_id,
        "period": f"{period.start_date.isoformat()} ~ {period.end_date.isoformat()}",
        "sample_sufficiency": report.sample_sufficiency.value,
        "nav_snapshot_count": performance.nav_snapshot_count,
        "total_return_percent": str(performance.total_return_percent),
        "max_drawdown_percent": str(performance.max_drawdown_percent),
        "recommendation_count": len(report.recommendations),
        "data_quality_warning_count": len(report.data_quality_warnings),
        "postmortem_top_error_tags": list(report.postmortem_top_error_tags),
        "emergency_trigger_count_keys": sorted(report.emergency_trigger_counts.keys()),
        "payload_hash": report.payload_hash(),
        "store_saved": "yes" if store_saved else "no",
        "markdown_written": "yes" if markdown_written else "no",
    }


def _print_text_summary(summary: dict[str, Any], *, out: TextIO) -> None:
    print(f"Paper review: {summary['outcome']}", file=out)
    for key in (
        "review_input",
        "review_id",
        "period",
        "sample_sufficiency",
        "nav_snapshot_count",
        "total_return_percent",
        "max_drawdown_percent",
        "recommendation_count",
        "data_quality_warning_count",
        "postmortem_top_error_tags",
        "emergency_trigger_count_keys",
        "payload_hash",
        "store_saved",
        "markdown_written",
    ):
        if key not in summary:
            continue
        print(f"{key}: {summary[key]}", file=out)


def _emit_summary(
    summary: dict[str, Any],
    *,
    as_json: bool,
    out: TextIO,
) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False), file=out)
    else:
        _print_text_summary(summary, out=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    input_path = Path(args.review_input)
    as_json = args.json

    if as_json and args.stdout_markdown:
        return _fail(
            "input",
            "--json and --stdout-markdown cannot be used together",
            as_json=as_json,
        )

    summary_out: TextIO = sys.stderr if args.stdout_markdown else sys.stdout

    if args.verbose:
        target = sys.stderr if as_json else summary_out
        print(f"verbose: review_input={input_path}", file=target)
        if args.store:
            print(f"verbose: store={args.store}", file=target)
        if args.markdown_out:
            print(f"verbose: markdown_out={args.markdown_out}", file=target)
        print(f"verbose: stdout_markdown={'yes' if args.stdout_markdown else 'no'}", file=target)

    if not input_path.is_file():
        return _fail(
            "input",
            f"input file not found: {input_path}",
            as_json=as_json,
            out=summary_out,
        )

    if args.verbose:
        target = sys.stderr if as_json else summary_out
        print(f"verbose: input {_input_fingerprint(input_path)}", file=target)

    try:
        raw_payload = json.loads(input_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return _fail(
            "input",
            f"unable to read input file: {exc}",
            as_json=as_json,
            out=summary_out,
        )
    except json.JSONDecodeError as exc:
        return _fail(
            "input",
            f"JSON parse failure: {exc.msg}",
            as_json=as_json,
            out=summary_out,
        )

    try:
        review_input = PaperReviewInput.model_validate(raw_payload)
    except ValidationError as exc:
        return _fail(
            "validation",
            _summarize_validation_error(exc),
            as_json=as_json,
            out=summary_out,
        )

    try:
        report = build_paper_review_report(review_input)
    except (ValidationError, ValueError) as exc:
        return _fail("report", str(exc), as_json=as_json, out=summary_out)

    store_saved = False
    if args.store:
        try:
            store = PaperReviewReportStore(Path(args.store))
            store.save(report)
            store_saved = True
        except ValueError as exc:
            return _fail("store", str(exc), as_json=as_json, out=summary_out)
        except OSError as exc:
            return _fail("store", f"unable to write store: {exc}", as_json=as_json, out=summary_out)

    markdown_written = False
    if args.markdown_out:
        try:
            markdown = render_paper_review_markdown(report)
            markdown_path = Path(args.markdown_out)
            markdown_path.parent.mkdir(parents=True, exist_ok=True)
            markdown_path.write_text(markdown, encoding="utf-8")
            markdown_written = True
        except OSError as exc:
            return _fail(
                "markdown",
                f"unable to write markdown: {exc}",
                as_json=as_json,
                out=summary_out,
            )

    if args.stdout_markdown:
        print(render_paper_review_markdown(report), file=sys.stdout)

    summary = _build_summary(
        review_input_path=input_path,
        report=report,
        store_saved=store_saved,
        markdown_written=markdown_written,
    )
    _emit_summary(summary, as_json=as_json, out=summary_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
