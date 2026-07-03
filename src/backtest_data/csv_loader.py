"""Phase 2a offline CSV loaders (benchmark role + instrument role).

Phase 2a is strategy-agnostic. It preserves original symbols, markets,
timestamps, source names, and value fields. It does not implement LLM input
masking, strategy execution, benchmark scoring, derived feature fitting, or
normalization. A later Phase 2c LLM input adapter may create anonymized or
feature-rich masked views from these preserved original fields.

Loader rules:
- reads local CSV only: no network, no current time, no config access
- parses Decimal values exactly from strings
- fail-fast validation; duplicates are rejected, never silently resolved
- ``date`` is the normalized alignment key; ``as_of`` is the source
  availability timestamp (look-ahead safety key); naive ``as_of`` is rejected
- benchmark/FX alignment uses common ``date`` values only: no forward-fill,
  no interpolation; dropped non-common dates produce deterministic warnings
"""

from __future__ import annotations

import csv
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from backtest_data.models import BacktestBenchmarkLoadResult, BacktestInstrumentBar
from domain._datetime import parse_timezone_aware_datetime
from paper_review.models import BenchmarkReturnPoint

_BENCHMARK_COLUMNS = ("date", "as_of", "sp500_tr_usd", "source_name")
_FX_COLUMNS = ("date", "as_of", "usdkrw", "source_name")
_INSTRUMENT_COLUMNS = ("date", "as_of", "symbol", "market", "close_adjusted", "source_name")


def load_benchmark_krw_unhedged(
    sp500_tr_usd_csv_path: str | Path,
    usdkrw_csv_path: str | Path,
) -> BacktestBenchmarkLoadResult:
    """S&P 500 TR(USD)와 USDKRW fixture를 읽어 KRW-unhedged benchmark series를 만든다.

    Conversion is exact Decimal arithmetic on common dates only:

        sp500_tr_krw_level(t) = sp500_tr_usd_level(t) * usdkrw(t)

    The resulting ``BenchmarkReturnPoint.total_return_index_value`` is
    KRW-unhedged and is for scoring only; the bot never trades this series.
    Each point's ``as_of`` is stamped conservatively as the LATER of the two
    source ``as_of`` timestamps (data is only known once both are known).
    Non-common dates are dropped with deterministic warnings; no
    forward-fill, no interpolation.
    """
    sp_rows = _read_dated_value_csv(
        sp500_tr_usd_csv_path,
        columns=_BENCHMARK_COLUMNS,
        value_column="sp500_tr_usd",
    )
    fx_rows = _read_dated_value_csv(
        usdkrw_csv_path,
        columns=_FX_COLUMNS,
        value_column="usdkrw",
    )

    common_dates = sorted(set(sp_rows) & set(fx_rows))
    sp_only = sorted(set(sp_rows) - set(fx_rows))
    fx_only = sorted(set(fx_rows) - set(sp_rows))

    warnings: list[str] = []
    for missing in sp_only:
        warnings.append(f"missing_fx_for_benchmark_date:{missing.isoformat()}")
    for missing in fx_only:
        warnings.append(f"missing_benchmark_for_fx_date:{missing.isoformat()}")
    dropped = len(sp_only) + len(fx_only)
    if dropped:
        warnings.append(f"dropped_non_common_dates:{dropped}")

    points: list[BenchmarkReturnPoint] = []
    for aligned_date in common_dates:
        sp_as_of, sp_level = sp_rows[aligned_date]
        fx_as_of, fx_rate = fx_rows[aligned_date]
        points.append(
            BenchmarkReturnPoint(
                as_of=max(sp_as_of, fx_as_of),
                total_return_index_value=sp_level * fx_rate,
            )
        )

    return BacktestBenchmarkLoadResult(
        benchmark_points=tuple(points),
        warnings=tuple(warnings),
    )


