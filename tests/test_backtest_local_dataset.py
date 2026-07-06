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

from backtest_data import BACKTEST_INSTRUMENT_PRICE_SCHEMA  # noqa: E402
from backtest_engine.local_dataset import (  # noqa: E402
    LOCAL_MONTHLY_DATASET_POLICY_V1,
    LocalMonthlyBenchmarkSpec,
    LocalMonthlyDatasetAssemblyResult,
    LocalMonthlyInstrumentSpec,
    assemble_local_monthly_dataset,
    default_local_monthly_benchmark_spec,
    default_local_monthly_instrument_specs_for_kospi_primary,
)
from domain import DateIdSourceRecord, FactType  # noqa: E402

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "backtest_engine"
    / "local_dataset.py"
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


def _spec(
    *,
    asset_id: str = "asset_test",
    symbol: str = "SAMPLE",
    market: str = "US",
    relative_path: str = "monthly/sample_monthly.csv",
) -> LocalMonthlyInstrumentSpec:
    return LocalMonthlyInstrumentSpec(
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        relative_path=relative_path,
    )


def _benchmark_spec(
    *,
    sp500tr_relative_path: str = "monthly/sp500tr_monthly.csv",
    usdkrw_relative_path: str = "monthly/usdkrw_monthly.csv",
) -> LocalMonthlyBenchmarkSpec:
    return LocalMonthlyBenchmarkSpec(
        sp500tr_relative_path=sp500tr_relative_path,
        usdkrw_relative_path=usdkrw_relative_path,
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


def _write_minimal_dataset(
    data_root: Path,
    *,
    instrument_specs: tuple[LocalMonthlyInstrumentSpec, ...] | None = None,
    periods: tuple[str, ...] = ("2020-01", "2020-02"),
) -> tuple[LocalMonthlyInstrumentSpec, ...]:
    specs = instrument_specs or (
        _spec(
            asset_id="asset_us",
            symbol="SP500TR",
            market="US",
            relative_path="monthly/sp500tr_monthly.csv",
        ),
        _spec(
            asset_id="asset_kr",
            symbol="KOSPI",
            market="KR",
            relative_path="monthly/kospi_monthly.csv",
        ),
    )
    benchmark = default_local_monthly_benchmark_spec()

    period_rows = {
        "2020-01": (
            "2020-01-31,2020-02-01T00:00:00+00:00,{symbol},{market},{close},synthetic",
        ),
        "2020-02": (
            "2020-02-29,2020-03-01T00:00:00+00:00,{symbol},{market},{close},synthetic",
        ),
    }

    for spec in specs:
        rows: list[str] = []
        for index, period in enumerate(periods):
            close = Decimal("100") + Decimal(index)
            template = period_rows[period][0]
            rows.append(
                template.format(
                    symbol=spec.symbol,
                    market=spec.market,
                    close=str(close),
                )
            )
        _write_csv(data_root / spec.relative_path, tuple(rows))

    sp_rows: list[str] = []
    fx_rows: list[str] = []
    for index, period in enumerate(periods):
        date_suffix = "31" if period.endswith("-01") else "29"
        month = period.split("-")[1]
        year = period.split("-")[0]
        sp_rows.append(
            f"{year}-{month}-{date_suffix},2020-0{int(month)+1}-01T00:00:00+00:00,SP500TR,US,{100 + index},synthetic"
        )
        fx_rows.append(
            f"{year}-{month}-{date_suffix},2020-0{int(month)+1}-01T00:00:00+00:00,USDKRW,FX,{1300 + index},synthetic"
        )
    _write_csv(data_root / benchmark.sp500tr_relative_path, tuple(sp_rows))
    _write_csv(data_root / benchmark.usdkrw_relative_path, tuple(fx_rows))

    return specs


def test_default_data_root_resolves_to_sibling_autostock_data(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root)

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert result.data_root == str((repo_root.parent / "autostock-data").resolve())


def test_rejects_data_root_inside_repo_root(tmp_path: Path) -> None:
    repo_root, _ = _layout(tmp_path)
    nested_data_root = repo_root / "nested-data"

    with pytest.raises(ValueError, match="data_root must not be inside repo_root"):
        assemble_local_monthly_dataset(
            repo_root=repo_root,
            data_root=nested_data_root,
            instrument_specs=(_spec(),),
            benchmark_spec=_benchmark_spec(),
        )


def test_rejects_repo_root_inside_data_root(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    nested_repo_root = data_root / "AutoStock"

    with pytest.raises(ValueError, match="repo_root must not be inside data_root"):
        assemble_local_monthly_dataset(
            repo_root=nested_repo_root,
            data_root=data_root,
            instrument_specs=(_spec(),),
            benchmark_spec=_benchmark_spec(),
        )


def test_rejects_absolute_instrument_path() -> None:
    with pytest.raises(ValidationError, match="relative_path must be relative"):
        LocalMonthlyInstrumentSpec(
            asset_id="asset_test",
            symbol="SAMPLE",
            market="US",
            relative_path="/absolute/path.csv",
        )


def test_rejects_parent_traversal_in_instrument_path() -> None:
    with pytest.raises(ValidationError, match="relative_path must not contain"):
        LocalMonthlyInstrumentSpec(
            asset_id="asset_test",
            symbol="SAMPLE",
            market="US",
            relative_path="../escape.csv",
        )


def test_rejects_path_that_resolves_inside_repo_root(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    linked_path = data_root / "monthly" / "link.csv"
    linked_path.parent.mkdir(parents=True, exist_ok=True)
    repo_csv = repo_root / "secret.csv"
    repo_csv.write_text("date\n2020-01-31\n", encoding="utf-8")
    linked_path.symlink_to(repo_csv)

    with pytest.raises(ValueError, match="escapes data_root|resolves inside repo_root"):
        assemble_local_monthly_dataset(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=(
                _spec(relative_path="monthly/link.csv"),
            ),
            benchmark_spec=_benchmark_spec(),
        )


def test_reads_instrument_csv_with_stdlib_csv(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root)

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert len(result.source_records) == 4


def test_required_missing_columns_raise_deterministically(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("date,symbol,market\n2020-01-31,SAMPLE,US\n", encoding="utf-8")
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",),
    )

    with pytest.raises(ValueError, match="missing required columns"):
        assemble_local_monthly_dataset(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=(_spec(),),
            benchmark_spec=default_local_monthly_benchmark_spec(),
        )


def test_symbol_mismatch_raises(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        ("2020-01-31,2020-02-01T00:00:00+00:00,OTHER,US,100.0,synthetic",),
    )
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",),
    )

    with pytest.raises(ValueError, match="symbol mismatch"):
        assemble_local_monthly_dataset(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=(_spec(symbol="SAMPLE"),),
            benchmark_spec=default_local_monthly_benchmark_spec(),
        )


def test_market_mismatch_raises(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        ("2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,KR,100.0,synthetic",),
    )
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",),
    )

    with pytest.raises(ValueError, match="market mismatch"):
        assemble_local_monthly_dataset(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=(_spec(market="US"),),
            benchmark_spec=default_local_monthly_benchmark_spec(),
        )


def test_non_positive_close_adjusted_raises(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        ("2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,0,synthetic",),
    )
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",),
    )

    with pytest.raises(ValueError, match="close_adjusted"):
        assemble_local_monthly_dataset(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=(_spec(),),
            benchmark_spec=default_local_monthly_benchmark_spec(),
        )


