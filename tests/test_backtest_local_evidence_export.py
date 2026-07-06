from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

sys_path_root = Path(__file__).resolve().parents[1] / "src"
import sys

sys.path.insert(0, str(sys_path_root))

from backtest_engine.local_dataset import (  # noqa: E402
    default_local_monthly_benchmark_spec,
    default_local_monthly_instrument_specs_for_kospi_primary,
)
from backtest_engine.local_dry_run_cli import render_local_dry_run_summary  # noqa: E402
from backtest_engine.local_evaluation import (  # noqa: E402
    LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1,
    run_local_monthly_evaluation_dry_run,
)
from backtest_engine.local_evidence_export import (  # noqa: E402
    LOCAL_EVIDENCE_EXPORT_POLICY_V1,
    MANIFEST_JSON_FILENAME,
    METRICS_JSON_FILENAME,
    SUMMARY_MARKDOWN_FILENAME,
    LocalEvidenceExportResult,
    export_local_dry_run_evidence,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "backtest_engine"
    / "local_evidence_export.py"
)

REQUIRED_COLUMNS = (
    "date",
    "as_of",
    "symbol",
    "market",
    "close_adjusted",
    "source_name",
)
HEADER = ",".join(REQUIRED_COLUMNS)

FORBIDDEN_TEXT_TOKENS = (
    "pandas",
    "read_csv",
    "to_csv",
    "yfinance",
    "fred",
    "requests",
    "httpx",
    "urllib",
    "socket",
    "websocket",
    "websockets",
    "aiohttp",
    "SQLiteDateIdSourceStore",
    ".save_record(",
    "config.toml",
    "config/config.toml",
    "uv run",
    "subprocess",
    "os.system",
    "ScoutInputBuilder",
    "AllocatorDecision",
    "AllocationRegime",
    "run_explicit_synthetic_backtest_evaluation_pipeline",
    "run_explicit_schedule_rules_walk_forward_nav",
    "compute_walk_forward_benchmark_relative_metrics",
    "render_backtest_evaluation_report_bundle",
    "recommendation",
    "investment advice",
    "beats S&P",
    "beat S&P",
)

FORBIDDEN_IMPORT_ROOTS = {
    "yfinance",
    "fred",
    "requests",
    "httpx",
    "urllib",
    "socket",
    "websocket",
    "websockets",
    "aiohttp",
    "scout",
    "allocator",
    "risk",
    "broker",
    "orders",
    "emergency",
    "composition",
}

SECRET_PLACEHOLDERS = (
    "appkey",
    "appsecret",
    "approval_key",
    "account",
)


def _write_csv(path: Path, rows: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "AutoStock"
    data_root = tmp_path / "autostock-data"
    repo_root.mkdir(exist_ok=True)
    data_root.mkdir(exist_ok=True)
    return repo_root, data_root


def _write_default_kospi_primary_csvs(
    data_root: Path,
    *,
    periods: tuple[str, ...] = (
        "2020-01",
        "2020-02",
        "2020-03",
        "2020-04",
        "2020-05",
    ),
) -> None:
    specs = default_local_monthly_instrument_specs_for_kospi_primary()
    benchmark = default_local_monthly_benchmark_spec()

    for spec in specs:
        rows: list[str] = []
        for index, period in enumerate(periods):
            year, month = period.split("-")
            day = "31" if month in {"01", "03", "05", "07", "08", "10", "12"} else "29"
            close = Decimal("100") + Decimal(index)
            as_of_month = int(month) + 1
            as_of_year = int(year)
            if as_of_month > 12:
                as_of_month = 1
                as_of_year += 1
            rows.append(
                f"{year}-{month}-{day},{as_of_year:04d}-{as_of_month:02d}-01T00:00:00+00:00,"
                f"{spec.symbol},{spec.market},{close},synthetic"
            )
        _write_csv(data_root / spec.relative_path, tuple(rows))

    sp_rows: list[str] = []
    fx_rows: list[str] = []
    for index, period in enumerate(periods):
        year, month = period.split("-")
        day = "31" if month in {"01", "03", "05", "07", "08", "10", "12"} else "29"
        as_of_month = int(month) + 1
        as_of_year = int(year)
        if as_of_month > 12:
            as_of_month = 1
            as_of_year += 1
        sp_rows.append(
            f"{year}-{month}-{day},{as_of_year:04d}-{as_of_month:02d}-01T00:00:00+00:00,"
            f"SP500TR,US,{100 + index},synthetic"
        )
        fx_rows.append(
            f"{year}-{month}-{day},{as_of_year:04d}-{as_of_month:02d}-01T00:00:00+00:00,"
            f"USDKRW,FX,{1300 + index},synthetic"
        )
    _write_csv(data_root / benchmark.sp500tr_relative_path, tuple(sp_rows))
    _write_csv(data_root / benchmark.usdkrw_relative_path, tuple(fx_rows))


def _prepare_default_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo_root, data_root = _layout(tmp_path)
    _write_default_kospi_primary_csvs(data_root)
    output_root = tmp_path / "autostock-data" / "outputs"
    return repo_root, data_root, output_root


def _run_dry_run(tmp_path: Path):
    repo_root, data_root, _ = _prepare_default_layout(tmp_path)
    return run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )


