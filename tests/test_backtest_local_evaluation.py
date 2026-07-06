from __future__ import annotations

import ast
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine.local_dataset import (  # noqa: E402
    LocalMonthlyInstrumentSpec,
    assemble_local_monthly_dataset,
    default_local_monthly_benchmark_spec,
    default_local_monthly_instrument_specs_for_kospi_primary,
)
from backtest_engine.local_evaluation import (  # noqa: E402
    LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1,
    LocalMonthlyEvaluationDryRunResult,
    run_local_monthly_evaluation_dry_run,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "backtest_engine"
    / "local_evaluation.py"
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
    "tests/test_backtest_local_evaluation.py",
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


def _month_add(*, year: int, month: int, offset: int) -> tuple[int, int]:
    month += offset
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    return year, month


def _iso_as_of(*, year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}-01T00:00:00+00:00"


def _instrument_rows_for_period(
    *,
    period: str,
    symbol: str,
    market: str,
    close: Decimal,
    lookback_count: int,
) -> tuple[str, ...]:
    year, month = period.split("-")
    year_int = int(year)
    month_int = int(month)
    day = "31" if month in {"01", "03", "05", "07", "08", "10", "12"} else "29"
    early_year, early_month = _month_add(
        year=year_int,
        month=month_int,
        offset=1,
    )
    late_year, late_month = _month_add(
        year=year_int,
        month=month_int,
        offset=lookback_count,
    )
    row_prefix = f"{year}-{month}-{day}"
    return (
        f"{row_prefix},{_iso_as_of(year=early_year, month=early_month)},"
        f"{symbol},{market},{close},synthetic",
        f"{row_prefix},{_iso_as_of(year=late_year, month=late_month)},"
        f"{symbol},{market},{close},synthetic",
    )


def _kospi_primary_working_instrument_specs() -> tuple[LocalMonthlyInstrumentSpec, ...]:
    """Mirror default KOSPI-primary specs with a separate US asset CSV path."""
    return (
        LocalMonthlyInstrumentSpec(
            asset_id="asset_us",
            symbol="SP500TR",
            market="US",
            relative_path="monthly/sp500tr_asset_monthly.csv",
        ),
        LocalMonthlyInstrumentSpec(
            asset_id="asset_kr",
            symbol="KOSPI",
            market="KR",
            relative_path="monthly/kospi_monthly.csv",
        ),
        LocalMonthlyInstrumentSpec(
            asset_id="asset_gold",
            symbol="GLD",
            market="US",
            relative_path="monthly/gld_monthly.csv",
        ),
    )


def _write_kospi_primary_csvs(
    data_root: Path,
    *,
    periods: tuple[str, ...] = (
        "2020-01",
        "2020-02",
        "2020-03",
        "2020-04",
        "2020-05",
        "2020-06",
    ),
    lookback_count: int = 3,
    instrument_specs: tuple[LocalMonthlyInstrumentSpec, ...] | None = None,
) -> None:
    specs = (
        _kospi_primary_working_instrument_specs()
        if instrument_specs is None
        else instrument_specs
    )
    benchmark = default_local_monthly_benchmark_spec()

    for spec in specs:
        rows: list[str] = []
        for index, period in enumerate(periods):
            close = Decimal("100") + Decimal(index)
            if spec.relative_path.endswith("sp500tr_monthly.csv"):
                year, month = period.split("-")
                year_int = int(year)
                month_int = int(month)
                day = "31" if month in {"01", "03", "05", "07", "08", "10", "12"} else "29"
                late_year, late_month = _month_add(
                    year=year_int,
                    month=month_int,
                    offset=lookback_count,
                )
                rows.append(
                    f"{year}-{month}-{day},{_iso_as_of(year=late_year, month=late_month)},"
                    f"{spec.symbol},{spec.market},{close},synthetic"
                )
            else:
                rows.extend(
                    _instrument_rows_for_period(
                        period=period,
                        symbol=spec.symbol,
                        market=spec.market,
                        close=close,
                        lookback_count=lookback_count,
                    )
                )
        _write_csv(data_root / spec.relative_path, tuple(rows))

    sp_rows: list[str] = []
    fx_rows: list[str] = []
    for index, period in enumerate(periods):
        year, month = period.split("-")
        month_int = int(month)
        year_int = int(year)
        day = "31" if month in {"01", "03", "05", "07", "08", "10", "12"} else "29"
        late_year, late_month = _month_add(
            year=year_int,
            month=month_int,
            offset=lookback_count,
        )
        as_of = _iso_as_of(year=late_year, month=late_month)
        sp_rows.append(
            f"{year}-{month}-{day},{as_of},"
            f"SP500TR,US,{100 + index},synthetic"
        )
        fx_rows.append(
            f"{year}-{month}-{day},{as_of},"
            f"USDKRW,FX,{1300 + index},synthetic"
        )
    _write_csv(data_root / benchmark.sp500tr_relative_path, tuple(sp_rows))
    _write_csv(data_root / benchmark.usdkrw_relative_path, tuple(fx_rows))


def _prepare_working_layout(
    tmp_path: Path,
    *,
    periods: tuple[str, ...] | None = None,
) -> tuple[Path, Path, tuple[LocalMonthlyInstrumentSpec, ...]]:
    repo_root, data_root = _layout(tmp_path)
    working_specs = _kospi_primary_working_instrument_specs()
    kwargs: dict[str, object] = {"instrument_specs": working_specs}
    if periods is not None:
        kwargs["periods"] = periods
    _write_kospi_primary_csvs(data_root, **kwargs)  # type: ignore[arg-type]
    return repo_root, data_root, working_specs


def _run_dry_run(tmp_path: Path) -> LocalMonthlyEvaluationDryRunResult:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    return run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=working_specs,
    )


