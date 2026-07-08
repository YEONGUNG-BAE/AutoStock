"""Sanitized local dry-run evidence export for Phase 2d-5.

This module writes opt-in sanitized evidence bundles to a repo-external output
directory. It accepts an already-computed ``LocalMonthlyEvaluationDryRunResult``,
does not rerun the dry-run, fetch data, or produce investment conclusions.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from backtest_engine.local_evaluation import LocalMonthlyEvaluationDryRunResult

LOCAL_EVIDENCE_EXPORT_POLICY_V1 = "sanitized_local_dry_run_evidence_export.v1"

SUMMARY_MARKDOWN_FILENAME = "local_dry_run_summary.backtest.md"
METRICS_JSON_FILENAME = "local_dry_run_metrics.backtest.json"
MANIFEST_JSON_FILENAME = "local_dry_run_manifest.backtest.json"

_EVIDENCE_OUTPUT_FILENAMES = (
    SUMMARY_MARKDOWN_FILENAME,
    METRICS_JSON_FILENAME,
    MANIFEST_JSON_FILENAME,
)

_RESEARCH_ONLY_STATEMENT = "research evidence only; not an investment conclusion"


class LocalEvidenceExportResult(BaseModel):
    """Immutable sanitized local dry-run evidence export result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_evidence_export_policy: Literal[
        "sanitized_local_dry_run_evidence_export.v1"
    ]
    output_root: str
    summary_markdown_path: str
    metrics_json_path: str
    manifest_json_path: str
    warnings: tuple[str, ...]

    @field_validator(
        "output_root",
        "summary_markdown_path",
        "metrics_json_path",
        "manifest_json_path",
    )
    @classmethod
    def validate_non_empty_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path fields must be non-empty strings.")
        return value


