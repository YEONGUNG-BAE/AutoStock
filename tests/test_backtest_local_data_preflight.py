from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine.local_data_preflight import (  # noqa: E402
    LOCAL_DATA_PREFLIGHT_POLICY_V1,
    LocalDataFilePreflightResult,
    LocalDataFileSpec,
    LocalDataPreflightResult,
    default_monthly_local_data_file_specs,
    run_local_data_preflight,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "backtest_engine"
    / "local_data_preflight.py"
)

REQUIRED_COLUMNS = (
    "date",
    "as_of",
    "symbol",
    "market",
    "close_adjusted",
    "source_name",
)

FOCUSED_TEST_FILES = (
    "tests/test_backtest_local_data_preflight.py",
    "tests/test_backtest_evaluation_pipeline.py",
    "tests/test_backtest_report_bundle.py",
    "tests/test_backtest_benchmark_adapter.py",
    "tests/test_backtest_walk_forward.py",
    "tests/test_backtest_period_step.py",
    "tests/test_backtest_rebalance.py",
    "tests/test_backtest_execution_prices.py",
    "tests/test_backtest_single_step_decision.py",
    "tests/test_backtest_observation_spacing.py",
    "tests/test_backtest_rolling_features.py",
    "tests/test_backtest_snapshot_builder.py",
    "tests/test_backtest_step_contract.py",
    "tests/test_rules_allocator.py",
    "tests/test_paper_review_benchmark_relative_metrics.py",
    "tests/test_paper_review_benchmark_relative_report.py",
    "tests/test_backtest_data_loader.py",
    "tests/test_backtest_asof_guard.py",
    "tests/test_backtest_source_record_conversion.py",
    "tests/test_scout_input_builder.py",
    "tests/test_backtest_design_freeze_docs.py",
)


def _spec(
    *,
    logical_name: str = "sample_monthly",
    relative_path: str = "monthly/sample_monthly.csv",
    required_columns: tuple[str, ...] = REQUIRED_COLUMNS,
    expected_symbol: str | None = "SAMPLE",
    expected_market: str | None = "US",
) -> LocalDataFileSpec:
    return LocalDataFileSpec(
        logical_name=logical_name,
        relative_path=relative_path,
        required_columns=required_columns,
        expected_symbol=expected_symbol,
        expected_market=expected_market,
    )


def _write_csv(path: Path, header: str, rows: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "AutoStock"
    data_root = tmp_path / "autostock-data"
    repo_root.mkdir()
    data_root.mkdir()
    return repo_root, data_root


def test_default_data_root_resolves_to_sibling_autostock_data(tmp_path: Path) -> None:
    repo_root, _ = _layout(tmp_path)

    result = run_local_data_preflight(
        repo_root=repo_root,
        file_specs=(_spec(),),
    )

    assert result.data_root == str((repo_root.parent / "autostock-data").resolve())


def test_rejects_data_root_inside_repo_root(tmp_path: Path) -> None:
    repo_root, _ = _layout(tmp_path)
    nested_data_root = repo_root / "nested-data"

    with pytest.raises(ValueError, match="data_root must not be inside repo_root"):
        run_local_data_preflight(
            repo_root=repo_root,
            data_root=nested_data_root,
            file_specs=(_spec(),),
        )


def test_rejects_repo_root_inside_data_root(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    nested_repo_root = data_root / "AutoStock"

    with pytest.raises(ValueError, match="repo_root must not be inside data_root"):
        run_local_data_preflight(
            repo_root=nested_repo_root,
            data_root=data_root,
            file_specs=(_spec(),),
        )


def test_rejects_absolute_file_spec_relative_path() -> None:
    with pytest.raises(ValidationError, match="relative_path must be relative"):
        LocalDataFileSpec(
            logical_name="sample",
            relative_path="/absolute/path.csv",
            required_columns=REQUIRED_COLUMNS,
        )


def test_rejects_parent_traversal_in_file_spec_relative_path() -> None:
    with pytest.raises(ValidationError, match="relative_path must not contain"):
        LocalDataFileSpec(
            logical_name="sample",
            relative_path="../escape.csv",
            required_columns=REQUIRED_COLUMNS,
        )


def test_missing_file_returns_exists_false_with_warning(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    file_result = result.files[0]
    assert file_result.exists is False
    assert file_result.row_count == 0
    assert "file missing" in file_result.warnings[0]
    assert any("file missing" in warning for warning in result.warnings)


def test_present_csv_header_is_read(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100.0,synthetic",
        ),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    file_result = result.files[0]
    assert file_result.exists is True
    assert file_result.columns == REQUIRED_COLUMNS


def test_required_missing_columns_are_detected(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,symbol,market",
        ("2020-01-31,SAMPLE,US",),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    file_result = result.files[0]
    assert "as_of" in file_result.missing_columns
    assert "close_adjusted" in file_result.missing_columns
    assert any("missing required columns" in warning for warning in file_result.warnings)


def test_row_count_is_computed(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100.0,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,SAMPLE,US,101.0,synthetic",
            "2020-03-31,2020-04-01T00:00:00+00:00,SAMPLE,US,102.0,synthetic",
        ),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    assert result.files[0].row_count == 3


def test_first_and_last_monthly_period_are_computed(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        (
            "2020-03-31,2020-04-01T00:00:00+00:00,SAMPLE,US,102.0,synthetic",
            "2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100.0,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,SAMPLE,US,101.0,synthetic",
        ),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    file_result = result.files[0]
    assert file_result.first_period == "2020-01"
    assert file_result.last_period == "2020-03"


def test_missing_monthly_periods_are_detected(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100.0,synthetic",
            "2020-03-31,2020-04-01T00:00:00+00:00,SAMPLE,US,102.0,synthetic",
        ),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    assert result.files[0].missing_periods == ("2020-02",)


def test_duplicate_monthly_periods_are_counted(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100.0,synthetic",
            "2020-01-15,2020-02-01T00:00:00+00:00,SAMPLE,US,100.5,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,SAMPLE,US,101.0,synthetic",
        ),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    assert result.files[0].duplicate_period_count == 1


def test_symbol_values_are_collected(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100.0,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,ALT,US,101.0,synthetic",
        ),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(expected_symbol=None),),
    )

    assert result.files[0].symbol_values == ("ALT", "SAMPLE")


def test_market_values_are_collected(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100.0,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,SAMPLE,KR,101.0,synthetic",
        ),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(expected_market=None),),
    )

    assert result.files[0].market_values == ("KR", "US")


def test_expected_symbol_mismatch_warns(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        ("2020-01-31,2020-02-01T00:00:00+00:00,OTHER,US,100.0,synthetic",),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(expected_symbol="SAMPLE"),),
    )

    assert any("expected symbol mismatch" in warning for warning in result.files[0].warnings)


