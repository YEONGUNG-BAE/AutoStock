from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine.local_dataset import (  # noqa: E402
    assemble_local_monthly_dataset,
    default_local_monthly_benchmark_spec,
    default_local_monthly_instrument_specs_for_kospi_primary,
)
from backtest_engine.local_run_config import (  # noqa: E402
    LOCAL_MONTHLY_RUN_CONFIG_POLICY_V1,
    LocalMonthlyRunConfig,
    build_kospi_primary_monthly_run_config,
)
from backtest_engine.rebalance import COST_MODEL_V1  # noqa: E402

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "backtest_engine"
    / "local_run_config.py"
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

FOCUSED_TEST_FILES = (
    "tests/test_backtest_local_run_config.py",
    "tests/test_backtest_local_dataset.py",
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


def _write_csv(path: Path, rows: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    repo_root = tmp_path / "AutoStock"
    data_root = tmp_path / "autostock-data"
    repo_root.mkdir()
    data_root.mkdir()
    return repo_root, data_root


def _assemble_kospi_primary_dataset(
    tmp_path: Path,
    *,
    periods: tuple[str, ...] = ("2020-01", "2020-02", "2020-03", "2020-04"),
    first_day_of_month_dates: bool = False,
):
    repo_root, data_root = _layout(tmp_path)
    specs = default_local_monthly_instrument_specs_for_kospi_primary()
    benchmark = default_local_monthly_benchmark_spec()

    for spec in specs:
        rows: list[str] = []
        for index, period in enumerate(periods):
            year, month = period.split("-")
            if first_day_of_month_dates:
                day = "01"
            else:
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
        if first_day_of_month_dates:
            day = "01"
        else:
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

    dataset = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=benchmark,
    )
    return dataset


def test_builds_local_monthly_run_config_from_synthetic_assembled_dataset(
    tmp_path: Path,
) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)
    assert result.local_monthly_run_config_policy == LOCAL_MONTHLY_RUN_CONFIG_POLICY_V1
    assert result.dataset is dataset


def test_does_not_read_csvs_directly() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_roots.add(node.module.split(".")[0])
    assert "csv" not in imported_roots
    assert "read_csv" not in text


def test_does_not_fetch_or_download_data() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = ("yfinance", "fred", "requests", "httpx", "urllib", "socket")
    for token in forbidden:
        assert token not in text


def test_does_not_call_walk_forward() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "run_explicit_schedule_rules_walk_forward_nav" not in text


def test_does_not_call_evaluation_pipeline() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "run_explicit_synthetic_backtest_evaluation_pipeline" not in text


def test_does_not_call_benchmark_adapter() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "compute_walk_forward_benchmark_relative_metrics" not in text


def test_does_not_call_report_bundle() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "render_backtest_evaluation_report_bundle" not in text


def test_creates_rolling_configs_for_asset_us_asset_kr_asset_gold(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)
    asset_ids = {config.asset_id for config in result.rolling_asset_configs}
    assert asset_ids == {"asset_us", "asset_kr", "asset_gold"}


def test_kospi_primary_uses_kospi_as_kr_proxy(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)
    kr_config = next(
        config for config in result.rolling_asset_configs if config.asset_id == "asset_kr"
    )
    assert kr_config.symbol == "KOSPI"
    assert kr_config.market == "KR"


def test_kodex200_is_not_included(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)
    symbols = {config.symbol for config in result.rolling_asset_configs}
    assert "KODEX200" not in symbols


def test_uses_fixed_deterministic_weights(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)
    by_asset = {config.asset_id: config for config in result.rolling_asset_configs}

    assert by_asset["asset_us"].risk_on_weight == Decimal("0.60")
    assert by_asset["asset_us"].risk_off_weight == Decimal("0.30")
    assert by_asset["asset_us"].max_weight == Decimal("0.80")

    assert by_asset["asset_kr"].risk_on_weight == Decimal("0.20")
    assert by_asset["asset_kr"].risk_off_weight == Decimal("0.05")
    assert by_asset["asset_kr"].max_weight == Decimal("0.40")

    assert by_asset["asset_gold"].risk_on_weight == Decimal("0.15")
    assert by_asset["asset_gold"].risk_off_weight == Decimal("0.25")
    assert by_asset["asset_gold"].max_weight == Decimal("0.35")