def test_naive_or_invalid_as_of_raises(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    csv_path = data_root / "monthly" / "sample_monthly.csv"
    _write_csv(
        csv_path,
        ("2020-01-31,2020-02-01 00:00:00,SAMPLE,US,100.0,synthetic",),
    )
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",),
    )

    with pytest.raises(ValueError, match="invalid as_of"):
        assemble_local_monthly_dataset(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=(_spec(),),
            benchmark_spec=default_local_monthly_benchmark_spec(),
        )


def test_creates_date_id_source_records(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root)

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert all(type(record) is DateIdSourceRecord for record in result.source_records)


def test_source_records_have_fact_type_price(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root)

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert all(record.fact_type == FactType.PRICE for record in result.source_records)


def test_source_records_use_backtest_instrument_price_schema(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root)

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert all(
        record.payload["schema_name"] == BACKTEST_INSTRUMENT_PRICE_SCHEMA
        for record in result.source_records
    )


def test_source_records_preserve_symbol_market_date_close_adjusted(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root, periods=("2020-01",))

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    record = next(record for record in result.source_records if record.symbol == "KOSPI")
    assert record.market == "KR"
    assert record.payload["date"] == "2020-01-31"
    assert record.payload["close_adjusted"] == "100"
    assert record.payload["symbol"] == "KOSPI"


def test_date_id_generation_is_deterministic(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root)

    first = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )
    second = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert [record.date_id.value for record in first.source_records] == [
        record.date_id.value for record in second.source_records
    ]


def test_benchmark_points_are_built_from_sp500tr_times_usdkrw(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root, periods=("2020-01",))
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",),
    )

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert len(result.benchmark_points) == 1
    assert result.benchmark_points[0].total_return_index_value == Decimal("130000")


