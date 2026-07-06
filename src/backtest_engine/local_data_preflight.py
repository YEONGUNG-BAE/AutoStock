"""Sibling local CSV metadata preflight for Phase 2d-0.

This module inspects operator-local monthly CSV files under a sibling
``autostock-data`` directory. It returns metadata and warnings only. It does not
execute backtests, compute NAV, compute benchmark-relative metrics, render
reports, fetch data, or produce investment conclusions.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

LOCAL_DATA_PREFLIGHT_POLICY_V1 = "sibling_local_csv_preflight.v1"


class LocalDataFileSpec(BaseModel):
    """Immutable specification for one local monthly CSV file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_name: str
    relative_path: str
    required_columns: tuple[str, ...]
    expected_symbol: str | None = None
    expected_market: str | None = None

    @field_validator("logical_name")
    @classmethod
    def validate_logical_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("logical_name must not be empty.")
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

    @field_validator("required_columns")
    @classmethod
    def validate_required_columns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("required_columns must not be empty.")
        return value


class LocalDataFilePreflightResult(BaseModel):
    """Immutable metadata preflight result for one local CSV file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_name: str
    path: str
    exists: bool
    row_count: int
    columns: tuple[str, ...]
    missing_columns: tuple[str, ...]
    duplicate_period_count: int
    first_period: str | None
    last_period: str | None
    missing_periods: tuple[str, ...]
    symbol_values: tuple[str, ...]
    market_values: tuple[str, ...]
    warnings: tuple[str, ...]

    @field_validator("row_count")
    @classmethod
    def validate_row_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("row_count must be >= 0.")
        return value

    @field_validator("duplicate_period_count")
    @classmethod
    def validate_duplicate_period_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("duplicate_period_count must be >= 0.")
        return value


class LocalDataPreflightResult(BaseModel):
    """Immutable aggregate local-data preflight result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_data_preflight_policy: Literal["sibling_local_csv_preflight.v1"]
    repo_root: str
    data_root: str
    files: tuple[LocalDataFilePreflightResult, ...]
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if not self.files:
            raise ValueError("files must not be empty.")
        repo_root = Path(self.repo_root).resolve()
        data_root = Path(self.data_root).resolve()
        if _path_is_relative_to(data_root, repo_root):
            raise ValueError("data_root must not be inside repo_root.")
        if _path_is_relative_to(repo_root, data_root):
            raise ValueError("repo_root must not be inside data_root.")
        return self


def default_monthly_local_data_file_specs() -> tuple[LocalDataFileSpec, ...]:
    """Return default monthly CSV file specs for sibling local data preflight."""
    required_columns = (
        "date",
        "as_of",
        "symbol",
        "market",
        "close_adjusted",
        "source_name",
    )
    return (
        LocalDataFileSpec(
            logical_name="sp500tr_monthly",
            relative_path="monthly/sp500tr_monthly.csv",
            required_columns=required_columns,
            expected_symbol="SP500TR",
            expected_market="US",
        ),
        LocalDataFileSpec(
            logical_name="usdkrw_monthly",
            relative_path="monthly/usdkrw_monthly.csv",
            required_columns=required_columns,
            expected_symbol="USDKRW",
            expected_market="FX",
        ),
        LocalDataFileSpec(
            logical_name="kospi_monthly",
            relative_path="monthly/kospi_monthly.csv",
            required_columns=required_columns,
            expected_symbol="KOSPI",
            expected_market="KR",
        ),
        LocalDataFileSpec(
            logical_name="gld_monthly",
            relative_path="monthly/gld_monthly.csv",
            required_columns=required_columns,
            expected_symbol="GLD",
            expected_market="US",
        ),
        LocalDataFileSpec(
            logical_name="kodex200_monthly",
            relative_path="monthly/kodex200_monthly.csv",
            required_columns=required_columns,
            expected_symbol="KODEX200",
            expected_market="KR",
        ),
    )