def test_expected_market_mismatch_warns(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        ("2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,KR,100.0,synthetic",),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(expected_market="US"),),
    )

    assert any("expected market mismatch" in warning for warning in result.files[0].warnings)


def test_first_day_of_month_date_values_warn(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        (
            "2020-01-01,2020-02-01T00:00:00+00:00,SAMPLE,US,100.0,synthetic",
            "2020-02-01,2020-03-01T00:00:00+00:00,SAMPLE,US,101.0,synthetic",
        ),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    assert any("first day of month" in warning for warning in result.files[0].warnings)


def test_naive_or_unparseable_as_of_warns(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        (
            "2020-01-31,2020-02-01 00:00:00,SAMPLE,US,100.0,synthetic",
            "2020-02-29,not-a-timestamp,SAMPLE,US,101.0,synthetic",
        ),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    assert any("naive or unparseable" in warning for warning in result.files[0].warnings)


def test_suspicious_as_of_date_relationship_warns(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        (
            "2020-03-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100.0,synthetic",
        ),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    assert any("appears before corresponding date" in warning for warning in result.files[0].warnings)


def test_result_stores_metadata_only_not_price_values(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        "date,as_of,symbol,market,close_adjusted,source_name",
        ("2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,12345.67,synthetic",),
    )

    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    serialized = result.model_dump_json()
    assert "12345.67" not in serialized


def test_result_has_no_nav_fields() -> None:
    result_fields = set(LocalDataPreflightResult.model_fields)
    file_fields = set(LocalDataFilePreflightResult.model_fields)
    forbidden = {
        "nav",
        "nav_points",
        "portfolio_value_krw",
        "cash_krw",
        "total_nav_krw",
    }
    assert result_fields.isdisjoint(forbidden)
    assert file_fields.isdisjoint(forbidden)


def test_result_has_no_benchmark_relative_fields() -> None:
    result_fields = set(LocalDataPreflightResult.model_fields)
    file_fields = set(LocalDataFilePreflightResult.model_fields)
    forbidden = {
        "benchmark_relative",
        "benchmark_points",
        "metrics",
        "alpha",
        "beta",
        "tracking_error",
        "information_ratio",
    }
    assert result_fields.isdisjoint(forbidden)
    assert file_fields.isdisjoint(forbidden)


def test_result_has_no_markdown_report_fields() -> None:
    result_fields = set(LocalDataPreflightResult.model_fields)
    forbidden = {
        "markdown_report",
        "report_bundle",
        "report_markdown",
        "rendered_report",
    }
    assert result_fields.isdisjoint(forbidden)


def test_result_has_no_investment_conclusion_fields() -> None:
    result_fields = set(LocalDataPreflightResult.model_fields)
    forbidden = {
        "recommendation",
        "recommendations",
        "investment_advice",
        "conclusion",
        "project_conclusion",
    }
    assert result_fields.isdisjoint(forbidden)


def test_uses_stdlib_csv_not_pandas() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)

    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.split(".")[0])

    assert "csv" in imported_roots
    assert "pandas" not in imported_roots


def test_module_does_not_import_yfinance_fred_or_network_libraries() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)

    forbidden_roots = {
        "pandas",
        "yfinance",
        "fred",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "websocket",
        "websockets",
        "aiohttp",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_roots


def test_module_does_not_call_walk_forward_or_evaluation_pipeline() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "run_explicit_schedule_rules_walk_forward_nav",
        "run_explicit_synthetic_backtest_evaluation_pipeline",
        "compute_walk_forward_benchmark_relative_metrics",
        "render_backtest_evaluation_report_bundle",
    )
    for token in forbidden:
        assert token not in text


