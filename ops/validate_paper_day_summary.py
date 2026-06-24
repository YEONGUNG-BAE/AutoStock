#!/usr/bin/env python3
"""Offline paper-day diagnostic summary/evidence validator (RTM-7c.7).

Reads a persisted ``summary.json`` and an ``evidence.jsonl`` produced by
``ops/run_attended_paper_day.py`` and classifies the run as
``PASS`` / ``NO_GO`` / ``FAIL`` / ``NEEDS_REVIEW``.

This helper is strictly offline and read-only:

- never opens a network connection,
- never reads ``config/config.toml`` or any credential/env value,
- never mutates, creates, or deletes any file.

Persisted ``summary.json`` holds only the mechanical summary scalars
(``_build_summary`` output). The cleanup/publication/lock keys
(``summary_publication_outcome``, ``cleanup_outcome``, ``runtime_lock_*``) live
only in the returned stdout envelope and are **never** written to the file. When
those envelope-only fields are absent the validator does **not** invent them: it
reports ``missing_from_persisted_summary`` and returns ``NEEDS_REVIEW`` so the
Operator can re-run with the stdout envelope supplied via ``--envelope``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

EXPECTED_SCHEMA_VERSION = "paper_day_diagnostic.v1"

PASS = "PASS"
NO_GO = "NO_GO"
FAIL = "FAIL"
NEEDS_REVIEW = "NEEDS_REVIEW"

# Mechanical safety scalars that must hold their fixed value on every paper-day
# run. An explicit wrong value is a hard FAIL (it cannot be a clean PASS); a
# missing value is a PASS blocker (cannot be asserted, not proven violated).
_SAFETY_EXPECT: dict[str, bool] = {
    "paper_only": True,
    "activation_authorized": False,
    "real_order_adapter_constructed": False,
    "automatic_restart": False,
}

# Cleanup/publication/lock keys that live only in the stdout envelope, never in
# the persisted summary file.
_ENVELOPE_ONLY = (
    "summary_publication_outcome",
    "cleanup_outcome",
    "runtime_lock_fd_closed",
    "runtime_lock_absent_confirmed",
    "runtime_lock_release_reason_code",
)

# Counter names surfaced for Operator review (informational, never gating).
_REVIEW_COUNTERS = (
    "quote_subscription_acks",
    "quote_frames",
    "normalized_trades",
    "normalized_quotes",
    "health_pass",
    "health_hold",
    "trigger_evaluations",
    "publication_slot_outcomes",
    "journal_committed",
    "orders",
    "fills",
)


class ValidatorError(Exception):
    """A CLI-level error (bad arguments). Not a run verdict."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline paper-day diagnostic summary/evidence validator.",
    )
    parser.add_argument("--summary", required=True, help="persisted summary.json path")
    parser.add_argument("--evidence", required=True, help="evidence.jsonl path")
    parser.add_argument(
        "--envelope",
        default=None,
        help=(
            "optional JSON file with the run's stdout envelope (the operator-pasted "
            "--json output). Supplies cleanup/publication/lock fields absent from "
            "the persisted summary."
        ),
    )
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
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _load_json_object(path: Path, *, label: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read a JSON object. Returns (object, error). Never mutates the file."""
    if not path.is_file():
        return None, f"{label}_missing"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, f"{label}_unreadable"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None, f"{label}_malformed"
    if not isinstance(parsed, dict):
        return None, f"{label}_not_object"
    return parsed, None


def _scan_evidence(path: Path) -> dict[str, Any]:
    """Stream evidence.jsonl read-only. Surface row count, sensitive rows, and the
    first ``failed_closed`` row without loading the whole file into memory."""
    result: dict[str, Any] = {
        "evidence_error": None,
        "rows": 0,
        "malformed_rows": 0,
        "sensitive_rows": 0,
        "first_failure": None,
        "latest_session": None,
        "latest_quote_readiness": None,
        "source_drop_subcodes": {},
    }
    if not path.is_file():
        result["evidence_error"] = "evidence_missing"
        return result
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        result["evidence_error"] = "evidence_unreadable"
        return result
    with handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                result["malformed_rows"] += 1
                continue
            if not isinstance(row, dict):
                result["malformed_rows"] += 1
                continue
            result["rows"] += 1
            if row.get("sensitive_data_present") is not False:
                result["sensitive_rows"] += 1
            if result["first_failure"] is None and row.get("event") == "failed_closed":
                result["first_failure"] = {
                    "recorded_at": row.get("recorded_at"),
                    "stage": row.get("stage"),
                    "event": row.get("event"),
                    "reason_code": row.get("reason_code"),
                }
            snapshot = row.get("snapshot")
            if isinstance(snapshot, dict):
                if snapshot.get("session_state") is not None:
                    result["latest_session"] = {
                        "recorded_at": row.get("recorded_at"),
                        "stage": row.get("stage"),
                        "event": row.get("event"),
                        "reason_code": row.get("reason_code"),
                        "session_state": snapshot.get("session_state"),
                        "market_data_health": snapshot.get("market_data_health"),
                        "required_session_state": snapshot.get("required_session_state"),
                    }
                if any(
                    key in snapshot
                    for key in (
                        "quote_subscription_ready",
                        "quote_frames",
                        "normalized_quotes",
                    )
                ):
                    result["latest_quote_readiness"] = {
                        "recorded_at": row.get("recorded_at"),
                        "stage": row.get("stage"),
                        "event": row.get("event"),
                        "quote_subscription_ready": snapshot.get("quote_subscription_ready"),
                        "quote_frames": snapshot.get("quote_frames"),
                        "normalized_quotes": snapshot.get("normalized_quotes"),
                        "market_data_health": snapshot.get("market_data_health"),
                        "session_state": snapshot.get("session_state"),
                    }
                reason_subcode = snapshot.get("reason_subcode")
                if row.get("reason_code") == "source_error" and isinstance(reason_subcode, str):
                    subcodes = result["source_drop_subcodes"]
                    subcodes[reason_subcode] = subcodes.get(reason_subcode, 0) + 1
    return result


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def classify(
    *,
    summary: dict[str, Any] | None,
    summary_error: str | None,
    envelope: dict[str, Any] | None,
    evidence: dict[str, Any],
    expect_schema_version: str,
    expect_source_kind: str | None,
) -> dict[str, Any]:
    """Pure classification over already-loaded artifacts. No I/O."""
    checks: list[dict[str, str]] = []
    pass_blockers: list[str] = []
    hard_fail: list[str] = []

    # Merged view: the stdout envelope is the post-finalize operator verdict, so
    # it overlays the persisted mechanical summary for any field it carries.
    merged: dict[str, Any] = dict(summary or {})
    if envelope:
        merged.update(envelope)

    if summary_error is not None:
        checks.append(_check("summary_file", "fail", summary_error))
        pass_blockers.append(summary_error)
    else:
        checks.append(_check("summary_file", "ok", "summary loaded"))

    # Evidence presence + integrity.
    if evidence["evidence_error"] is not None:
        checks.append(_check("evidence_file", "fail", evidence["evidence_error"]))
        pass_blockers.append(evidence["evidence_error"])
    else:
        checks.append(_check("evidence_file", "ok", f"{evidence['rows']} rows"))
    if evidence["malformed_rows"]:
        checks.append(
            _check("evidence_integrity", "fail", f"{evidence['malformed_rows']} malformed rows")
        )
        pass_blockers.append("evidence_malformed")

    # Sensitive-data leak is a hard safety failure regardless of outcome.
    if evidence["sensitive_rows"]:
        checks.append(
            _check("sensitive_data_present", "fail", f"{evidence['sensitive_rows']} rows true")
        )
        hard_fail.append("sensitive_data_present")
    elif evidence["evidence_error"] is None:
        checks.append(_check("sensitive_data_present", "ok", "all rows false"))

    # schema_version.
    schema = merged.get("schema_version")
    if schema is None:
        checks.append(_check("schema_version", "missing", "absent"))
        pass_blockers.append("schema_version_missing")
    elif schema != expect_schema_version:
        checks.append(_check("schema_version", "fail", f"{schema} != {expect_schema_version}"))
        pass_blockers.append("schema_version_mismatch")
    else:
        checks.append(_check("schema_version", "ok", str(schema)))

    # Mechanical safety scalars.
    for field, expected in _SAFETY_EXPECT.items():
        if field not in merged:
            checks.append(_check(field, "missing", "absent"))
            pass_blockers.append(f"{field}_missing")
        elif merged[field] != expected:
            checks.append(_check(field, "fail", f"{merged[field]!r} != {expected!r}"))
            hard_fail.append(field)
        else:
            checks.append(_check(field, "ok", repr(expected)))

    # source_kind (informational unless an expectation is supplied).
    source_kind = merged.get("source_kind")
    if expect_source_kind is not None:
        if source_kind == expect_source_kind:
            checks.append(_check("source_kind", "ok", str(source_kind)))
        else:
            checks.append(
                _check("source_kind", "fail", f"{source_kind} != {expect_source_kind}")
            )
            pass_blockers.append("source_kind_mismatch")
    else:
        checks.append(_check("source_kind", "info", str(source_kind)))

    # nonterminal_journal must be terminal (0 or null) for a clean PASS.
    nonterminal = merged.get("nonterminal_journal", "__absent__")
    if nonterminal in (0, None):
        checks.append(_check("nonterminal_journal", "ok", str(nonterminal)))
    elif nonterminal == "__absent__":
        checks.append(_check("nonterminal_journal", "missing", "absent"))
        pass_blockers.append("nonterminal_journal_missing")
    else:
        checks.append(_check("nonterminal_journal", "fail", str(nonterminal)))
        pass_blockers.append("nonterminal_journal")

    # Envelope-only cleanup/publication/lock fields. Absence is reported as
    # missing_from_persisted_summary — never invented.
    missing_from_persisted: list[str] = [f for f in _ENVELOPE_ONLY if f not in merged]
    if missing_from_persisted:
        for field in missing_from_persisted:
            checks.append(_check(field, "missing", "missing_from_persisted_summary"))
        pass_blockers.append("missing_from_persisted_summary")

    # Clean-exit clauses (only meaningful when present).
    _clause(checks, pass_blockers, merged, "summary_publication_outcome", "WRITTEN")
    _clause(checks, pass_blockers, merged, "cleanup_outcome", "CLEAN")
    _clause(checks, pass_blockers, merged, "runtime_lock_fd_closed", True)
    _clause(checks, pass_blockers, merged, "runtime_lock_absent_confirmed", True)
    if "runtime_lock_release_reason_code" in merged:
        reason = merged["runtime_lock_release_reason_code"]
        if reason is None:
            checks.append(_check("runtime_lock_release_reason_code", "ok", "null"))
        else:
            checks.append(_check("runtime_lock_release_reason_code", "fail", str(reason)))
            pass_blockers.append("runtime_lock_release_reason_code")

    # Outcome-driven verdict with safety precedence.
    outcome = merged.get("outcome")
    stop_reason = merged.get("stop_reason")
    if hard_fail:
        verdict = FAIL
    elif outcome == FAIL:
        verdict = FAIL
    elif outcome == NO_GO:
        verdict = NO_GO
    elif outcome == PASS:
        verdict = PASS if not pass_blockers else NEEDS_REVIEW
    else:
        verdict = NEEDS_REVIEW

    observations: dict[str, Any] = {
        "outcome": outcome,
        "stop_reason": stop_reason,
        "source_kind": source_kind,
        "run_id": merged.get("run_id"),
        "session_date": merged.get("session_date"),
        "symbol": merged.get("symbol"),
        "nonterminal_journal": None if nonterminal == "__absent__" else nonterminal,
        "evidence_rows": evidence["rows"],
        "first_failure": evidence["first_failure"],
        "latest_session": evidence.get("latest_session"),
        "latest_quote_readiness": evidence.get("latest_quote_readiness"),
        "source_drop_subcodes": evidence.get("source_drop_subcodes", {}),
        "counters": _select_counters(merged),
        "reason_counts": _reason_counts(merged),
    }

    return {
        "verdict": verdict,
        "hard_fail": hard_fail,
        "pass_blockers": pass_blockers,
        "missing_from_persisted_summary": missing_from_persisted,
        "checks": checks,
        "observations": observations,
    }


def _clause(
    checks: list[dict[str, str]],
    pass_blockers: list[str],
    merged: dict[str, Any],
    field: str,
    expected: Any,
) -> None:
    if field not in merged:
        return  # absence handled by missing_from_persisted_summary
    actual = merged[field]
    if actual == expected and type(actual) is type(expected):
        checks.append(_check(field, "ok", repr(expected)))
    else:
        checks.append(_check(field, "fail", f"{actual!r} != {expected!r}"))
        pass_blockers.append(field)


def _select_counters(merged: dict[str, Any]) -> dict[str, Any]:
    counters_block = merged.get("counters")
    values: dict[str, Any] = {}
    if isinstance(counters_block, dict):
        inner = counters_block.get("counters")
        if isinstance(inner, dict):
            for name in _REVIEW_COUNTERS:
                if name in inner:
                    values[name] = inner[name]
    return values


def _reason_counts(merged: dict[str, Any]) -> dict[str, Any]:
    counters_block = merged.get("counters")
    if isinstance(counters_block, dict):
        reason = counters_block.get("reason_counts")
        if isinstance(reason, dict):
            return dict(reason)
    return {}


def run_validate(
    *,
    summary_path: Path,
    evidence_path: Path,
    envelope_path: Path | None,
    expect_schema_version: str,
    expect_source_kind: str | None,
) -> dict[str, Any]:
    summary, summary_error = _load_json_object(summary_path, label="summary")
    envelope: dict[str, Any] | None = None
    if envelope_path is not None:
        envelope, envelope_error = _load_json_object(envelope_path, label="envelope")
        if envelope_error is not None:
            raise ValidatorError(f"--envelope could not be read: {envelope_error}")
    evidence = _scan_evidence(evidence_path)
    return classify(
        summary=summary,
        summary_error=summary_error,
        envelope=envelope,
        evidence=evidence,
        expect_schema_version=expect_schema_version,
        expect_source_kind=expect_source_kind,
    )


def _emit(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False), file=out)
        return
    print(f"verdict: {payload['verdict']}", file=out)
    obs = payload["observations"]
    print(f"outcome: {obs['outcome']}  stop_reason: {obs['stop_reason']}", file=out)
    print(f"source_kind: {obs['source_kind']}  symbol: {obs['symbol']}", file=out)
    if payload["hard_fail"]:
        print(f"hard_fail: {', '.join(payload['hard_fail'])}", file=out)
    if payload["pass_blockers"]:
        print(f"pass_blockers: {', '.join(payload['pass_blockers'])}", file=out)
    if payload["missing_from_persisted_summary"]:
        print(
            "missing_from_persisted_summary: "
            + ", ".join(payload["missing_from_persisted_summary"])
            + " (re-run with --envelope to supply the stdout envelope)",
            file=out,
        )
    for chk in payload["checks"]:
        if chk["status"] != "ok":
            print(f"  [{chk['status']}] {chk['name']}: {chk['detail']}", file=out)
    if obs["first_failure"] is not None:
        ff = obs["first_failure"]
        print(
            f"first_failure: stage={ff['stage']} reason={ff['reason_code']} at={ff['recorded_at']}",
            file=out,
        )
    if obs["latest_session"] is not None:
        session = obs["latest_session"]
        print(
            "latest_session: "
            f"session_state={session.get('session_state')} "
            f"market_data_health={session.get('market_data_health')} "
            f"at={session.get('recorded_at')}",
            file=out,
        )
    if obs["latest_quote_readiness"] is not None:
        quote = obs["latest_quote_readiness"]
        print(
            "latest_quote_readiness: "
            f"quote_subscription_ready={quote.get('quote_subscription_ready')} "
            f"quote_frames={quote.get('quote_frames')} "
            f"normalized_quotes={quote.get('normalized_quotes')}",
            file=out,
        )
    if obs["source_drop_subcodes"]:
        print(f"source_drop_subcodes: {obs['source_drop_subcodes']}", file=out)
    if obs["counters"]:
        print(f"counters: {obs['counters']}", file=out)
    if obs["reason_counts"]:
        print(f"reason_counts: {obs['reason_counts']}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = run_validate(
            summary_path=Path(args.summary),
            evidence_path=Path(args.evidence),
            envelope_path=Path(args.envelope) if args.envelope else None,
            expect_schema_version=args.expect_schema_version,
            expect_source_kind=args.expect_source_kind,
        )
    except ValidatorError as exc:
        if args.json:
            print(json.dumps({"verdict": NEEDS_REVIEW, "error": str(exc)}, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    _emit(payload, as_json=args.json, out=sys.stdout)
    return 0 if payload["verdict"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