def test_fx_points_are_built_from_usdkrw_csv(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root, periods=("2020-01", "2020-02"))
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,SP500TR,US,101,synthetic",
        ),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,USDKRW,FX,1301,synthetic",
        ),
    )

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert len(result.fx_points) == 2
    assert result.fx_points[0].period_key == "2020-01"
    assert result.fx_points[0].usdkrw_rate == Decimal("1300")
    assert result.fx_points[1].period_key == "2020-02"
    assert result.fx_points[1].usdkrw_rate == Decimal("1301")


def test_fx_points_are_metadata_only_not_nav() -> None:
    fields = set(LocalMonthlyDatasetAssemblyResult.model_fields)
    forbidden = {
        "nav",
        "nav_points",
        "portfolio_value_krw",
        "total_nav_krw",
        "benchmark_relative",
        "metrics",
        "markdown_report",
        "investment_advice",
    }
    assert "fx_points" in fields
    assert fields.isdisjoint(forbidden)


def test_benchmark_as_of_uses_max_of_sp500tr_and_usdkrw_as_of(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root, periods=("2020-01",))
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        ("2020-01-31,2020-03-15T12:00:00+00:00,USDKRW,FX,1300,synthetic",),
    )

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert result.benchmark_points[0].as_of == datetime(2020, 3, 15, 12, 0, tzinfo=UTC)


def test_benchmark_alignment_uses_common_periods_only(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root, periods=("2020-01", "2020-02"))
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,SP500TR,US,101,synthetic",
            "2020-03-31,2020-04-01T00:00:00+00:00,SP500TR,US,102,synthetic",
        ),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,USDKRW,FX,1301,synthetic",
        ),
    )

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert len(result.benchmark_points) == 2
    assert any("dropped_non_common_periods" in warning for warning in result.warnings)


def test_benchmark_has_no_forward_fill(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root, periods=("2020-02",))
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        ("2020-02-29,2020-03-01T00:00:00+00:00,SP500TR,US,101,synthetic",),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,USDKRW,FX,1301,synthetic",
        ),
    )

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert len(result.benchmark_points) == 1
    assert result.benchmark_points[0].total_return_index_value == Decimal("101") * Decimal("1301")


def test_benchmark_has_no_back_fill(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    _write_csv(
        data_root / "monthly/sample_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,SAMPLE,US,101,synthetic",
        ),
    )
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,SP500TR,US,101,synthetic",
        ),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        ("2020-02-29,2020-03-01T00:00:00+00:00,USDKRW,FX,1301,synthetic",),
    )

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=(_spec(),),
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert len(result.benchmark_points) == 1
    assert result.common_periods == ("2020-02",)
    assert any("missing_fx_for_benchmark_period:2020-01" in warning for warning in result.warnings)


def test_benchmark_has_no_interpolation(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    _write_csv(
        data_root / "monthly/sample_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100,synthetic",
            "2020-03-31,2020-04-01T00:00:00+00:00,SAMPLE,US,102,synthetic",
        ),
    )
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",
            "2020-03-31,2020-04-01T00:00:00+00:00,SP500TR,US,102,synthetic",
        ),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",
            "2020-03-31,2020-04-01T00:00:00+00:00,USDKRW,FX,1302,synthetic",
        ),
    )

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=(_spec(),),
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert result.common_periods == ("2020-01", "2020-03")
    assert len(result.benchmark_points) == 2


def test_common_periods_across_all_instruments_and_benchmark_are_computed(
    tmp_path: Path,
) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = (
        _spec(
            asset_id="asset_a",
            symbol="SYM_A",
            market="US",
            relative_path="monthly/a_monthly.csv",
        ),
        _spec(
            asset_id="asset_b",
            symbol="SYM_B",
            market="US",
            relative_path="monthly/b_monthly.csv",
        ),
    )
    _write_csv(
        data_root / "monthly/a_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SYM_A,US,100,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,SYM_A,US,101,synthetic",
        ),
    )
    _write_csv(
        data_root / "monthly/b_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SYM_B,US,200,synthetic",
            "2020-03-31,2020-04-01T00:00:00+00:00,SYM_B,US,202,synthetic",
        ),
    )
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,SP500TR,US,101,synthetic",
            "2020-03-31,2020-04-01T00:00:00+00:00,SP500TR,US,102,synthetic",
        ),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        (
            "2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",
            "2020-02-29,2020-03-01T00:00:00+00:00,USDKRW,FX,1301,synthetic",
            "2020-03-31,2020-04-01T00:00:00+00:00,USDKRW,FX,1302,synthetic",
        ),
    )

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert result.common_periods == ("2020-01",)