def _export_default(tmp_path: Path, *, overwrite: bool = False):
    repo_root, _, output_root = _prepare_default_layout(tmp_path)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=repo_root.parent / "autostock-data",
    )
    return export_local_dry_run_evidence(
        repo_root=repo_root,
        result=result,
        output_root=output_root,
        overwrite=overwrite,
    )


def _all_output_text(output_root: Path) -> str:
    return "\n".join(
        (output_root / filename).read_text(encoding="utf-8")
        for filename in (
            SUMMARY_MARKDOWN_FILENAME,
            METRICS_JSON_FILENAME,
            MANIFEST_JSON_FILENAME,
        )
    )


def test_default_output_root_resolves_to_sibling_autostock_data_outputs(
    tmp_path: Path,
) -> None:
    repo_root, data_root, _ = _prepare_default_layout(tmp_path)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    export_result = export_local_dry_run_evidence(
        repo_root=repo_root,
        result=result,
    )
    expected = (repo_root.parent / "autostock-data" / "outputs").resolve()
    assert Path(export_result.output_root) == expected


def test_rejects_output_root_inside_repo_root(tmp_path: Path) -> None:
    repo_root, _, _ = _prepare_default_layout(tmp_path)
    result = _run_dry_run(tmp_path)
    inside_repo_output = repo_root / "outputs"
    with pytest.raises(ValueError, match="output_root must not be inside repo_root"):
        export_local_dry_run_evidence(
            repo_root=repo_root,
            result=result,
            output_root=inside_repo_output,
        )


def test_rejects_repo_root_inside_output_root(tmp_path: Path) -> None:
    repo_root, _, _ = _prepare_default_layout(tmp_path)
    result = _run_dry_run(tmp_path)
    parent_output = repo_root.parent
    with pytest.raises(ValueError, match="repo_root must not be inside output_root"):
        export_local_dry_run_evidence(
            repo_root=repo_root,
            result=result,
            output_root=parent_output,
        )


def test_writes_exactly_three_files(tmp_path: Path) -> None:
    repo_root, _, output_root = _prepare_default_layout(tmp_path)
    _export_default(tmp_path)
    output_files = sorted(path.name for path in output_root.iterdir() if path.is_file())
    assert output_files == [
        MANIFEST_JSON_FILENAME,
        METRICS_JSON_FILENAME,
        SUMMARY_MARKDOWN_FILENAME,
    ]


def test_refuses_overwrite_by_default(tmp_path: Path) -> None:
    _export_default(tmp_path)
    with pytest.raises(FileExistsError):
        _export_default(tmp_path)


def test_allows_overwrite_when_enabled(tmp_path: Path) -> None:
    first = _export_default(tmp_path)
    second = _export_default(tmp_path, overwrite=True)
    assert first.summary_markdown_path == second.summary_markdown_path
    assert first.metrics_json_path == second.metrics_json_path
    assert first.manifest_json_path == second.manifest_json_path


def test_summary_markdown_contains_sanitized_summary_and_report_section(
    tmp_path: Path,
) -> None:
    repo_root, data_root, output_root = _prepare_default_layout(tmp_path)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    export_local_dry_run_evidence(
        repo_root=repo_root,
        result=result,
        output_root=output_root,
    )
    summary_text = (output_root / SUMMARY_MARKDOWN_FILENAME).read_text(encoding="utf-8")
    sanitized_summary = render_local_dry_run_summary(result)
    assert sanitized_summary.strip() in summary_text
    assert "## Sanitized Summary" in summary_text
    assert "## In-Memory Markdown Report" in summary_text
    assert result.report_bundle.markdown_report.strip() in summary_text


