"""Operator local monthly real-data evaluation dry-run CLI for Phase 2d-4.

This module exposes a command-line entry point that calls
``run_local_monthly_evaluation_dry_run(...)`` and prints a sanitized summary to
stdout. It does not fetch or download data, read CSVs directly, write report
files, create artifacts, or produce investment conclusions.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from backtest_engine.local_evaluation import (
    LocalMonthlyEvaluationDryRunResult,
    run_local_monthly_evaluation_dry_run,
)

_FORBIDDEN_CLI_ARGS = frozenset(
    {
        "--output",
        "--output-path",
        "--report-path",
        "--artifact-path",
        "--write",
        "--save",
        "--fetch",
        "--download",
        "--live",
        "--paper",
    }
)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the operator local dry-run CLI argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Run an in-memory local monthly real-data evaluation dry-run and "
            "print a sanitized summary."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="AutoStock repository root (default: current working directory).",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help=(
            "Sibling autostock-data directory "
            "(default: repo_root.parent / autostock-data)."
        ),
    )
    parser.add_argument(
        "--initial-cash-krw",
        type=Decimal,
        default=Decimal("100000000"),
        help="Initial cash balance in KRW.",
    )
    parser.add_argument(
        "--cash-min-weight",
        type=Decimal,
        default=Decimal("0.05"),
        help="Minimum cash weight for rebalancing.",
    )
    parser.add_argument(
        "--rolling-lookback-count",
        type=int,
        default=3,
        help="Rolling feature lookback count in common periods.",
    )
    parser.add_argument(
        "--fee-bps",
        type=Decimal,
        default=Decimal("10"),
        help="Trading fee in basis points.",
    )
    parser.add_argument(
        "--kr-sell-tax-bps",
        type=Decimal,
        default=Decimal("23"),
        help="KR sell tax in basis points.",
    )
    parser.add_argument(
        "--fx-spread-bps",
        type=Decimal,
        default=Decimal("15"),
        help="FX spread in basis points.",
    )
    parser.add_argument(
        "--show-markdown-preview",
        action="store_true",
        help="Print the first 20 lines of the in-memory markdown report.",
    )
    return parser


def _find_forbidden_cli_arg(argv: Sequence[str]) -> str | None:
    for arg in argv:
        if arg in _FORBIDDEN_CLI_ARGS:
            return arg
        for forbidden in _FORBIDDEN_CLI_ARGS:
            if arg.startswith(f"{forbidden}="):
                return forbidden
    return None


def render_local_dry_run_summary(
    result: LocalMonthlyEvaluationDryRunResult,
    *,
    show_markdown_preview: bool = False,
) -> str:
    """Render a sanitized stdout summary for a local dry-run result."""
    dataset = result.dataset
    run_config = result.run_config
    walk_forward = result.walk_forward_result
    metrics = result.benchmark_relative_result.metrics

    period_specs = run_config.period_specs
    nav_points = walk_forward.nav_points

    lines = [
        "AutoStock local monthly evaluation dry-run",
        f"policy: {result.local_monthly_evaluation_dry_run_policy}",
        f"dataset_policy: {dataset.local_monthly_dataset_policy}",
        f"run_config_policy: {run_config.local_monthly_run_config_policy}",
        f"period_count: {len(period_specs)}",
        f"nav_point_count: {len(nav_points)}",
        f"benchmark_point_count: {len(dataset.benchmark_points)}",
        f"common_period_count: {len(dataset.common_periods)}",
        f"first_common_period: {dataset.common_periods[0]}",
        f"last_common_period: {dataset.common_periods[-1]}",
        (
            "first_execution_as_of: "
            f"{period_specs[0].intended_execution_time.isoformat()}"
        ),
        (
            "last_execution_as_of: "
            f"{period_specs[-1].intended_execution_time.isoformat()}"
        ),
        f"final_portfolio_value_krw: {nav_points[-1].portfolio_value_krw}",
        f"total_cost_krw: {walk_forward.total_cost_krw}",
        f"terminal_strategy_return: {metrics.bot_total_return_percent}",
        f"terminal_benchmark_return: {metrics.benchmark_total_return_percent}",
        f"terminal_excess_return: {metrics.excess_return_percent}",
        f"max_relative_drawdown: {metrics.relative_drawdown_percent}",
        "warnings:",
    ]
    for warning in result.warnings:
        lines.append(f"- {warning}")

    if show_markdown_preview:
        preview_lines = result.report_bundle.markdown_report.splitlines()[:20]
        if preview_lines:
            lines.append("")
            lines.append("markdown_preview:")
            lines.extend(preview_lines)

    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI args, run the local dry-run, and print a sanitized summary."""
    materialized_argv = list(sys.argv[1:] if argv is None else argv)

    forbidden = _find_forbidden_cli_arg(materialized_argv)
    if forbidden is not None:
        print(f"rejected CLI arg: {forbidden}", file=sys.stderr)
        return 2

    parser = build_arg_parser()
    args = parser.parse_args(materialized_argv)

    repo_root = Path.cwd() if args.repo_root is None else args.repo_root
    data_root = (
        repo_root.resolve().parent / "autostock-data"
        if args.data_root is None
        else args.data_root
    )

    try:
        result = run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            initial_cash_krw=args.initial_cash_krw,
            cash_min_weight=args.cash_min_weight,
            rolling_lookback_count=args.rolling_lookback_count,
            fee_bps=args.fee_bps,
            kr_sell_tax_bps=args.kr_sell_tax_bps,
            fx_spread_bps=args.fx_spread_bps,
        )
    except Exception as exc:  # noqa: BLE001 - operator CLI must surface failure safely
        print(f"local dry-run failed: {exc}", file=sys.stderr)
        return 1

    print(
        render_local_dry_run_summary(
            result,
            show_markdown_preview=args.show_markdown_preview,
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
