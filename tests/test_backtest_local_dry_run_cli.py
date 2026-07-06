from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine.local_dataset import (  # noqa: E402
    default_local_monthly_benchmark_spec,
    default_local_monthly_instrument_specs_for_kospi_primary,
)
from backtest_engine.local_dry_run_cli import (  # noqa: E402
    build_arg_parser,
    main,
    render_local_dry_run_summary,
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
    / "local_dry_run_cli.py"
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
    "tests/test_backtest_local_dry_run_cli.py",
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

FORBIDDEN_CLI_ARGS = (
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


def _prepare_default_layout(tmp_path: Path) -> tuple[Path, Path]:
    repo_root, data_root = _layout(tmp_path)
    _write_default_kospi_primary_csvs(data_root)
    return repo_root, data_root


def _run_dry_run(tmp_path: Path) -> LocalMonthlyEvaluationDryRunResult:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    return run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )


def _cli_argv(repo_root: Path, data_root: Path, *extra: str) -> list[str]:
    return [
        "--repo-root",
        str(repo_root),
        "--data-root",
        str(data_root),
        *extra,
    ]


def test_build_arg_parser_accepts_allowed_args() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--repo-root",
            "/tmp/AutoStock",
            "--data-root",
            "/tmp/autostock-data",
            "--initial-cash-krw",
            "100000000",
            "--cash-min-weight",
            "0.05",
            "--rolling-lookback-count",
            "3",
            "--fee-bps",
            "10",
            "--kr-sell-tax-bps",
            "23",
            "--fx-spread-bps",
            "15",
            "--show-markdown-preview",
        ]
    )
    assert args.repo_root == Path("/tmp/AutoStock")
    assert args.data_root == Path("/tmp/autostock-data")
    assert args.initial_cash_krw == Decimal("100000000")
    assert args.cash_min_weight == Decimal("0.05")
    assert args.rolling_lookback_count == 3
    assert args.fee_bps == Decimal("10")
    assert args.kr_sell_tax_bps == Decimal("23")
    assert args.fx_spread_bps == Decimal("15")
    assert args.show_markdown_preview is True


@pytest.mark.parametrize("forbidden_arg", FORBIDDEN_CLI_ARGS)
def test_forbidden_cli_args_are_rejected(forbidden_arg: str) -> None:
    exit_code = main([forbidden_arg])
    assert exit_code != 0