def test_metrics_json_contains_required_fields(tmp_path: Path) -> None:
    repo_root, data_root, output_root = _prepare_default_layout(tmp_path)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    export_local_dry_run_evidence(
        repo_root=repo_root,
        result=result,
        output_root=output_root,
    )
    metrics = json.loads(
        (output_root / METRICS_JSON_FILENAME).read_text(encoding="utf-8")
    )
    assert metrics["local_evidence_export_policy"] == LOCAL_EVIDENCE_EXPORT_POLICY_V1
    assert (
        metrics["local_monthly_evaluation_dry_run_policy"]
        == LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1
    )
    assert metrics["dataset_policy"] == result.dataset.local_monthly_dataset_policy
    assert metrics["run_config_policy"] == result.run_config.local_monthly_run_config_policy
    assert metrics["period_count"] == len(result.run_config.period_specs)
    assert metrics["nav_point_count"] == len(result.walk_forward_result.nav_points)
    assert metrics["benchmark_point_count"] == len(result.dataset.benchmark_points)
    assert metrics["common_period_count"] == len(result.dataset.common_periods)
    assert metrics["first_common_period"] == result.dataset.common_periods[0]
    assert metrics["last_common_period"] == result.dataset.common_periods[-1]
    assert metrics["first_execution_as_of"] == (
        result.run_config.period_specs[0].intended_execution_time.isoformat()
    )
    assert metrics["last_execution_as_of"] == (
        result.run_config.period_specs[-1].intended_execution_time.isoformat()
    )
    assert metrics["final_portfolio_value_krw"] == str(
        result.walk_forward_result.nav_points[-1].portfolio_value_krw
    )
    assert metrics["total_cost_krw"] == str(result.walk_forward_result.total_cost_krw)
    metrics_obj = result.benchmark_relative_result.metrics
    assert metrics["terminal_strategy_return"] == str(metrics_obj.bot_total_return_percent)
    assert metrics["terminal_benchmark_return"] == str(
        metrics_obj.benchmark_total_return_percent
    )
    assert metrics["terminal_excess_return"] == str(metrics_obj.excess_return_percent)
    assert metrics["max_relative_drawdown"] == str(metrics_obj.relative_drawdown_percent)
    assert metrics["warnings"] == list(result.warnings)


def test_manifest_json_contains_export_policy_and_generated_from_policies(
    tmp_path: Path,
) -> None:
    repo_root, data_root, output_root = _prepare_default_layout(tmp_path)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    export_local_dry_run_evidence(
        repo_root=repo_root,
        result=result,
        output_root=output_root,
    )
    manifest = json.loads(
        (output_root / MANIFEST_JSON_FILENAME).read_text(encoding="utf-8")
    )
    assert manifest["local_evidence_export_policy"] == LOCAL_EVIDENCE_EXPORT_POLICY_V1
    assert manifest["output_filenames"]["summary_markdown"] == SUMMARY_MARKDOWN_FILENAME
    assert manifest["output_filenames"]["metrics_json"] == METRICS_JSON_FILENAME
    assert manifest["output_filenames"]["manifest_json"] == MANIFEST_JSON_FILENAME
    generated_from = manifest["generated_from"]
    assert (
        generated_from["local_monthly_evaluation_dry_run_policy"]
        == result.local_monthly_evaluation_dry_run_policy
    )
    assert (
        generated_from["local_monthly_dataset_policy"]
        == result.dataset.local_monthly_dataset_policy
    )
    assert (
        generated_from["local_monthly_run_config_policy"]
        == result.run_config.local_monthly_run_config_policy
    )
    assert (
        generated_from["report_bundle_policy"]
        == result.report_bundle.report_bundle_policy
    )
    assert (
        generated_from["benchmark_adapter_policy"]
        == result.benchmark_relative_result.benchmark_adapter_policy
    )
    assert manifest["statement"] == "research evidence only; not an investment conclusion"


@pytest.mark.parametrize(
    "artifact_builder",
    [
        lambda tmp_path: _all_output_text(tmp_path / "autostock-data" / "outputs"),
        lambda tmp_path: (
            tmp_path / "autostock-data" / "outputs" / SUMMARY_MARKDOWN_FILENAME
        ).read_text(encoding="utf-8"),
        lambda tmp_path: (
            tmp_path / "autostock-data" / "outputs" / METRICS_JSON_FILENAME
        ).read_text(encoding="utf-8"),
        lambda tmp_path: (
            tmp_path / "autostock-data" / "outputs" / MANIFEST_JSON_FILENAME
        ).read_text(encoding="utf-8"),
    ],
)
def test_output_files_do_not_contain_raw_csv_rows(
    tmp_path: Path,
    artifact_builder,
) -> None:
    repo_root, data_root, _ = _prepare_default_layout(tmp_path)
    csv_text = (data_root / "monthly/sp500tr_monthly.csv").read_text(encoding="utf-8")
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    export_local_dry_run_evidence(
        repo_root=repo_root,
        result=result,
        output_root=tmp_path / "autostock-data" / "outputs",
    )
    artifact_text = artifact_builder(tmp_path)
    for line in csv_text.splitlines()[1:]:
        assert line not in artifact_text