def test_kodex200_is_not_in_primary_default_specs() -> None:
    specs = default_local_monthly_instrument_specs_for_kospi_primary()
    asset_ids = {spec.asset_id for spec in specs}
    symbols = {spec.symbol for spec in specs}
    assert "KODEX200" not in symbols
    assert asset_ids == {"asset_us", "asset_kr", "asset_gold"}


def test_first_day_of_month_labels_warn(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    _write_csv(
        data_root / "monthly/sample_monthly.csv",
        (
            "2020-01-01,2020-02-01T00:00:00+00:00,SAMPLE,US,100.0,synthetic",
            "2020-02-01,2020-03-01T00:00:00+00:00,SAMPLE,US,101.0,synthetic",
        ),
    )
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        ("2020-01-01,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        ("2020-01-01,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",),
    )

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=(_spec(),),
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    assert any("first day of month" in warning for warning in result.warnings)


def test_result_has_no_nav_fields() -> None:
    fields = set(LocalMonthlyDatasetAssemblyResult.model_fields)
    forbidden = {
        "nav",
        "nav_points",
        "portfolio_value_krw",
        "cash_krw",
        "total_nav_krw",
    }
    assert fields.isdisjoint(forbidden)


def test_result_has_no_benchmark_relative_metrics_fields() -> None:
    fields = set(LocalMonthlyDatasetAssemblyResult.model_fields)
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
    fields = set(LocalMonthlyDatasetAssemblyResult.model_fields)
    forbidden = {
        "markdown_report",
        "report_bundle",
        "report_markdown",
        "rendered_report",
    }
    assert fields.isdisjoint(forbidden)


def test_result_has_no_investment_conclusion_fields() -> None:
    fields = set(LocalMonthlyDatasetAssemblyResult.model_fields)
    forbidden = {
        "recommendation",
        "recommendations",
        "investment_advice",
        "conclusion",
        "project_conclusion",
    }
    assert fields.isdisjoint(forbidden)


def test_result_stores_assembled_records_not_raw_csv_rows(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root)

    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    serialized = result.model_dump_json()
    assert "synthetic" in serialized
    assert "csv.DictReader" not in serialized
    assert all(hasattr(record, "date_id") for record in result.source_records)


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


def test_does_not_write_files() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (".write(", "to_csv", "open(")
    for token in forbidden:
        assert token not in text


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


def test_does_not_import_yfinance_fred_or_network_libraries() -> None:
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


def test_policy_constant_matches_result_model() -> None:
    assert LOCAL_MONTHLY_DATASET_POLICY_V1 == "sibling_local_monthly_csv_dataset.v1"


def test_result_model_is_frozen_and_forbids_extra_fields(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    specs = _write_minimal_dataset(data_root)
    result = assemble_local_monthly_dataset(
        repo_root=repo_root,
        data_root=data_root,
        instrument_specs=specs,
        benchmark_spec=default_local_monthly_benchmark_spec(),
    )

    with pytest.raises(ValidationError):
        result.local_monthly_dataset_policy = "other"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        LocalMonthlyDatasetAssemblyResult(
            local_monthly_dataset_policy=LOCAL_MONTHLY_DATASET_POLICY_V1,
            repo_root=str(repo_root),
            data_root=str(data_root),
            instrument_specs=specs,
            benchmark_spec=default_local_monthly_benchmark_spec(),
            source_records=result.source_records,
            benchmark_points=result.benchmark_points,
            fx_points=result.fx_points,
            common_periods=result.common_periods,
            warnings=(),
            recommendation="forbidden",  # type: ignore[call-arg]
        )


def test_rejects_file_spec_path_escaping_data_root(tmp_path: Path) -> None:
    repo_root, data_root = _layout(tmp_path)
    outside_path = tmp_path / "outside.csv"
    outside_path.write_text(
        HEADER + "\n2020-01-31,2020-02-01T00:00:00+00:00,SAMPLE,US,100,synthetic\n",
        encoding="utf-8",
    )
    linked_path = data_root / "monthly" / "link.csv"
    linked_path.parent.mkdir(parents=True, exist_ok=True)
    linked_path.symlink_to(outside_path)
    _write_csv(
        data_root / "monthly/sp500tr_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,SP500TR,US,100,synthetic",),
    )
    _write_csv(
        data_root / "monthly/usdkrw_monthly.csv",
        ("2020-01-31,2020-02-01T00:00:00+00:00,USDKRW,FX,1300,synthetic",),
    )

    with pytest.raises(ValueError, match="escapes data_root"):
        assemble_local_monthly_dataset(
            repo_root=repo_root,
            data_root=data_root,
            instrument_specs=(_spec(relative_path="monthly/link.csv"),),
            benchmark_spec=default_local_monthly_benchmark_spec(),
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
            if path != "tests/test_backtest_local_dataset.py"
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