def test_main_calls_run_local_monthly_evaluation_dry_run(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    with patch(
        "backtest_engine.local_dry_run_cli.run_local_monthly_evaluation_dry_run",
        wraps=run_local_monthly_evaluation_dry_run,
    ) as mocked:
        exit_code = main(_cli_argv(repo_root, data_root))
    mocked.assert_called_once()
    assert exit_code == 0


def test_main_passes_repo_root_data_root_and_options(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    expected_result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    with patch(
        "backtest_engine.local_dry_run_cli.run_local_monthly_evaluation_dry_run"
    ) as mocked:
        mocked.return_value = expected_result
        exit_code = main(
            [
                "--repo-root",
                str(repo_root),
                "--data-root",
                str(data_root),
                "--initial-cash-krw",
                "50000000",
                "--cash-min-weight",
                "0.10",
                "--rolling-lookback-count",
                "3",
                "--fee-bps",
                "12",
                "--kr-sell-tax-bps",
                "20",
                "--fx-spread-bps",
                "18",
            ]
        )
    mocked.assert_called_once_with(
        repo_root=repo_root,
        data_root=data_root,
        initial_cash_krw=Decimal("50000000"),
        cash_min_weight=Decimal("0.10"),
        rolling_lookback_count=3,
        fee_bps=Decimal("12"),
        kr_sell_tax_bps=Decimal("20"),
        fx_spread_bps=Decimal("18"),
    )
    assert exit_code == 0


def test_main_success_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    exit_code = main(_cli_argv(repo_root, data_root))
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "AutoStock local monthly evaluation dry-run" in captured.out


def test_main_failure_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_root, data_root = _layout(tmp_path)
    exit_code = main(_cli_argv(repo_root, data_root))
    captured = capsys.readouterr()
    assert exit_code != 0
    assert captured.err


def test_summary_includes_policy_fields(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    summary = render_local_dry_run_summary(result)
    assert f"policy: {LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1}" in summary
    assert (
        f"dataset_policy: {result.dataset.local_monthly_dataset_policy}" in summary
    )
    assert (
        f"run_config_policy: {result.run_config.local_monthly_run_config_policy}"
        in summary
    )


def test_summary_includes_period_nav_benchmark_common_counts(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    summary = render_local_dry_run_summary(result)
    assert f"period_count: {len(result.run_config.period_specs)}" in summary
    assert (
        f"nav_point_count: {len(result.walk_forward_result.nav_points)}" in summary
    )
    assert (
        f"benchmark_point_count: {len(result.dataset.benchmark_points)}" in summary
    )
    assert (
        f"common_period_count: {len(result.dataset.common_periods)}" in summary
    )


def test_summary_includes_first_and_last_common_period(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    summary = render_local_dry_run_summary(result)
    assert f"first_common_period: {result.dataset.common_periods[0]}" in summary
    assert f"last_common_period: {result.dataset.common_periods[-1]}" in summary


def test_summary_includes_first_and_last_execution_as_of(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    summary = render_local_dry_run_summary(result)
    first_as_of = result.run_config.period_specs[0].intended_execution_time.isoformat()
    last_as_of = result.run_config.period_specs[-1].intended_execution_time.isoformat()
    assert f"first_execution_as_of: {first_as_of}" in summary
    assert f"last_execution_as_of: {last_as_of}" in summary


def test_summary_includes_final_portfolio_value(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    summary = render_local_dry_run_summary(result)
    final_value = result.walk_forward_result.nav_points[-1].portfolio_value_krw
    assert f"final_portfolio_value_krw: {final_value}" in summary


def test_summary_includes_total_cost(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    summary = render_local_dry_run_summary(result)
    assert (
        f"total_cost_krw: {result.walk_forward_result.total_cost_krw}" in summary
    )


def test_summary_includes_benchmark_relative_terminal_fields(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    metrics = result.benchmark_relative_result.metrics
    summary = render_local_dry_run_summary(result)
    assert f"terminal_strategy_return: {metrics.bot_total_return_percent}" in summary
    assert (
        f"terminal_benchmark_return: {metrics.benchmark_total_return_percent}"
        in summary
    )
    assert f"terminal_excess_return: {metrics.excess_return_percent}" in summary


def test_summary_includes_max_relative_drawdown(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    metrics = result.benchmark_relative_result.metrics
    summary = render_local_dry_run_summary(result)
    assert f"max_relative_drawdown: {metrics.relative_drawdown_percent}" in summary


def test_summary_includes_research_only_warning(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    summary = render_local_dry_run_summary(result)
    assert "research evidence only" in summary
    assert "investment conclusion" in summary


def test_summary_includes_kospi_proxy_caveat_when_present(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    summary = render_local_dry_run_summary(result)
    assert any(
        "KOSPI primary is a KR proxy" in warning for warning in result.warnings
    )
    assert "KOSPI primary is a KR proxy" in summary


def test_summary_does_not_include_raw_csv_rows(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    csv_text = (data_root / "monthly/sp500tr_monthly.csv").read_text(encoding="utf-8")
    result = run_local_monthly_evaluation_dry_run(
        repo_root=repo_root,
        data_root=data_root,
    )
    summary = render_local_dry_run_summary(result)
    for line in csv_text.splitlines()[1:]:
        assert line not in summary


def test_summary_does_not_include_full_markdown_by_default(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    summary = render_local_dry_run_summary(result)
    markdown = result.report_bundle.markdown_report
    if len(markdown.splitlines()) > 20:
        assert markdown not in summary
    assert "markdown_preview:" not in summary


def test_markdown_preview_prints_at_most_first_20_lines(tmp_path: Path) -> None:
    result = _run_dry_run(tmp_path)
    markdown_lines = result.report_bundle.markdown_report.splitlines()
    summary = render_local_dry_run_summary(result, show_markdown_preview=True)
    preview_start = summary.index("markdown_preview:")
    preview_body = summary[preview_start:].splitlines()[1:]
    assert len(preview_body) <= 20
    assert preview_body == markdown_lines[:20]


def test_cli_does_not_write_files(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    before = {
        path
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    exit_code = main(_cli_argv(repo_root, data_root))
    after = {
        path
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert exit_code == 0
    assert before == after


def test_cli_does_not_create_artifacts(tmp_path: Path) -> None:
    repo_root, data_root = _prepare_default_layout(tmp_path)
    forbidden_suffixes = (".backtest.json", ".backtest.md")
    exit_code = main(_cli_argv(repo_root, data_root))
    assert exit_code == 0
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert not path.name.endswith(forbidden_suffixes)
            assert "backtest_outputs" not in path.parts


def test_cli_does_not_import_network_or_fetch_libraries() -> None:
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


def test_cli_does_not_read_config_toml() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "config.toml" not in text
    assert "config/config.toml" not in text


def test_cli_does_not_call_synthetic_pipeline_wrapper() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    assert "run_explicit_synthetic_backtest_evaluation_pipeline" not in text


def test_cli_does_not_import_runtime_packages() -> None:
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


def test_module_static_scan_rejects_forbidden_tokens() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
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
        "config.toml",
        "config/config.toml",
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
        "output_path",
        "recommendation",
        "investment advice",
        "beats S&P",
        "beat S&P",
        "run_explicit_synthetic_backtest_evaluation_pipeline",
    )
    for token in forbidden_text:
        assert token not in text, f"forbidden token present: {token}"

    allowed_calls = (
        "run_local_monthly_evaluation_dry_run",
        "LocalMonthlyEvaluationDryRunResult",
    )
    for call in allowed_calls:
        assert call in text, f"expected allowed call present: {call}"


def test_module_import_roots_exclude_runtime_packages() -> None:
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


def test_focused_regression_suite_passes() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "src")
    completed = subprocess.run(
        ["uv", "run", "pytest", *FOCUSED_TEST_FILES, "-q", "-k", "not focused_regression"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