def run_local_data_preflight(
    *,
    repo_root: Path,
    data_root: Path | None = None,
    file_specs: Iterable[LocalDataFileSpec],
) -> LocalDataPreflightResult:
    """Inspect sibling local CSV metadata and return warnings-only preflight result."""
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

    materialized_specs = tuple(file_specs)
    if not materialized_specs:
        raise ValueError("file_specs must not be empty.")

    file_results: list[LocalDataFilePreflightResult] = []
    aggregate_warnings: list[str] = []

    for spec in materialized_specs:
        file_path = (resolved_data_root / spec.relative_path).resolve()
        if not _path_is_relative_to(file_path, resolved_data_root):
            raise ValueError(
                f"file spec relative_path escapes data_root: {spec.relative_path}"
            )
        if _path_is_relative_to(file_path, resolved_repo_root):
            raise ValueError(
                f"file spec relative_path resolves inside repo_root: {spec.relative_path}"
            )

        if not file_path.is_file():
            warning = f"file missing: {spec.logical_name}"
            file_results.append(
                LocalDataFilePreflightResult(
                    logical_name=spec.logical_name,
                    path=str(file_path),
                    exists=False,
                    row_count=0,
                    columns=(),
                    missing_columns=spec.required_columns,
                    duplicate_period_count=0,
                    first_period=None,
                    last_period=None,
                    missing_periods=(),
                    symbol_values=(),
                    market_values=(),
                    warnings=(warning,),
                )
            )
            aggregate_warnings.append(warning)
            continue

        file_results.append(_inspect_local_csv_file(spec=spec, path=file_path))

    return LocalDataPreflightResult(
        local_data_preflight_policy=LOCAL_DATA_PREFLIGHT_POLICY_V1,
        repo_root=str(resolved_repo_root),
        data_root=str(resolved_data_root),
        files=tuple(file_results),
        warnings=tuple(aggregate_warnings),
    )


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _inspect_local_csv_file(
    *,
    spec: LocalDataFileSpec,
    path: Path,
) -> LocalDataFilePreflightResult:
    warnings: list[str] = []

    content = path.read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(content))
    columns = tuple(reader.fieldnames or ())
    rows = list(reader)

    missing_columns = tuple(
        column for column in spec.required_columns if column not in columns
    )
    if missing_columns:
        warnings.append(
            f"missing required columns: {', '.join(missing_columns)}"
        )

    if "date" not in columns:
        warnings.append("date column is missing")

    if "as_of" not in columns:
        warnings.append("as_of column is missing")

    period_keys: list[str] = []
    symbol_values: set[str] = set()
    market_values: set[str] = set()
    parsed_dates: list[datetime] = []
    suspicious_as_of_count = 0
    naive_or_unparseable_as_of_count = 0
    first_day_of_month_count = 0

    for row in rows:
        if "symbol" in columns and row.get("symbol"):
            symbol_values.add(row["symbol"].strip())
        if "market" in columns and row.get("market"):
            market_values.add(row["market"].strip())

        if "date" in columns and row.get("date"):
            date_text = row["date"].strip()
            period_key = _month_period_key_from_date_text(date_text)
            if period_key is not None:
                period_keys.append(period_key)
            parsed_date = _parse_date_text(date_text)
            if parsed_date is not None:
                parsed_dates.append(parsed_date)
                if parsed_date.day == 1:
                    first_day_of_month_count += 1

        if "as_of" in columns and row.get("as_of"):
            as_of_text = row["as_of"].strip()
            parsed_as_of = _parse_as_of_text(as_of_text)
            if parsed_as_of is None:
                naive_or_unparseable_as_of_count += 1
            elif parsed_as_of.tzinfo is None or parsed_as_of.tzinfo.utcoffset(parsed_as_of) is None:
                naive_or_unparseable_as_of_count += 1
            elif "date" in columns and row.get("date"):
                parsed_date = _parse_date_text(row["date"].strip())
                if parsed_date is not None and parsed_as_of.date() < parsed_date.date():
                    suspicious_as_of_count += 1

    duplicate_period_count = max(0, len(period_keys) - len(set(period_keys)))
    ordered_periods = sorted(set(period_keys))
    first_period = ordered_periods[0] if ordered_periods else None
    last_period = ordered_periods[-1] if ordered_periods else None
    missing_periods = (
        tuple(_missing_monthly_periods(first_period, last_period, set(period_keys)))
        if first_period is not None and last_period is not None
        else ()
    )

    if parsed_dates and first_day_of_month_count == len(parsed_dates):
        warnings.append(
            "all date values are first day of month; monthly labels may require restamping"
        )

    if naive_or_unparseable_as_of_count > 0:
        warnings.append(
            f"as_of has {naive_or_unparseable_as_of_count} naive or unparseable value(s)"
        )

    if suspicious_as_of_count > 0:
        warnings.append(
            f"as_of appears before corresponding date for {suspicious_as_of_count} row(s)"
        )

    if spec.expected_symbol is not None and symbol_values:
        if spec.expected_symbol not in symbol_values:
            warnings.append(
                f"expected symbol mismatch: expected {spec.expected_symbol}, found {sorted(symbol_values)}"
            )

    if spec.expected_market is not None and market_values:
        if spec.expected_market not in market_values:
            warnings.append(
                f"expected market mismatch: expected {spec.expected_market}, found {sorted(market_values)}"
            )

    return LocalDataFilePreflightResult(
        logical_name=spec.logical_name,
        path=str(path),
        exists=True,
        row_count=len(rows),
        columns=columns,
        missing_columns=missing_columns,
        duplicate_period_count=duplicate_period_count,
        first_period=first_period,
        last_period=last_period,
        missing_periods=missing_periods,
        symbol_values=tuple(sorted(symbol_values)),
        market_values=tuple(sorted(market_values)),
        warnings=tuple(warnings),
    )


def _month_period_key_from_date_text(date_text: str) -> str | None:
    parsed = _parse_date_text(date_text)
    if parsed is not None:
        return f"{parsed.year:04d}-{parsed.month:02d}"

    if len(date_text) >= 7 and date_text[4] == "-":
        year_text = date_text[:4]
        month_text = date_text[5:7]
        if year_text.isdigit() and month_text.isdigit():
            return f"{int(year_text):04d}-{int(month_text):02d}"

    return None


def _parse_date_text(date_text: str) -> datetime | None:
    normalized = date_text.strip()
    if not normalized:
        return None

    for parser in (
        lambda value: datetime.fromisoformat(value),
        lambda value: datetime.strptime(value, "%Y-%m-%d"),
        lambda value: datetime.strptime(value, "%Y-%m"),
    ):
        try:
            return parser(normalized)
        except ValueError:
            continue

    return None


def _parse_as_of_text(as_of_text: str) -> datetime | None:
    normalized = as_of_text.strip()
    if not normalized:
        return None

    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(normalized, fmt)
            except ValueError:
                continue

    return None


def _missing_monthly_periods(
    first_period: str,
    last_period: str,
    observed_periods: set[str],
) -> list[str]:
    start_year, start_month = map(int, first_period.split("-"))
    end_year, end_month = map(int, last_period.split("-"))

    year = start_year
    month = start_month
    missing: list[str] = []

    while (year, month) <= (end_year, end_month):
        period = f"{year:04d}-{month:02d}"
        if period not in observed_periods:
            missing.append(period)
        month += 1
        if month > 12:
            month = 1
            year += 1

    return missing