def export_local_dry_run_evidence(
    *,
    repo_root: Path,
    result: LocalMonthlyEvaluationDryRunResult,
    output_root: Path | None = None,
    overwrite: bool = False,
) -> LocalEvidenceExportResult:
    """Write a sanitized local dry-run evidence bundle outside the repository."""
    resolved_repo_root = repo_root.resolve()
    resolved_output_root = _resolve_output_root(
        repo_root=resolved_repo_root,
        output_root=output_root,
    )

    summary_path = resolved_output_root / SUMMARY_MARKDOWN_FILENAME
    metrics_path = resolved_output_root / METRICS_JSON_FILENAME
    manifest_path = resolved_output_root / MANIFEST_JSON_FILENAME
    output_paths = (summary_path, metrics_path, manifest_path)

    existing_paths = [path for path in output_paths if path.exists()]
    if existing_paths and not overwrite:
        existing_names = ", ".join(path.name for path in existing_paths)
        raise FileExistsError(
            f"evidence output files already exist: {existing_names}"
        )

    summary_markdown = _render_export_summary_markdown(result)
    metrics_payload = _build_metrics_payload(result)
    manifest_payload = _build_manifest_payload(result)

    summary_path.write_text(summary_markdown, encoding="utf-8")
    metrics_path.write_text(
        json.dumps(metrics_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return LocalEvidenceExportResult(
        local_evidence_export_policy=LOCAL_EVIDENCE_EXPORT_POLICY_V1,
        output_root=str(resolved_output_root),
        summary_markdown_path=str(summary_path),
        metrics_json_path=str(metrics_path),
        manifest_json_path=str(manifest_path),
        warnings=result.warnings,
    )


def _resolve_output_root(
    *,
    repo_root: Path,
    output_root: Path | None,
) -> Path:
    materialized_output_root = (
        repo_root.parent / "autostock-data" / "outputs"
        if output_root is None
        else output_root
    )
    resolved_output_root = materialized_output_root.resolve()

    if _path_is_relative_to(resolved_output_root, repo_root):
        raise ValueError("output_root must not be inside repo_root.")
    if _path_is_relative_to(repo_root, resolved_output_root):
        raise ValueError("repo_root must not be inside output_root.")

    resolved_output_root.mkdir(parents=True, exist_ok=True)
    return resolved_output_root


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _render_export_summary_markdown(
    result: LocalMonthlyEvaluationDryRunResult,
) -> str:
    from backtest_engine.local_dry_run_cli import render_local_dry_run_summary

    sanitized_summary = render_local_dry_run_summary(result)
    markdown_report = result.report_bundle.markdown_report.strip()

    sections = [
        "# AutoStock Local Dry-Run Evidence Export",
        "",
        f"policy: {LOCAL_EVIDENCE_EXPORT_POLICY_V1}",
        "",
        "## Sanitized Summary",
        "",
        sanitized_summary.rstrip(),
        "",
        "## In-Memory Markdown Report",
        "",
        (
            "The section below reproduces the in-memory markdown report only. "
            f"{_RESEARCH_ONLY_STATEMENT}."
        ),
        "",
        markdown_report,
        "",
    ]
    return "\n".join(sections)


def _build_metrics_payload(
    result: LocalMonthlyEvaluationDryRunResult,
) -> dict[str, object]:
    dataset = result.dataset
    run_config = result.run_config
    walk_forward = result.walk_forward_result
    metrics = result.benchmark_relative_result.metrics
    static_result = result.static_neutral_baseline_result
    static_walk_forward = static_result.walk_forward_result
    static_metrics = static_result.benchmark_relative_result.metrics
    period_specs = run_config.period_specs
    nav_points = walk_forward.nav_points
    static_nav_points = static_walk_forward.nav_points

    return {
        "local_evidence_export_policy": LOCAL_EVIDENCE_EXPORT_POLICY_V1,
        "local_monthly_evaluation_dry_run_policy": (
            result.local_monthly_evaluation_dry_run_policy
        ),
        "dataset_policy": dataset.local_monthly_dataset_policy,
        "run_config_policy": run_config.local_monthly_run_config_policy,
        "period_count": len(period_specs),
        "nav_point_count": len(nav_points),
        "benchmark_point_count": len(dataset.benchmark_points),
        "common_period_count": len(dataset.common_periods),
        "first_common_period": dataset.common_periods[0],
        "last_common_period": dataset.common_periods[-1],
        "first_execution_as_of": period_specs[0].intended_execution_time.isoformat(),
        "last_execution_as_of": period_specs[-1].intended_execution_time.isoformat(),
        "final_portfolio_value_krw": _decimal_to_json(nav_points[-1].portfolio_value_krw),
        "total_cost_krw": _decimal_to_json(walk_forward.total_cost_krw),
        "terminal_strategy_return": _decimal_to_json(metrics.bot_total_return_percent),
        "terminal_benchmark_return": _decimal_to_json(
            metrics.benchmark_total_return_percent
        ),
        "terminal_excess_return": _decimal_to_json(metrics.excess_return_percent),
        "max_relative_drawdown": _decimal_to_json(metrics.relative_drawdown_percent),
        "static_neutral_baseline_policy": (
            static_result.local_static_neutral_baseline_policy
        ),
        "static_terminal_strategy_return": _decimal_to_json(
            static_metrics.bot_total_return_percent
        ),
        "static_terminal_benchmark_return": _decimal_to_json(
            static_metrics.benchmark_total_return_percent
        ),
        "static_terminal_excess_return": _decimal_to_json(
            static_metrics.excess_return_percent
        ),
        "static_max_relative_drawdown": _decimal_to_json(
            static_metrics.relative_drawdown_percent
        ),
        "static_final_portfolio_value_krw": _decimal_to_json(
            static_nav_points[-1].portfolio_value_krw
        ),
        "static_total_cost_krw": _decimal_to_json(static_walk_forward.total_cost_krw),
        "rules_minus_static_terminal_return": _decimal_to_json(
            metrics.bot_total_return_percent - static_metrics.bot_total_return_percent
        ),
        "rules_minus_static_excess_return": _decimal_to_json(
            metrics.excess_return_percent - static_metrics.excess_return_percent
        ),
        "warnings": list(result.warnings),
    }


def _build_manifest_payload(
    result: LocalMonthlyEvaluationDryRunResult,
) -> dict[str, object]:
    return {
        "local_evidence_export_policy": LOCAL_EVIDENCE_EXPORT_POLICY_V1,
        "output_filenames": {
            "summary_markdown": SUMMARY_MARKDOWN_FILENAME,
            "metrics_json": METRICS_JSON_FILENAME,
            "manifest_json": MANIFEST_JSON_FILENAME,
        },
        "generated_from": {
            "local_monthly_evaluation_dry_run_policy": (
                result.local_monthly_evaluation_dry_run_policy
            ),
            "local_monthly_dataset_policy": result.dataset.local_monthly_dataset_policy,
            "local_monthly_run_config_policy": (
                result.run_config.local_monthly_run_config_policy
            ),
            "report_bundle_policy": result.report_bundle.report_bundle_policy,
            "benchmark_adapter_policy": (
                result.benchmark_relative_result.benchmark_adapter_policy
            ),
            "static_neutral_baseline_policy": (
                result.static_neutral_baseline_result.local_static_neutral_baseline_policy
            ),
            "static_benchmark_adapter_policy": (
                result.static_neutral_baseline_result.benchmark_relative_result.benchmark_adapter_policy
            ),
        },
        "warnings": list(result.warnings),
        "statement": _RESEARCH_ONLY_STATEMENT,
    }


def _decimal_to_json(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value)