def test_output_files_do_not_contain_raw_source_records(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    _export_default(tmp_path)
    artifact_text = _all_output_text(tmp_path / "autostock-data" / "outputs")
    for record in result.dataset.source_records:
        assert record.date_id.value not in artifact_text
        assert record.summary not in artifact_text
        assert json.dumps(record.payload, sort_keys=True) not in artifact_text


def test_output_files_do_not_contain_config_toml(tmp_path: Path) -> None:
    _export_default(tmp_path)
    artifact_text = _all_output_text(tmp_path / "autostock-data" / "outputs")
    assert "config.toml" not in artifact_text
    assert "config/config.toml" not in artifact_text


@pytest.mark.parametrize("placeholder", SECRET_PLACEHOLDERS)
def test_output_files_do_not_contain_secret_placeholders(
    tmp_path: Path,
    placeholder: str,
) -> None:
    _export_default(tmp_path)
    artifact_text = _all_output_text(tmp_path / "autostock-data" / "outputs").lower()
    assert placeholder not in artifact_text


def test_output_files_do_not_contain_investment_advice(tmp_path: Path) -> None:
    _export_default(tmp_path)
    artifact_text = _all_output_text(tmp_path / "autostock-data" / "outputs").lower()
    assert "investment advice" not in artifact_text


def test_output_files_do_not_contain_beats_sp_claim(tmp_path: Path) -> None:
    _export_default(tmp_path)
    artifact_text = _all_output_text(tmp_path / "autostock-data" / "outputs")
    assert "beats S&P" not in artifact_text
    assert "beat S&P" not in artifact_text


def test_accepts_already_computed_result_without_rerunning_dry_run(
    tmp_path: Path,
) -> None:
    repo_root, data_root, output_root = _prepare_default_layout(tmp_path)
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    with patch(
        "backtest_engine.local_evaluation.run_local_monthly_evaluation_dry_run"
    ) as mocked:
        export_local_dry_run_evidence(
            repo_root=repo_root,
            result=result,
            output_root=output_root,
        )
    mocked.assert_not_called()


def test_does_not_call_walk_forward_directly() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "run_explicit_schedule_rules_walk_forward_nav" not in text


def test_does_not_call_synthetic_pipeline_wrapper() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "run_explicit_synthetic_backtest_evaluation_pipeline" not in text


def test_result_model_is_frozen_and_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        LocalEvidenceExportResult.model_validate(
            {
                "local_evidence_export_policy": LOCAL_EVIDENCE_EXPORT_POLICY_V1,
                "output_root": "/tmp/outputs",
                "summary_markdown_path": "/tmp/outputs/summary.backtest.md",
                "metrics_json_path": "/tmp/outputs/metrics.backtest.json",
                "manifest_json_path": "/tmp/outputs/manifest.backtest.json",
                "warnings": (),
                "extra_field": "forbidden",
            }
        )


def test_result_has_no_forbidden_path_or_conclusion_fields() -> None:
    forbidden_fields = {
        "raw_csv_path",
        "source_records_path",
        "trades_path",
        "config_path",
        "secret_path",
        "recommendation",
        "investment_advice",
        "project_conclusion",
    }
    model_fields = set(LocalEvidenceExportResult.model_fields)
    assert model_fields.isdisjoint(forbidden_fields)


def test_module_static_scan_rejects_forbidden_tokens() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    for token in FORBIDDEN_TEXT_TOKENS:
        assert token not in text, f"forbidden token present: {token}"


def test_module_does_not_import_network_fetch_or_runtime_packages() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(FORBIDDEN_IMPORT_ROOTS)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in FORBIDDEN_IMPORT_ROOTS


def test_module_allows_expected_dependencies() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "import json" in text
    assert ".write_text(" in text
    assert "render_local_dry_run_summary" in text
    assert "LocalMonthlyEvaluationDryRunResult" in text