def test_builds_local_monthly_evaluation_dry_run_result_from_synthetic_csvs(
    tmp_path: Path,
) -> None:
    result = _run_dry_run(tmp_path)
    assert (
        result.local_monthly_evaluation_dry_run_policy
        == LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1
    )
    assert result.dataset.source_records
    assert result.run_config.period_specs
    assert result.walk_forward_result.steps
    assert result.benchmark_relative_result.metrics
    assert result.report_bundle.markdown_report.strip()


def test_calls_assemble_local_monthly_dataset(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.assemble_local_monthly_dataset",
        wraps=assemble_local_monthly_dataset,
    ) as mocked:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
        )
    mocked.assert_called_once()


def test_calls_build_kospi_primary_monthly_run_config(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.build_kospi_primary_monthly_run_config"
    ) as mocked:
        mocked.side_effect = lambda **kwargs: __import__(
            "backtest_engine.local_run_config",
            fromlist=["build_kospi_primary_monthly_run_config"],
        ).build_kospi_primary_monthly_run_config(**kwargs)
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
        )
    mocked.assert_called_once()


def test_calls_run_explicit_schedule_rules_walk_forward_nav(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.run_explicit_schedule_rules_walk_forward_nav",
        wraps=__import__(
            "backtest_engine.walk_forward",
            fromlist=["run_explicit_schedule_rules_walk_forward_nav"],
        ).run_explicit_schedule_rules_walk_forward_nav,
    ) as mocked:
        result = run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
        )
        mocked.assert_called_once()
        assert mocked.call_args.args[0] == result.dataset.source_records


def test_calls_compute_walk_forward_benchmark_relative_metrics(
    tmp_path: Path,
) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.compute_walk_forward_benchmark_relative_metrics"
    ) as mocked:
        mocked.side_effect = lambda **kwargs: __import__(
            "backtest_engine.benchmark_adapter",
            fromlist=["compute_walk_forward_benchmark_relative_metrics"],
        ).compute_walk_forward_benchmark_relative_metrics(**kwargs)
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
        )
    mocked.assert_called_once()


