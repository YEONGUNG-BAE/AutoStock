"""CLI for the offline Phase 2f forward monthly observation harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from backtest_engine.forward_monthly_observation import (
    ForwardObservationError,
    finalize_forward_monthly_observation,
    prepare_forward_monthly_observation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forward_monthly_observation_cli",
        description="Offline PREPARE/FINALIZE forward monthly observation harness.",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    prepare = subparsers.add_parser("prepare")
    finalize = subparsers.add_parser("finalize")
    for command in (prepare, finalize):
        command.add_argument("--repo-root", type=Path, required=True)
        command.add_argument("--data-root", type=Path, required=True)
        command.add_argument("--output-root", type=Path, required=True)
        command.add_argument("--expected-git-main", required=True)
        command.add_argument("--candidate-allocator-version", required=True)
        command.add_argument(
            "--safe-overwrite",
            action="store_true",
            help="Allow only policy-safe replacement of an existing external artifact.",
        )
    prepare.add_argument("--report-month", required=True)
    finalize.add_argument("--decision-snapshot", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.operation == "prepare":
            result = prepare_forward_monthly_observation(
                repo_root=args.repo_root,
                data_root=args.data_root,
                output_root=args.output_root,
                report_month=args.report_month,
                expected_git_main=args.expected_git_main,
                candidate_allocator_version=args.candidate_allocator_version,
                safe_overwrite=args.safe_overwrite,
            )
            summary = {
                "operation": "prepare",
                "report_month": result.snapshot.report_month,
                "observation_index": result.snapshot.observation_index,
                "snapshot_path": result.snapshot_path,
                "snapshot_sha256": result.snapshot_sha256,
            }
        else:
            result = finalize_forward_monthly_observation(
                repo_root=args.repo_root,
                data_root=args.data_root,
                output_root=args.output_root,
                decision_snapshot_path=args.decision_snapshot,
                expected_git_main=args.expected_git_main,
                candidate_allocator_version=args.candidate_allocator_version,
                safe_overwrite=args.safe_overwrite,
            )
            summary = {
                "operation": "finalize",
                "evidence_status": result.evidence_status,
                "metrics_path": result.metrics_path,
                "manifest_path": result.manifest_path,
            }
    except ForwardObservationError as exc:
        print(json.dumps({"status": "ERROR", "message": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
