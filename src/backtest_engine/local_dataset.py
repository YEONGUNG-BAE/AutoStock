"""Sibling local monthly CSV dataset assembly for Phase 2d-1.

This module reads operator-local monthly CSV files under a sibling
``autostock-data`` directory and assembles in-memory backtest inputs. It does
not execute backtests, compute NAV, compute benchmark-relative metrics, render
reports, fetch data, or produce investment conclusions.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_data.source_records import BACKTEST_INSTRUMENT_PRICE_SCHEMA
from domain._datetime import parse_timezone_aware_datetime
from domain.identifiers import DateId
from domain.source import DateIdSourceRecord, FactType
from paper_review.models import BenchmarkReturnPoint

LOCAL_MONTHLY_DATASET_POLICY_V1 = "sibling_local_monthly_csv_dataset.v1"

REQUIRED_MONTHLY_COLUMNS = (
    "date",
    "as_of",
    "symbol",
    "market",
    "close_adjusted",
    "source_name",
)

_FIRST_DAY_OF_MONTH_WARNING = (
    "all date values are first day of month; monthly labels may require restamping before execution"
)


class LocalMonthlyInstrumentSpec(BaseModel):
    """Immutable specification for one local monthly instrument CSV."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    symbol: str
    market: str
    relative_path: str

    @field_validator("asset_id", "symbol", "market")
    @classmethod
    def validate_non_empty_fields(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty.")
        return value

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("relative_path must not be empty.")
        path = Path(value)
        if path.is_absolute():
            raise ValueError("relative_path must be relative, not absolute.")
        if ".." in path.parts:
            raise ValueError("relative_path must not contain '..'.")
        return value


class LocalMonthlyBenchmarkSpec(BaseModel):
    """Immutable benchmark CSV path specification for local monthly assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sp500tr_relative_path: str
    usdkrw_relative_path: str

    @field_validator("sp500tr_relative_path", "usdkrw_relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("relative_path must not be empty.")
        path = Path(value)
        if path.is_absolute():
            raise ValueError("relative_path must be relative, not absolute.")
        if ".." in path.parts:
            raise ValueError("relative_path must not contain '..'.")
        return value


class LocalMonthlyDatasetAssemblyResult(BaseModel):
    """Immutable assembled local monthly dataset for downstream backtest phases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_monthly_dataset_policy: Literal["sibling_local_monthly_csv_dataset.v1"]
    repo_root: str
    data_root: str
    instrument_specs: tuple[LocalMonthlyInstrumentSpec, ...]
    benchmark_spec: LocalMonthlyBenchmarkSpec
    source_records: tuple[DateIdSourceRecord, ...]
    benchmark_points: tuple[BenchmarkReturnPoint, ...]
    common_periods: tuple[str, ...]
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if not self.instrument_specs:
            raise ValueError("instrument_specs must not be empty.")
        if not self.source_records:
            raise ValueError("source_records must not be empty.")
        if not self.benchmark_points:
            raise ValueError("benchmark_points must not be empty.")
        if not self.common_periods:
            raise ValueError("common_periods must not be empty.")

        repo_root = Path(self.repo_root).resolve()
        data_root = Path(self.data_root).resolve()
        if _path_is_relative_to(data_root, repo_root):
            raise ValueError("data_root must not be inside repo_root.")
        if _path_is_relative_to(repo_root, data_root):
            raise ValueError("repo_root must not be inside data_root.")

        for previous, current in zip(self.common_periods, self.common_periods[1:], strict=False):
            if previous >= current:
                raise ValueError("common_periods must be strictly increasing.")
        return self


def default_local_monthly_instrument_specs_for_kospi_primary() -> tuple[
    LocalMonthlyInstrumentSpec, ...
]:
    """Return default KOSPI-primary local monthly instrument specs."""
    return (
        LocalMonthlyInstrumentSpec(
            asset_id="asset_us",
            symbol="SP500TR",
            market="US",
            relative_path="monthly/sp500tr_monthly.csv",
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


def default_local_monthly_benchmark_spec() -> LocalMonthlyBenchmarkSpec:
    """Return default local monthly benchmark CSV path spec."""
    return LocalMonthlyBenchmarkSpec(
        sp500tr_relative_path="monthly/sp500tr_monthly.csv",
        usdkrw_relative_path="monthly/usdkrw_monthly.csv",
    )


def assemble_local_monthly_dataset(
    *,
    repo_root: Path,
    data_root: Path | None = None,
    instrument_specs: Iterable[LocalMonthlyInstrumentSpec],
    benchmark_spec: LocalMonthlyBenchmarkSpec,
) -> LocalMonthlyDatasetAssemblyResult:
    """Read sibling local monthly CSVs and assemble in-memory backtest inputs."""
    resolved_repo_root = repo_root.resolve()
    resolved_data_root = (
        resolved_repo_root.parent / "autostock-data"
        if data_root is None
        else data_root.resolve()
    )

    if _path_is_relative_to(resolved_data_root, resolved_repo_root):
        raise ValueError("data_root must not be inside repo_root.")
    if _path_is_relative_to(resolved_repo_root, resolved_data_root):
        raise ValueError("repo_root must not be inside data_root.")

    materialized_specs = tuple(instrument_specs)
    if not materialized_specs:
        raise ValueError("instrument_specs must not be empty.")

    warnings: list[str] = []
    source_records: list[DateIdSourceRecord] = []
    instrument_period_sets: list[set[str]] = []

    for asset_index, spec in enumerate(materialized_specs):
        file_path = _resolve_data_csv_path(
            relative_path=spec.relative_path,
            data_root=resolved_data_root,
            repo_root=resolved_repo_root,
        )
        rows, file_warnings = _read_instrument_csv_rows(path=file_path)
        warnings.extend(file_warnings)

        if rows and all(row.parsed_date.day == 1 for row in rows):
            warnings.append(_FIRST_DAY_OF_MONTH_WARNING)

        period_set = {row.period_key for row in rows}
        instrument_period_sets.append(period_set)

        records = _rows_to_source_records(spec=spec, rows=rows, asset_index=asset_index)
        source_records.extend(records)

    sp500tr_path = _resolve_data_csv_path(
        relative_path=benchmark_spec.sp500tr_relative_path,
        data_root=resolved_data_root,
        repo_root=resolved_repo_root,
    )
    usdkrw_path = _resolve_data_csv_path(
        relative_path=benchmark_spec.usdkrw_relative_path,
        data_root=resolved_data_root,
        repo_root=resolved_repo_root,
    )
    benchmark_points, benchmark_periods, benchmark_warnings = _build_benchmark_points(
        sp500tr_path=sp500tr_path,
        usdkrw_path=usdkrw_path,
    )
    warnings.extend(benchmark_warnings)

    common_periods = _compute_common_periods(
        instrument_period_sets=instrument_period_sets,
        benchmark_periods=benchmark_periods,
    )
    if not common_periods:
        raise ValueError("common_periods must not be empty.")

    return LocalMonthlyDatasetAssemblyResult(
        local_monthly_dataset_policy=LOCAL_MONTHLY_DATASET_POLICY_V1,
        repo_root=str(resolved_repo_root),
        data_root=str(resolved_data_root),
        instrument_specs=materialized_specs,
        benchmark_spec=benchmark_spec,
        source_records=tuple(source_records),
        benchmark_points=benchmark_points,
        common_periods=common_periods,
        warnings=tuple(warnings),
    )


class _InstrumentCsvRow:
    __slots__ = (
        "date_text",
        "parsed_date",
        "period_key",
        "as_of",
        "symbol",
        "market",
        "close_adjusted",
        "source_name",
    )

    def __init__(
        self,
        *,
        date_text: str,
        parsed_date: date,
        period_key: str,
        as_of: datetime,
        symbol: str,
        market: str,
        close_adjusted: Decimal,
        source_name: str,
    ) -> None:
        self.date_text = date_text
        self.parsed_date = parsed_date
        self.period_key = period_key
        self.as_of = as_of
        self.symbol = symbol
        self.market = market
        self.close_adjusted = close_adjusted
        self.source_name = source_name


def _resolve_data_csv_path(
    *,
    relative_path: str,
    data_root: Path,
    repo_root: Path,
) -> Path:
    file_path = (data_root / relative_path).resolve()
    if not _path_is_relative_to(file_path, data_root):
        raise ValueError(f"relative_path escapes data_root: {relative_path}")
    if _path_is_relative_to(file_path, repo_root):
        raise ValueError(
            f"relative_path resolves inside repo_root: {relative_path}"
        )
    return file_path


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _read_instrument_csv_rows(
    *,
    path: Path,
) -> tuple[tuple[_InstrumentCsvRow, ...], tuple[str, ...]]:
    if not path.is_file():
        raise ValueError(f"CSV file not found: {path}")

    content = path.read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(content))
    columns = tuple(reader.fieldnames or ())
    missing_columns = tuple(
        column for column in REQUIRED_MONTHLY_COLUMNS if column not in columns
    )
    if missing_columns:
        raise ValueError(
            f"missing required columns in {path.name}: {', '.join(missing_columns)}"
        )

    rows: list[_InstrumentCsvRow] = []
    for line_number, raw_row in enumerate(reader, start=2):
        if not any(value and value.strip() for value in raw_row.values()):
            continue
        try:
            rows.append(_parse_instrument_csv_row(raw_row, line_number=line_number))
        except ValueError as exc:
            raise ValueError(f"malformed row at line {line_number} in {path.name}: {exc}") from exc

    return tuple(rows), ()


def _parse_instrument_csv_row(row: dict[str, str], *, line_number: int) -> _InstrumentCsvRow:
    date_text = row["date"].strip()
    try:
        parsed_date = date.fromisoformat(date_text)
    except ValueError as exc:
        raise ValueError(f"invalid date: {date_text!r}") from exc

    try:
        as_of = parse_timezone_aware_datetime(row["as_of"].strip(), field_name="as_of")
    except ValueError as exc:
        raise ValueError(f"invalid as_of: {exc}") from exc

    try:
        close_adjusted = Decimal(row["close_adjusted"].strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid close_adjusted: {row['close_adjusted']!r}") from exc
    if not close_adjusted.is_finite() or close_adjusted <= Decimal("0"):
        raise ValueError("close_adjusted must be a positive finite Decimal.")

    return _InstrumentCsvRow(
        date_text=date_text,
        parsed_date=parsed_date,
        period_key=f"{parsed_date.year:04d}-{parsed_date.month:02d}",
        as_of=as_of,
        symbol=row["symbol"].strip(),
        market=row["market"].strip(),
        close_adjusted=close_adjusted,
        source_name=row["source_name"].strip(),
    )


def _rows_to_source_records(
    *,
    spec: LocalMonthlyInstrumentSpec,
    rows: tuple[_InstrumentCsvRow, ...],
    asset_index: int,
) -> tuple[DateIdSourceRecord, ...]:
    sequenced_rows = _rows_with_deterministic_sequence(rows)
    records: list[DateIdSourceRecord] = []
    for row, sequence in sequenced_rows:
        if row.symbol != spec.symbol:
            raise ValueError(
                f"symbol mismatch for {spec.asset_id}: expected {spec.symbol!r}, got {row.symbol!r}"
            )
        if row.market != spec.market:
            raise ValueError(
                f"market mismatch for {spec.asset_id}: expected {spec.market!r}, got {row.market!r}"
            )

        date_id = _deterministic_local_date_id(
            asset_id=spec.asset_id,
            period_key=row.period_key,
            sequence=sequence,
            row_date=row.parsed_date,
            asset_index=asset_index,
        )
        records.append(
            DateIdSourceRecord(
                date_id=date_id,
                fact_type=FactType.PRICE,
                source_name=row.source_name,
                source_timestamp=row.as_of,
                created_at=row.as_of,
                summary="local monthly csv price",
                payload={
                    "schema_name": BACKTEST_INSTRUMENT_PRICE_SCHEMA,
                    "date": row.date_text,
                    "symbol": row.symbol,
                    "market": row.market,
                    "close_adjusted": str(row.close_adjusted),
                },
                symbol=row.symbol,
                market=row.market,
            )
        )
    return tuple(records)


def _rows_with_deterministic_sequence(
    rows: tuple[_InstrumentCsvRow, ...],
) -> tuple[tuple[_InstrumentCsvRow, int], ...]:
    by_period: dict[str, list[_InstrumentCsvRow]] = defaultdict(list)
    for row in rows:
        by_period[row.period_key].append(row)

    sequenced: list[tuple[_InstrumentCsvRow, int]] = []
    for period_key in sorted(by_period):
        sorted_rows = sorted(
            by_period[period_key],
            key=lambda row: (
                row.parsed_date.isoformat(),
                row.symbol,
                row.market,
                row.as_of.isoformat(),
                str(row.close_adjusted),
            ),
        )
        sequenced.extend((row, index) for index, row in enumerate(sorted_rows, start=1))
    return tuple(sequenced)


def _deterministic_local_date_id(
    *,
    asset_id: str,
    period_key: str,
    sequence: int,
    row_date: date,
    asset_index: int,
) -> DateId:
    """Build a deterministic DateId compatible with canonical YYMMDD-N format."""
    _ = asset_id
    _ = period_key
    encoded_sequence = (asset_index + 1) * 100 + sequence
    return DateId(f"{row_date:%y%m%d}-{encoded_sequence}")


def _build_benchmark_points(
    *,
    sp500tr_path: Path,
    usdkrw_path: Path,
) -> tuple[tuple[BenchmarkReturnPoint, ...], set[str], tuple[str, ...]]:
    sp_rows = _read_benchmark_value_rows(path=sp500tr_path)
    fx_rows = _read_benchmark_value_rows(path=usdkrw_path)

    sp_periods = {row.period_key for row in sp_rows.values()}
    fx_periods = {row.period_key for row in fx_rows.values()}
    common_periods = sp_periods & fx_periods

    warnings: list[str] = []
    dropped_sp = sorted(sp_periods - fx_periods)
    dropped_fx = sorted(fx_periods - sp_periods)
    for period in dropped_sp:
        warnings.append(f"missing_fx_for_benchmark_period:{period}")
    for period in dropped_fx:
        warnings.append(f"missing_benchmark_for_fx_period:{period}")
    if dropped_sp or dropped_fx:
        warnings.append(f"dropped_non_common_periods:{len(dropped_sp) + len(dropped_fx)}")

    points: list[BenchmarkReturnPoint] = []
    for period_key in sorted(common_periods):
        sp_row = sp_rows[period_key]
        fx_row = fx_rows[period_key]
        points.append(
            BenchmarkReturnPoint(
                as_of=max(sp_row.as_of, fx_row.as_of),
                total_return_index_value=sp_row.close_adjusted * fx_row.close_adjusted,
            )
        )

    if not points:
        raise ValueError("benchmark_points must not be empty.")

    return tuple(points), common_periods, tuple(warnings)


class _BenchmarkCsvRow:
    __slots__ = ("period_key", "as_of", "close_adjusted")

    def __init__(
        self,
        *,
        period_key: str,
        as_of: datetime,
        close_adjusted: Decimal,
    ) -> None:
        self.period_key = period_key
        self.as_of = as_of
        self.close_adjusted = close_adjusted


def _read_benchmark_value_rows(*, path: Path) -> dict[str, _BenchmarkCsvRow]:
    rows, _ = _read_instrument_csv_rows(path=path)
    parsed: dict[str, _BenchmarkCsvRow] = {}
    for row in rows:
        if row.period_key in parsed:
            raise ValueError(
                f"duplicate benchmark period in {path.name}: {row.period_key}"
            )
        parsed[row.period_key] = _BenchmarkCsvRow(
            period_key=row.period_key,
            as_of=row.as_of,
            close_adjusted=row.close_adjusted,
        )
    return parsed


def _compute_common_periods(
    *,
    instrument_period_sets: list[set[str]],
    benchmark_periods: set[str],
) -> tuple[str, ...]:
    common = set(benchmark_periods)
    for period_set in instrument_period_sets:
        common &= period_set
    return tuple(sorted(common))