def test_uses_requested_rolling_lookback_count(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(
        tmp_path,
        periods=("2020-01", "2020-02", "2020-03", "2020-04", "2020-05"),
    )
    result = build_kospi_primary_monthly_run_config(
        dataset=dataset,
        rolling_lookback_count=4,
    )
    assert all(config.lookback_count == 4 for config in result.rolling_asset_configs)


def test_builds_initial_portfolio_with_cash_only(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(
        dataset=dataset,
        initial_cash_krw=Decimal("50000000"),
    )
    assert result.initial_portfolio_state.cash_krw == Decimal("50000000")
    assert result.initial_portfolio_state.holdings == ()


def test_initial_portfolio_as_of_is_first_common_period_latest_source_timestamp(
    tmp_path: Path,
) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)

    first_period = dataset.common_periods[0]
    expected = max(
        record.source_timestamp
        for record in dataset.source_records
        if record.payload["date"].startswith(first_period[:4] + "-" + first_period[5:])
    )
    assert result.initial_portfolio_state.as_of == expected


def test_builds_cost_model_from_args(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(
        dataset=dataset,
        fee_bps=Decimal("11"),
        kr_sell_tax_bps=Decimal("24"),
        fx_spread_bps=Decimal("16"),
    )
    assert result.cost_model.cost_model_version == COST_MODEL_V1
    assert result.cost_model.fee_bps == Decimal("11")
    assert result.cost_model.kr_sell_tax_bps == Decimal("24")
    assert result.cost_model.fx_spread_bps == Decimal("16")


def test_builds_period_specs_from_common_periods(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)
    assert len(result.period_specs) == len(dataset.common_periods) - 1


def test_first_common_period_is_warm_up_baseline(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)
    first_execution_period = dataset.common_periods[1]
    first_spec = result.period_specs[0]
    expected_execution = max(
        record.source_timestamp
        for record in dataset.source_records
        if record.payload["date"].startswith(
            first_execution_period[:4] + "-" + first_execution_period[5:]
        )
    )
    assert first_spec.intended_execution_time == expected_execution


def test_period_spec_count_equals_common_periods_minus_one(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)
    assert len(result.period_specs) == len(dataset.common_periods) - 1


def test_each_decision_time_is_previous_period_latest_source_timestamp(
    tmp_path: Path,
) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)

    for index, period_spec in enumerate(result.period_specs):
        previous_period = dataset.common_periods[index]
        expected = max(
            record.source_timestamp
            for record in dataset.source_records
            if record.payload["date"].startswith(
                previous_period[:4] + "-" + previous_period[5:]
            )
        )
        assert period_spec.decision_time == expected


def test_each_intended_execution_time_is_current_period_latest_source_timestamp(
    tmp_path: Path,
) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)

    for index, period_spec in enumerate(result.period_specs):
        current_period = dataset.common_periods[index + 1]
        expected = max(
            record.source_timestamp
            for record in dataset.source_records
            if record.payload["date"].startswith(
                current_period[:4] + "-" + current_period[5:]
            )
        )
        assert period_spec.intended_execution_time == expected


def test_each_period_uses_current_period_usdkrw_rate(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)
    fx_by_period = {point.period_key: point.usdkrw_rate for point in dataset.fx_points}

    for index, period_spec in enumerate(result.period_specs):
        current_period = dataset.common_periods[index + 1]
        assert period_spec.usdkrw_rate == fx_by_period[current_period]


def test_rejects_insufficient_common_periods(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path, periods=("2020-01", "2020-02", "2020-03"))
    with pytest.raises(ValueError, match="rolling_lookback_count \\+ 1"):
        build_kospi_primary_monthly_run_config(dataset=dataset, rolling_lookback_count=3)