def test_calls_render_backtest_evaluation_report_bundle(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.render_backtest_evaluation_report_bundle"
    ) as mocked:
        mocked.side_effect = lambda **kwargs: __import__(
            "backtest_engine.report_bundle",
            fromlist=["render_backtest_evaluation_report_bundle"],
        ).render_backtest_evaluation_report_bundle(**kwargs)
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
        )
    mocked.assert_called_once()


def test_does_not_call_synthetic_pipeline_wrapper(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch(
        "backtest_engine.evaluation_pipeline.run_explicit_synthetic_backtest_evaluation_pipeline"
    ) as mocked:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
        )
    mocked.assert_not_called()


def test_uses_default_kospi_primary_instrument_specs_when_none_supplied(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _layout(tmp_path)
    _write_kospi_primary_csvs(
        data_root,
        instrument_specs=default_local_monthly_instrument_specs_for_kospi_primary(),
    )
    with patch(
        "backtest_engine.local_evaluation.assemble_local_monthly_dataset",
        wraps=assemble_local_monthly_dataset,
    ) as mocked_assemble:
        with pytest.raises(ValueError, match="insufficient visible price observations"):
            run_local_monthly_evaluation_dry_run(
                repo_root=repo_root,
                data_root=data_root,
                instrument_specs=None,
            )
    assert mocked_assemble.call_args.kwargs["instrument_specs"] == (
        default_local_monthly_instrument_specs_for_kospi_primary()
    )


def test_uses_default_benchmark_spec_when_none_supplied(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    working_specs = _kospi_primary_working_instrument_specs()
    _write_kospi_primary_csvs(data_root, instrument_specs=working_specs)
    with patch(
        "backtest_engine.local_evaluation.assemble_local_monthly_dataset",
        wraps=assemble_local_monthly_dataset,
    ) as mocked_assemble:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
            benchmark_spec=None,
        )
    assert mocked_assemble.call_args.kwargs["benchmark_spec"] == (
        default_local_monthly_benchmark_spec()
    )


def test_preserves_dataset(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    dataset = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=working_specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=working_specs,
    )
    assert result.dataset == dataset


def test_preserves_run_config(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert result.run_config.dataset == result.dataset
    assert result.run_config.period_specs


def test_preserves_walk_forward_result(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert result.walk_forward_result.initial_portfolio_state == (
        result.run_config.initial_portfolio_state
    )
    assert len(result.walk_forward_result.steps) == len(result.run_config.period_specs)


def test_preserves_benchmark_relative_result(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert (
        result.benchmark_relative_result.walk_forward_result
        == result.walk_forward_result
    )


def test_preserves_report_bundle(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert (
        result.report_bundle.benchmark_relative_result
        == result.benchmark_relative_result
    )


def test_produces_non_empty_markdown_report_in_memory(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert result.report_bundle.markdown_report.strip()


def test_does_not_write_markdown_report_file(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch("pathlib.Path.write_text") as mocked_write_text:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
        )
    mocked_write_text.assert_not_called()


def test_does_not_write_json_artifact(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch("json.dump") as mocked_json_dump:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
        )
    mocked_json_dump.assert_not_called()


def test_does_not_call_open_for_write(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch("builtins.open") as mocked_open:
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
        )
    for call in mocked_open.call_args_list:
        mode = call.args[1] if len(call.args) > 1 else call.kwargs.get("mode", "r")
        assert "w" not in str(mode)


def test_does_not_fetch_or_download_data(tmp_path: Path) -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "yfinance",
        "fred",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "websocket",
        "websockets",
        "aiohttp",
        "subprocess",
        "os.system",
        "uv run",
    )
    for token in forbidden:
        assert token not in text, f"forbidden token present: {token}"


def test_does_not_import_yfinance_fred_or_network_libraries() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)
    forbidden_roots = {
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


def test_does_not_write_sqlite_or_call_save_record() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "SQLiteDateIdSourceStore" not in text
    assert ".save_record(" not in text


def test_does_not_import_runtime_packages() -> None:
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


def test_result_model_is_frozen_and_forbids_extra_fields(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)

    with pytest.raises(ValidationError):
        result.local_monthly_evaluation_dry_run_policy = "other"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        LocalMonthlyEvaluationDryRunResult(
            local_monthly_evaluation_dry_run_policy=(
                LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1
            ),
            dataset=result.dataset,
            run_config=result.run_config,
            walk_forward_result=result.walk_forward_result,
            benchmark_relative_result=result.benchmark_relative_result,
            report_bundle=result.report_bundle,
            warnings=result.warnings,
            artifact_path="/tmp/forbidden",  # type: ignore[call-arg]
        )


def test_result_has_no_forbidden_fields() -> None:
    fields = set(LocalMonthlyEvaluationDryRunResult.model_fields)
    forbidden = {
        "output_path",
        "artifact_path",
        "markdown_report_path",
        "persisted_report_path",
        "report_path",
        "recommendation",
        "recommendations",
        "investment_advice",
        "conclusion",
        "project_conclusion",
    }
    assert fields.isdisjoint(forbidden)


def test_warnings_include_research_only_no_investment_conclusion(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert any(
        "research evidence only" in warning and "investment conclusion" in warning
        for warning in result.warnings
    )


def test_warnings_include_kospi_proxy_caveat(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    assert any("KOSPI primary is a KR proxy" in warning for warning in result.warnings)


def test_propagates_dataset_assembly_failure(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    with pytest.raises(ValueError, match="CSV file not found"):
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
        )


def test_propagates_run_config_failure(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(
        tmp_path,
        periods=("2020-01", "2020-02", "2020-03"),
    )
    with pytest.raises(ValueError, match="rolling_lookback_count \\+ 1"):
        run_local_monthly_evaluation_dry_run(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=working_specs,
            rolling_lookback_count=3,
        )


def test_propagates_walk_forward_failure(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.run_explicit_schedule_rules_walk_forward_nav",
        side_effect=ValueError("walk-forward failed"),
    ):
        with pytest.raises(ValueError, match="walk-forward failed"):
            run_local_monthly_evaluation_dry_run(
                repo_root=repo_root,
                data_root=data_root,
                instrument_specs=working_specs,
            )


def test_propagates_benchmark_adapter_failure(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.compute_walk_forward_benchmark_relative_metrics",
        side_effect=ValueError("benchmark adapter failed"),
    ):
        with pytest.raises(ValueError, match="benchmark adapter failed"):
            run_local_monthly_evaluation_dry_run(
                repo_root=repo_root,
                data_root=data_root,
                instrument_specs=working_specs,
            )


def test_propagates_report_bundle_failure(tmp_path: Path) -> None:
    repo_root, data_root, working_specs = _prepare_working_layout(tmp_path)
    with patch(
        "backtest_engine.local_evaluation.render_backtest_evaluation_report_bundle",
        side_effect=ValueError("report bundle failed"),
    ):
        with pytest.raises(ValueError, match="report bundle failed"):
            run_local_monthly_evaluation_dry_run(
                repo_root=repo_root,
                data_root=data_root,
                instrument_specs=working_specs,
            )


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
        "open(",
        ".write(",
        "Path.write_text",
        "json.dump",
        "markdown_report_path",
        "artifact_path",
        "recommendation",
        "investment advice",
        "beats S&P",
        "beat S&P",
        "run_explicit_synthetic_backtest_evaluation_pipeline",
    )
    for token in forbidden_text:
        assert token not in text, f"forbidden token present: {token}"

    allowed_calls = (
        "assemble_local_monthly_dataset",
        "build_kospi_primary_monthly_run_config",
        "run_explicit_schedule_rules_walk_forward_nav",
        "compute_walk_forward_benchmark_relative_metrics",
        "render_backtest_evaluation_report_bundle",
    )
    for call in allowed_calls:
        assert call in text, f"expected allowed call present: {call}"


def test_focused_regression_suite_passes() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    completed = subprocess.run(
        ["uv", "run", "pytest", *FOCUSED_TEST_FILES, "-q"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