def load_instrument_bars(csv_path: str | Path) -> tuple[BacktestInstrumentBar, ...]:
    """Instrument adjusted-close fixture CSV를 neutral instrument model로 읽는다.

    Rows may represent asset-class proxy instruments or individual
    securities; no asset-class-only assumption is made. Original date,
    as_of, symbol, market, close_adjusted, and source_name are preserved
    unmasked. Rows are NOT wired into the runtime store and are NOT
    converted to DateIdSourceRecord in this phase.
    """
    rows = _read_csv_rows(csv_path, columns=_INSTRUMENT_COLUMNS)

    bars: list[BacktestInstrumentBar] = []
    seen: set[tuple[date_type, str, str]] = set()
    for line_number, row in rows:
        row_date = _parse_date(row["date"], line_number=line_number)
        bar = _build_instrument_bar(row, row_date=row_date, line_number=line_number)
        key = (bar.date, bar.symbol, bar.market)
        if key in seen:
            raise ValueError(
                f"duplicate instrument (date, symbol, market) at line {line_number}: "
                f"({bar.date.isoformat()}, {bar.symbol}, {bar.market})"
            )
        seen.add(key)
        bars.append(bar)
    return tuple(bars)


def _build_instrument_bar(
    row: dict[str, str],
    *,
    row_date: date_type,
    line_number: int,
) -> BacktestInstrumentBar:
    try:
        return BacktestInstrumentBar(
            date=row_date,
            as_of=_parse_as_of(row["as_of"], line_number=line_number),
            symbol=row["symbol"],
            market=row["market"],
            close_adjusted=_parse_positive_decimal(
                row["close_adjusted"],
                field_name="close_adjusted",
                line_number=line_number,
            ),
            source_name=row["source_name"],
        )
    except ValueError as exc:
        raise ValueError(f"malformed instrument row at line {line_number}: {exc}") from exc


def _read_dated_value_csv(
    csv_path: str | Path,
    *,
    columns: tuple[str, ...],
    value_column: str,
) -> dict[date_type, tuple[datetime, Decimal]]:
    """date를 key로 (as_of, value)를 반환한다. 중복 date는 fail-fast로 거부한다."""
    rows = _read_csv_rows(csv_path, columns=columns)

    parsed: dict[date_type, tuple[datetime, Decimal]] = {}
    for line_number, row in rows:
        row_date = _parse_date(row["date"], line_number=line_number)
        if row_date in parsed:
            raise ValueError(
                f"duplicate date at line {line_number}: {row_date.isoformat()} in {value_column} series"
            )
        as_of = _parse_as_of(row["as_of"], line_number=line_number)
        value = _parse_positive_decimal(
            row[value_column],
            field_name=value_column,
            line_number=line_number,
        )
        # source_name is validated non-blank; preserved in the source CSV and
        # available for later phases (points carry conservative as_of).
        _require_non_blank(row["source_name"], field_name="source_name", line_number=line_number)
        parsed[row_date] = (as_of, value)
    return parsed


def _read_csv_rows(
    csv_path: str | Path,
    *,
    columns: tuple[str, ...],
) -> list[tuple[int, dict[str, str]]]:
    path = Path(csv_path)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"empty CSV file: {path.name}") from exc
        if tuple(column.strip() for column in header) != columns:
            raise ValueError(
                f"unexpected CSV header in {path.name}: expected {list(columns)}, got {header}"
            )
        rows: list[tuple[int, dict[str, str]]] = []
        for line_number, raw_row in enumerate(reader, start=2):
            if not raw_row:
                continue
            if len(raw_row) != len(columns):
                raise ValueError(
                    f"malformed row at line {line_number} in {path.name}: "
                    f"expected {len(columns)} columns, got {len(raw_row)}"
                )
            rows.append((line_number, dict(zip(columns, raw_row))))
    return rows


def _parse_date(value: str, *, line_number: int) -> date_type:
    try:
        return date_type.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"malformed date at line {line_number}: {value!r}") from exc


def _parse_as_of(value: str, *, line_number: int) -> datetime:
    try:
        return parse_timezone_aware_datetime(value.strip(), field_name="as_of")
    except ValueError as exc:
        raise ValueError(f"invalid as_of at line {line_number}: {exc}") from exc


def _parse_positive_decimal(value: str, *, field_name: str, line_number: int) -> Decimal:
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"malformed {field_name} at line {line_number}: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite at line {line_number}: {value!r}")
    if parsed <= Decimal("0"):
        raise ValueError(f"{field_name} must be greater than 0 at line {line_number}: {value!r}")
    return parsed


def _require_non_blank(value: str, *, field_name: str, line_number: int) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank at line {line_number}.")
    return stripped
