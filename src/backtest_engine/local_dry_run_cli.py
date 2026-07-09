"""Operator local monthly real-data evaluation dry-run CLI for Phase 2d-4/2d-5.

This module exposes a command-line entry point that calls
``run_local_monthly_evaluation_dry_run(...)`` and prints a sanitized summary to
stdout. Optional export writes sanitized evidence bundles outside the repository
through ``export_local_dry_run_evidence(...)``. It does not fetch or download
data, read CSVs directly, or produce investment conclusions.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from backtest_engine.local_evaluation import (
    LocalMonthlyEvaluationDryRunResult,
    resolve_local_rules_allocator_v2_state_policy,
    run_local_monthly_evaluation_dry_run,
)
from backtest_engine.local_run_config import (
    LOCAL_RULES_ALLOCATOR_VERSION_V1,
    LOCAL_RULES_ALLOCATOR_VERSION_V2,
)
from backtest_engine.local_evidence_export import export_local_dry_run_evidence

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
    parser.add_argument(
        "--export-output-root",
        type=Path,
        default=None,
        help=(
            "Repo-external directory for sanitized evidence export "
            "(opt-in; default writes no files)."
        ),
    )
    parser.add_argument(
        "--overwrite-export",
        action="store_true",
        help="Replace existing evidence export files in the output root.",
    )
    parser.add_argument(
        "--rules-allocator-version",
        choices=(
            LOCAL_RULES_ALLOCATOR_VERSION_V1,
            LOCAL_RULES_ALLOCATOR_VERSION_V2,
        ),
        default=LOCAL_RULES_ALLOCATOR_VERSION_V1,
        help=(
            "Local rules allocator version for the dry-run "
            f"(default: {LOCAL_RULES_ALLOCATOR_VERSION_V1})."
        ),
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
    static_result = result.static_neutral_baseline_result
    static_walk_forward = static_result.walk_forward_result
    static_metrics = static_result.benchmark_relative_result.metrics
    product_relative_result = result.product_relative_v1_neutral_baseline_result
    product_relative_walk_forward = product_relative_result.walk_forward_result
    product_relative_metrics = product_relative_result.benchmark_relative_result.metrics

    period_specs = run_config.period_specs
    nav_points = walk_forward.nav_points
    static_nav_points = static_walk_forward.nav_points
    product_relative_nav_points = product_relative_walk_forward.nav_points

    v2_state_policy = resolve_local_rules_allocator_v2_state_policy(
        run_config.rules_allocator_version,
    )

    lines = [
        "AutoStock local monthly evaluation dry-run",
        f"policy: {result.local_monthly_evaluation_dry_run_policy}",
        f"dataset_policy: {dataset.local_monthly_dataset_policy}",
        f"run_config_policy: {run_config.local_monthly_run_config_policy}",
        f"rules_allocator_version: {run_config.rules_allocator_version}",
        f"rules_allocator_v2_state_policy: {v2_state_policy}",
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
        (
            "static_neutral_baseline_policy: "
            f"{static_result.local_static_neutral_baseline_policy}"
        ),
        (
            "static_final_portfolio_value_krw: "
            f"{static_nav_points[-1].portfolio_value_krw}"
        ),
        f"static_total_cost_krw: {static_walk_forward.total_cost_krw}",
        (
            "static_terminal_strategy_return: "
            f"{static_metrics.bot_total_return_percent}"
        ),
        (
            "static_terminal_benchmark_return: "
            f"{static_metrics.benchmark_total_return_percent}"
        ),
        (
            "static_terminal_excess_return: "
            f"{static_metrics.excess_return_percent}"
        ),
        (
            "static_max_relative_drawdown: "
            f"{static_metrics.relative_drawdown_percent}"
        ),
        (
            "rules_minus_static_terminal_return: "
            f"{metrics.bot_total_return_percent - static_metrics.bot_total_return_percent}"
        ),
        (
            "rules_minus_static_excess_return: "
            f"{metrics.excess_return_percent - static_metrics.excess_return_percent}"
        ),
        (
            "product_relative_v1_neutral_baseline_policy: "
            f"{product_relative_result.local_static_neutral_baseline_policy}"
        ),
        (
            "product_relative_v1_final_portfolio_value_krw: "
            f"{product_relative_nav_points[-1].portfolio_value_krw}"
        ),
        (
            "product_relative_v1_total_cost_krw: "
            f"{product_relative_walk_forward.total_cost_krw}"
        ),
        (
            "product_relative_v1_terminal_strategy_return: "
            f"{product_relative_metrics.bot_total_return_percent}"
        ),
        (
            "product_relative_v1_terminal_benchmark_return: "
            f"{product_relative_metrics.benchmark_total_return_percent}"
        ),
        (
            "product_relative_v1_terminal_excess_return: "
            f"{product_relative_metrics.excess_return_percent}"
        ),
        (
            "product_relative_v1_max_relative_drawdown: "
            f"{product_relative_metrics.relative_drawdown_percent}"
        ),
        (
            "rules_minus_product_relative_v1_terminal_return: "
            f"{metrics.bot_total_return_percent - product_relative_metrics.bot_total_return_percent}"
        ),
        (
            "rules_minus_product_relative_v1_excess_return: "
            f"{metrics.excess_return_percent - product_relative_metrics.excess_return_percent}"
        ),
        (
            "static_minus_product_relative_v1_terminal_return: "
            f"{static_metrics.bot_total_return_percent - product_relative_metrics.bot_total_return_percent}"
        ),
        (
            "static_minus_product_relative_v1_excess_return: "
            f"{static_metrics.excess_return_percent - product_relative_metrics.excess_return_percent}"
        ),
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
            rules_allocator_version=args.rules_allocator_version,
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

    if args.export_output_root is not None:
        try:
            export_result = export_local_dry_run_evidence(
                repo_root=repo_root,
                result=result,
                output_root=args.export_output_root,
                overwrite=args.overwrite_export,
            )
        except Exception as exc:  # noqa: BLE001 - operator CLI must surface failure safely
            print(f"evidence export failed: {exc}", file=sys.stderr)
            return 1

        print("evidence_exported: true")
        print(f"summary_markdown_path: {export_result.summary_markdown_path}")
        print(f"metrics_json_path: {export_result.metrics_json_path}")
        print(f"manifest_json_path: {export_result.manifest_json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
