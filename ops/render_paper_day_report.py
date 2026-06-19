#!/usr/bin/env python3
"""Offline paper-day diagnostic Markdown report generator (RTM-7c.8).

Renders a human-readable Reviewer report from a persisted ``summary.json``, an
``evidence.jsonl``, and (optionally) the captured ``stdout-envelope.json``. The
PASS / NO_GO / FAIL / NEEDS_REVIEW verdict is **not** recomputed here — it is
reused verbatim from ``ops/validate_paper_day_summary.py`` (``classify``), so the
report and the validator can never disagree.

Strictly offline and read-only: no network, no ``config``/credential reads, no
file mutation other than the explicit ``--out`` target.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

# Same directory as the validator; ops/ is on sys.path when run as a script and
# is inserted by the test harness when imported.
from validate_paper_day_summary import (
    EXPECTED_SCHEMA_VERSION,
    FAIL,
    NEEDS_REVIEW,
    NO_GO,
    PASS,
    classify,
    _load_json_object,
    _scan_evidence,
)

_DEFAULT_TIMELINE_ROWS = 200

_CLEAN_EXIT_FIELDS = (
    "summary_publication_outcome",
    "cleanup_outcome",
    "runtime_lock_fd_closed",
    "runtime_lock_absent_confirmed",
    "runtime_lock_release_reason_code",
    "nonterminal_journal",
)

_SAFETY_FIELDS = (
    "paper_only",
    "activation_authorized",
    "automatic_restart",
    "real_order_adapter_constructed",
)

_SOURCE_READINESS_COUNTERS = (
    "connect_attempts",
    "connected",
    "subscription_requests",
    "subscription_acks",
    "subscription_rejections",
    "all_subscribed",
    "disconnects",
)

_REVIEW_COUNTERS = (
    "normalized_trades",
    "normalized_quotes",
    "health_hold",
    "health_pass",
    "trigger_evaluations",
    "publication_slot_outcomes",
    "journal_committed",
    "orders",
    "fills",
)

_NOT_IN_ARTIFACTS = "operator-supplied, not present in artifacts"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline paper-day diagnostic Markdown report generator.",
    )
    parser.add_argument("--summary", required=True, help="persisted summary.json path")
    parser.add_argument("--evidence", required=True, help="evidence.jsonl path")
    parser.add_argument(
        "--envelope",
        default=None,
        help="optional stdout-envelope.json (the run's --json output)",
    )
    parser.add_argument("--out", default=None, help="write Markdown to this path (else stdout)")
    parser.add_argument(
        "--expect-schema-version",
        default=EXPECTED_SCHEMA_VERSION,
        help="expected schema_version (default: %(default)s)",
    )
    parser.add_argument(
        "--expect-source-kind",
        default=None,
        help="optional expected source_kind (e.g. kis_live); mismatch blocks PASS",
    )
    parser.add_argument(
        "--max-timeline-rows",
        type=int,
        default=_DEFAULT_TIMELINE_ROWS,
        help="max evidence rows rendered in the timeline (default: %(default)s)",
    )
    return parser


def _read_timeline_rows(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Read evidence rows for the timeline only (verdict-irrelevant). Returns
    (rows, had_error). Never mutates the file."""
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows, True
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return rows, True
    with handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows, False