def test_module_does_not_write_files() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        ".write(",
        "to_csv",
        "open(",
    )
    for token in forbidden:
        assert token not in text


def test_module_does_not_import_forbidden_runtime_packages() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)

    forbidden_import_roots = {
        "scout",
        "allocator",
        "risk",
        "broker",
        "orders",
        "emergency",
        "composition",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots


def test_module_has_no_forbidden_runtime_or_imports() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)

    forbidden_import_roots = {
        "scout",
        "allocator",
        "risk",
        "broker",
        "orders",
        "emergency",
        "composition",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots

    forbidden_text = (
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
        "uv run",
        "subprocess",
        "os.system",
        "ScoutInputBuilder",
        "AllocatorDecision",
        "AllocationRegime",
        "run_explicit_schedule_rules_walk_forward_nav",
        "run_explicit_synthetic_backtest_evaluation_pipeline",
        "compute_walk_forward_benchmark_relative_metrics",
        "render_backtest_evaluation_report_bundle",
        "BenchmarkRelativeMetrics",
        "BacktestWalkForwardResult",
        "BacktestEvaluationPipelineResult",
        "markdown_report",
        "investment advice",
        "beats S&P",
        "beat S&P",
    )
    for token in forbidden_text:
        assert token not in text, f"forbidden token present: {token}"


def test_default_monthly_local_data_file_specs_cover_expected_files() -> None:
    specs = default_monthly_local_data_file_specs()
    logical_names = {spec.logical_name for spec in specs}
    assert logical_names == {
        "sp500tr_monthly",
        "usdkrw_monthly",
        "kospi_monthly",
        "gld_monthly",
        "kodex200_monthly",
    }
    for spec in specs:
        assert spec.required_columns == REQUIRED_COLUMNS


def test_policy_constant_matches_result_model() -> None:
    assert LOCAL_DATA_PREFLIGHT_POLICY_V1 == "sibling_local_csv_preflight.v1"


def test_result_model_is_frozen_and_forbids_extra_fields(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    result = run_local_data_preflight(
        repo_root=repo_root,
        data_root=data_root,
        file_specs=(_spec(),),
    )

    with pytest.raises(ValidationError):
        result.local_data_preflight_policy = "other"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        LocalDataPreflightResult(
            local_data_preflight_policy=LOCAL_DATA_PREFLIGHT_POLICY_V1,
            repo_root=str(repo_root),
            data_root=str(data_root),
            files=result.files,
            warnings=(),
            recommendation="forbidden",  # type: ignore[call-arg]
        )


def test_rejects_file_spec_path_escaping_data_root(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    outside_path = tmp_path / "outside.csv"
    outside_path.write_text("date\n2020-01-31\n", encoding="utf-8")

    evil_spec = LocalDataFileSpec(
        logical_name="escape",
        relative_path="monthly/link.csv",
        required_columns=("date",),
    )
    linked_path = data_root / "monthly" / "link.csv"
    linked_path.parent.mkdir(parents=True, exist_ok=True)
    linked_path.symlink_to(outside_path)

    with pytest.raises(ValueError, match="escapes data_root"):
        run_local_data_preflight(
            repo_root=repo_root,
            data_root=data_root,
            file_specs=(evil_spec,),
        )


def test_focused_regression_suite_passes() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    command = [
        "uv",
        "run",
        "pytest",
        *[
            path
            for path in FOCUSED_TEST_FILES
            if path != "tests/test_backtest_local_data_preflight.py"
        ],
        "-q",
    ]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
