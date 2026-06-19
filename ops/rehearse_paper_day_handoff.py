#!/usr/bin/env python3
"""Offline Monday preflight rehearsal of the paper-day handoff flow (RTM-7c.10).

Copies a synthetic fixture (``summary.json`` + ``evidence.jsonl`` + optional
``stdout-envelope.json``) into a working directory, runs the offline validator /
report-generator path over it, and writes ``review-report.md`` — without ever
touching real KIS, the network, ``config``, or any credential. The verdict is
reused verbatim from ``ops/render_paper_day_report.py`` (which itself reuses
``ops/validate_paper_day_summary.py``); this script recomputes nothing.

Strictly offline and read-only with respect to the repository: it copies fixture
files (never mutating the source) and writes only under ``--work-dir``. It refuses
to write into the repository ``runtime/`` tree unless ``--allow-runtime-dir`` is
given, so a rehearsal can never masquerade as a real run artifact.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# ops/ is on sys.path when run as a script and is inserted by the test harness.
from render_paper_day_report import build_report
from validate_paper_day_summary import EXPECTED_SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_ROOT = (REPO_ROOT / "runtime").resolve()

_VALID_VERDICTS = ("PASS", "NO_GO", "FAIL", "NEEDS_REVIEW")


class RehearsalError(Exception):
    """Raised for an offline-rehearsal precondition or expectation failure."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline Monday preflight rehearsal of the paper-day handoff flow.",
    )
    parser.add_argument(
        "--fixture",
        required=True,
        help="fixture dir with summary.json + evidence.jsonl (+ stdout-envelope.json)",
    )
    parser.add_argument(
        "--work-dir",
        required=True,
        help="rehearsal output dir (created if absent); review-report.md is written here",
    )
    parser.add_argument(
        "--expect-verdict",
        default=None,
        choices=_VALID_VERDICTS,
        help="optional expected verdict; mismatch exits non-zero",
    )
    parser.add_argument(
        "--expect-source-kind",
        default="kis_live",
        help="expected source_kind passed through to the verdict (default: %(default)s)",
    )
    parser.add_argument(
        "--allow-runtime-dir",
        action="store_true",
        help="permit --work-dir inside the repository runtime/ tree (off by default)",
    )
    return parser


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def rehearse(
    *,
    fixture_dir: Path,
    work_dir: Path,
    expect_source_kind: str | None,
    allow_runtime_dir: bool,
) -> dict[str, object]:
    """Copy the fixture into work_dir, render the report, return a result dict.

    Never mutates the source fixture; writes only under work_dir. Raises
    RehearsalError on a precondition failure.
    """
    fixture_dir = fixture_dir.resolve()
    if not fixture_dir.is_dir():
        raise RehearsalError(f"fixture dir not found: {fixture_dir}")

    summary_src = fixture_dir / "summary.json"
    evidence_src = fixture_dir / "evidence.jsonl"
    envelope_src = fixture_dir / "stdout-envelope.json"
    if not summary_src.is_file():
        raise RehearsalError(f"missing summary.json in fixture: {fixture_dir}")
    if not evidence_src.is_file():
        raise RehearsalError(f"missing evidence.jsonl in fixture: {fixture_dir}")

    work_resolved = work_dir.resolve()
    if _is_within(work_resolved, _RUNTIME_ROOT) and not allow_runtime_dir:
        raise RehearsalError(
            "refusing to write a rehearsal under the repository runtime/ tree; "
            "pass --allow-runtime-dir to override"
        )
    if _is_within(work_resolved, fixture_dir):
        raise RehearsalError("--work-dir must not be inside the fixture dir")

    work_resolved.mkdir(parents=True, exist_ok=True)
    summary_dst = work_resolved / "summary.json"
    evidence_dst = work_resolved / "evidence.jsonl"
    shutil.copyfile(summary_src, summary_dst)
    shutil.copyfile(evidence_src, evidence_dst)
    envelope_dst: Path | None = None
    envelope_present = envelope_src.is_file()
    if envelope_present:
        envelope_dst = work_resolved / "stdout-envelope.json"
        shutil.copyfile(envelope_src, envelope_dst)

    report_path = work_resolved / "review-report.md"
    markdown, result = build_report(
        summary_path=summary_dst,
        evidence_path=evidence_dst,
        envelope_path=envelope_dst,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind=expect_source_kind,
        max_timeline_rows=200,
    )
    report_path.write_text(markdown + "\n", encoding="utf-8")

    return {
        "verdict": result["verdict"],
        "report_path": str(report_path),
        "envelope_present": envelope_present,
        "first_failure": result["observations"].get("stop_reason"),
        "hard_fail": list(result["hard_fail"]),
        "pass_blockers": list(result["pass_blockers"]),
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = rehearse(
            fixture_dir=Path(args.fixture),
            work_dir=Path(args.work_dir),
            expect_source_kind=args.expect_source_kind,
            allow_runtime_dir=args.allow_runtime_dir,
        )
    except RehearsalError as exc:
        print(f"rehearsal error: {exc}", file=sys.stderr)
        return 2

    verdict = result["verdict"]
    print(f"verdict: {verdict}")
    print(f"report written: {result['report_path']}")
    print(f"envelope present: {result['envelope_present']}")
    if result["hard_fail"]:
        print(f"hard_fail: {', '.join(result['hard_fail'])}")
    if result["pass_blockers"]:
        print(f"pass_blockers: {', '.join(result['pass_blockers'])}")

    if args.expect_verdict is not None and verdict != args.expect_verdict:
        print(
            f"expected verdict {args.expect_verdict} but got {verdict}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