def test_rejects_non_positive_initial_cash(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    with pytest.raises(ValueError, match="initial_cash_krw must be greater than 0"):
        build_kospi_primary_monthly_run_config(dataset=dataset, initial_cash_krw=Decimal("0"))


def test_rejects_invalid_cash_min_weight(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    with pytest.raises(ValueError, match="cash_min_weight must be between 0 and 1"):
        build_kospi_primary_monthly_run_config(dataset=dataset, cash_min_weight=Decimal("1.1"))


def test_rejects_invalid_rolling_lookback_count(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    with pytest.raises(ValueError, match="rolling_lookback_count must be >= 2"):
        build_kospi_primary_monthly_run_config(dataset=dataset, rolling_lookback_count=1)


def test_rejects_invalid_fee_tax_fx_spread_args(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    with pytest.raises(ValueError, match="fee_bps must be >= 0"):
        build_kospi_primary_monthly_run_config(dataset=dataset, fee_bps=Decimal("-1"))
    with pytest.raises(ValueError, match="kr_sell_tax_bps must be >= 0"):
        build_kospi_primary_monthly_run_config(dataset=dataset, kr_sell_tax_bps=Decimal("-1"))
    with pytest.raises(ValueError, match="fx_spread_bps must be >= 0"):
        build_kospi_primary_monthly_run_config(dataset=dataset, fx_spread_bps=Decimal("-1"))


def test_warns_if_dataset_has_first_day_of_month_restamping_warning(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path, first_day_of_month_dates=True)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)
    assert any("first day of month" in warning for warning in result.warnings)


def test_result_model_is_frozen_and_forbids_extra_fields(tmp_path: Path) -> None:
    dataset = _assemble_kospi_primary_dataset(tmp_path)
    result = build_kospi_primary_monthly_run_config(dataset=dataset)

    with pytest.raises(ValidationError):
        result.local_monthly_run_config_policy = "other"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        LocalMonthlyRunConfig(
            local_monthly_run_config_policy=LOCAL_MONTHLY_RUN_CONFIG_POLICY_V1,
            dataset=dataset,
            period_specs=result.period_specs,
            rolling_asset_configs=result.rolling_asset_configs,
            initial_portfolio_state=result.initial_portfolio_state,
            cost_model=result.cost_model,
            cash_asset_id="cash",
            cash_min_weight=Decimal("0.05"),
            warnings=(),
            nav_points=(),  # type: ignore[call-arg]
        )


def test_result_has_no_nav_fields() -> None:
    fields = set(LocalMonthlyRunConfig.model_fields)
    forbidden = {
        "nav",
        "nav_points",
        "portfolio_value_krw",
        "cash_krw",
        "total_nav_krw",
    }
    assert fields.isdisjoint(forbidden)


def test_result_has_no_benchmark_relative_metrics_fields() -> None:
    fields = set(LocalMonthlyRunConfig.model_fields)
    forbidden = {
        "benchmark_relative",
        "metrics",
        "alpha",
        "beta",
        "tracking_error",
        "information_ratio",
    }
    assert fields.isdisjoint(forbidden)


def test_result_has_no_markdown_report_fields() -> None:
    fields = set(LocalMonthlyRunConfig.model_fields)
    forbidden = {
        "markdown_report",
        "report_bundle",
        "report_markdown",
        "rendered_report",
    }
    assert fields.isdisjoint(forbidden)


def test_result_has_no_investment_conclusion_fields() -> None:
    fields = set(LocalMonthlyRunConfig.model_fields)
    forbidden = {
        "recommendation",
        "recommendations",
        "investment_advice",
        "conclusion",
        "project_conclusion",
    }
    assert fields.isdisjoint(forbidden)


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
        "csv",
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


def test_policy_constant_matches_result_model() -> None:
    assert LOCAL_MONTHLY_RUN_CONFIG_POLICY_V1 == "kospi_primary_monthly_rules_config.v1"


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
            if path != "tests/test_backtest_local_run_config.py"
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