def _fmt(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _present(merged: dict[str, Any], field: str) -> str:
    return _fmt(merged[field]) if field in merged else "missing"


def _counter(values: dict[str, Any], name: str) -> str:
    return _fmt(values[name]) if name in values else "missing"


def build_report(
    *,
    summary_path: Path,
    evidence_path: Path,
    envelope_path: Path | None,
    expect_schema_version: str,
    expect_source_kind: str | None,
    max_timeline_rows: int,
) -> tuple[str, dict[str, Any]]:
    summary, summary_error = _load_json_object(summary_path, label="summary")
    envelope: dict[str, Any] | None = None
    envelope_error: str | None = None
    if envelope_path is not None:
        envelope, envelope_error = _load_json_object(envelope_path, label="envelope")
        # A bad envelope is treated as absent: the verdict then reports
        # missing_from_persisted_summary rather than inventing the fields.

    evidence_scan = _scan_evidence(evidence_path)
    result = classify(
        summary=summary,
        summary_error=summary_error,
        envelope=envelope,
        evidence=evidence_scan,
        expect_schema_version=expect_schema_version,
        expect_source_kind=expect_source_kind,
    )

    merged: dict[str, Any] = dict(summary or {})
    if envelope:
        merged.update(envelope)

    counters_block = merged.get("counters")
    counter_values: dict[str, Any] = {}
    reason_counts: dict[str, Any] = {}
    timestamps: dict[str, Any] = {}
    if isinstance(counters_block, dict):
        if isinstance(counters_block.get("counters"), dict):
            counter_values = counters_block["counters"]
        if isinstance(counters_block.get("reason_counts"), dict):
            reason_counts = counters_block["reason_counts"]
        if isinstance(counters_block.get("timestamps"), dict):
            timestamps = counters_block["timestamps"]

    timeline_rows, timeline_error = _read_timeline_rows(evidence_path)
    checks_by_name = {c["name"]: c for c in result["checks"]}

    markdown = _render_markdown(
        result=result,
        merged=merged,
        checks_by_name=checks_by_name,
        evidence_scan=evidence_scan,
        counter_values=counter_values,
        reason_counts=reason_counts,
        timestamps=timestamps,
        timeline_rows=timeline_rows,
        timeline_error=timeline_error,
        envelope_provided=envelope_path is not None,
        envelope_error=envelope_error,
        summary_error=summary_error,
        max_timeline_rows=max_timeline_rows,
    )
    return markdown, result


def _render_markdown(
    *,
    result: dict[str, Any],
    merged: dict[str, Any],
    checks_by_name: dict[str, dict[str, str]],
    evidence_scan: dict[str, Any],
    counter_values: dict[str, Any],
    reason_counts: dict[str, Any],
    timestamps: dict[str, Any],
    timeline_rows: list[dict[str, Any]],
    timeline_error: bool,
    envelope_provided: bool,
    envelope_error: str | None,
    summary_error: str | None,
    max_timeline_rows: int,
) -> str:
    obs = result["observations"]
    out: list[str] = []
    w = out.append

    def status_of(field: str) -> str:
        chk = checks_by_name.get(field)
        return chk["status"] if chk else "missing"

    w("# Paper Day Diagnostic Review Report")
    w("")

    # Run identity.
    w("## Run identity")
    w("")
    w("| field | value |")
    w("| --- | --- |")
    for field in ("run_id", "session_date", "symbol", "market", "source_kind", "schema_version"):
        w(f"| {field} | {_present(merged, field)} |")
    w("")

    # Verdict.
    w("## Verdict")
    w("")
    w(f"- **verdict: {result['verdict']}**")
    w(f"- runtime outcome: {_fmt(obs['outcome'])}")
    w(f"- stop_reason: {_fmt(obs['stop_reason'])}")
    w(f"- PILOT_EXIT: {_NOT_IN_ARTIFACTS}")
    if result["hard_fail"]:
        w(f"- hard_fail: {', '.join(result['hard_fail'])}")
    if result["pass_blockers"]:
        w(f"- pass_blockers: {', '.join(result['pass_blockers'])}")
    if result["missing_from_persisted_summary"]:
        w(
            "- missing_from_persisted_summary: "
            + ", ".join(result["missing_from_persisted_summary"])
        )
    if summary_error:
        w(f"- summary_error: {summary_error}")
    if not envelope_provided:
        w("- envelope: not provided — PASS cannot be confirmed (NEEDS_REVIEW for envelope clauses)")
    elif envelope_error:
        w(f"- envelope_error: {envelope_error} — treated as absent")
    w("")

    # Clean-exit clauses.
    w("## Clean-exit clauses")
    w("")
    w("| clause | value | status |")
    w("| --- | --- | --- |")
    for field in _CLEAN_EXIT_FIELDS:
        w(f"| {field} | {_present(merged, field)} | {status_of(field)} |")
    w("")

    # Source readiness.
    w("## Source readiness")
    w("")
    w("| counter | value |")
    w("| --- | --- |")
    for name in _SOURCE_READINESS_COUNTERS:
        w(f"| {name} | {_counter(counter_values, name)} |")
    w("")
    source_reasons = {k: v for k, v in reason_counts.items() if k.startswith("source_")}
    if source_reasons:
        w("source_* reason counts:")
        w("")
        for key in sorted(source_reasons):
            w(f"- {key}: {source_reasons[key]}")
    else:
        w("source_* reason counts: none")
    w("")

    # Paper-only safety proof.
    w("## Paper-only safety proof")
    w("")
    w("| field | value | status |")
    w("| --- | --- | --- |")
    for field in _SAFETY_FIELDS:
        w(f"| {field} | {_present(merged, field)} | {status_of(field)} |")
    sensitive_any = evidence_scan["sensitive_rows"] > 0
    sensitive_status = "fail" if sensitive_any else "ok"
    w(
        f"| sensitive_data_present_any | {_fmt(sensitive_any)} "
        f"({evidence_scan['sensitive_rows']} rows) | {sensitive_status} |"
    )
    w(f"| tracked_runtime | {_NOT_IN_ARTIFACTS} | n/a |")
    w("")

    # Evidence timeline.
    w("## Evidence timeline")
    w("")
    if timeline_error:
        w("evidence file missing or unreadable.")
    elif not timeline_rows:
        w("no evidence rows.")
    else:
        total = len(timeline_rows)
        shown = timeline_rows[:max_timeline_rows]
        w("| recorded_at | stage | event | reason_code |")
        w("| --- | --- | --- | --- |")
        for row in shown:
            w(
                f"| {_fmt(row.get('recorded_at'))} | {_fmt(row.get('stage'))} "
                f"| {_fmt(row.get('event'))} | {_fmt(row.get('reason_code'))} |"
            )
        if total > len(shown):
            w("")
            w(f"_(showing {len(shown)} of {total} rows; raise --max-timeline-rows to see more)_")
    if evidence_scan["malformed_rows"]:
        w("")
        w(f"**malformed evidence rows: {evidence_scan['malformed_rows']} (blocks PASS)**")
    w("")

    # First failure.
    w("## First failure")
    w("")
    first = evidence_scan["first_failure"]
    if first is None:
        w("None observed.")
    else:
        w(f"- stage: {_fmt(first.get('stage'))}")
        w(f"- reason_code: {_fmt(first.get('reason_code'))}")
        w(f"- recorded_at: {_fmt(first.get('recorded_at'))}")
    w("")

    # Counters.
    w("## Counters")
    w("")
    w("| counter | value |")
    w("| --- | --- |")
    for name in _REVIEW_COUNTERS:
        w(f"| {name} | {_counter(counter_values, name)} |")
    w("")
    if reason_counts:
        w("reason_counts:")
        w("")
        for key in sorted(reason_counts):
            w(f"- {key}: {reason_counts[key]}")
        w("")
    if timestamps:
        w("timestamps:")
        w("")
        for key in sorted(timestamps):
            w(f"- {key}: {timestamps[key]}")
        w("")

    # Orders and fills.
    w("## Orders and fills")
    w("")
    w(f"- orders: {_counter(counter_values, 'orders')}")
    w(f"- fills: {_counter(counter_values, 'fills')}")
    w("")
    w("`orders > 0` is **not** required for PASS. A clean day with zero paper orders")
    w("is valid when trigger/health/decision conditions do not require an order.")
    w("")

    # Journal / completion state.
    w("## Journal/completion state")
    w("")
    w(f"- nonterminal_journal: {_present(merged, 'nonterminal_journal')}")
    w(f"- stop_reason: {_fmt(obs['stop_reason'])}")
    w(f"- journal_committed: {_counter(counter_values, 'journal_committed')}")
    for key in ("journal_uncertain", "reconcile_required", "nonterminal_journal"):
        if key in reason_counts:
            w(f"- reason {key}: {reason_counts[key]}")
    w("")

    # Publication and lock state.
    w("## Publication and lock state")
    w("")
    w("| field | value |")
    w("| --- | --- |")
    for field in (
        "summary_publication_outcome",
        "summary_publication_reason_codes",
        "runtime_lock_fd_closed",
        "runtime_lock_absent_confirmed",
        "runtime_lock_identity_matched",
        "runtime_lock_release_reason_code",
    ):
        w(f"| {field} | {_present(merged, field)} |")
    w("")

    # Operator git/runtime hygiene.
    w("## Operator git/runtime hygiene")
    w("")
    w("Run and record (not derivable from artifacts):")
    w("")
    w("```bash")
    w("git status --short")
    w("git ls-files runtime")
    w("```")
    w("")

    # Reviewer checklist.
    w("## Reviewer checklist")
    w("")
    for item in (
        "PILOT_EXIT captured immediately",
        "summary/envelope consistency reviewed",
        "evidence has no sensitive_data_present=true",
        "source readiness reviewed",
        "paper-only flags reviewed",
        "journal/completion state reviewed",
        "git status --short reviewed",
        "git ls-files runtime reviewed",
        "no live order path observed",
    ):
        w(f"- [ ] {item}")
    w("")

    # Remaining NO-GO items.
    w("## Remaining NO-GO items")
    w("")
    if result["verdict"] != PASS:
        w(f"- Verdict is {result['verdict']}: not a clean PASS. Triage the first failed stage above.")
    w("- 1-day pilot remains NO-GO until Reviewer PASS.")
    w("- Live order / runtime activation / automatic restart remain prohibited.")
    w("")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    markdown, result = build_report(
        summary_path=Path(args.summary),
        evidence_path=Path(args.evidence),
        envelope_path=Path(args.envelope) if args.envelope else None,
        expect_schema_version=args.expect_schema_version,
        expect_source_kind=args.expect_source_kind,
        max_timeline_rows=args.max_timeline_rows,
    )
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown + "\n", encoding="utf-8")
        stream: TextIO = sys.stdout
        print(f"verdict: {result['verdict']}", file=stream)
        print(f"report written: {out_path}", file=stream)
    else:
        print(markdown)
    return 0 if result["verdict"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
