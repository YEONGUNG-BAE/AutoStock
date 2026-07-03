"""Backtest-only conversion from offline instrument bars to source records."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from backtest_data.models import BacktestInstrumentBar
from domain._datetime import require_timezone_aware_datetime
from domain.identifiers import DateId
from domain.source import DateIdSourceRecord, FactType

BACKTEST_INSTRUMENT_PRICE_SCHEMA = "backtest.instrument_price.v1"


class InMemoryDateIdSourceReader:
    """Small read-only source reader for composing converted records in tests."""

    def __init__(self, records: Iterable[DateIdSourceRecord]) -> None:
        self._records = tuple(records)
        for record in self._records:
            if not isinstance(record, DateIdSourceRecord):
                raise ValueError("records must contain DateIdSourceRecord objects only.")

    def list_records(self, fact_type: FactType | None = None) -> tuple[DateIdSourceRecord, ...]:
        if fact_type is None:
            return self._records
        return tuple(record for record in self._records if record.fact_type == fact_type)


def instrument_bars_to_source_records(
    bars: Iterable[BacktestInstrumentBar],
    *,
    created_at: datetime,
) -> tuple[DateIdSourceRecord, ...]:
    """Convert offline instrument bars into deterministic Date-ID source records.

    ``created_at`` is explicit conversion metadata. It is never used as the
    source timestamp; ``BacktestInstrumentBar.as_of`` is copied to
    ``DateIdSourceRecord.source_timestamp`` exactly.
    """

    aware_created_at = require_timezone_aware_datetime(created_at, field_name="created_at")
    bar_tuple = tuple(bars)
    _validate_instrument_bars_only(bar_tuple)
    _reject_duplicate_bars(bar_tuple)

    sequenced_bars = _bars_with_deterministic_sequence(bar_tuple)
    return tuple(
        _bar_to_source_record(bar, date_sequence=sequence, created_at=aware_created_at)
        for bar, sequence in sequenced_bars
    )


def _validate_instrument_bars_only(bars: tuple[object, ...]) -> None:
    for bar in bars:
        if not isinstance(bar, BacktestInstrumentBar):
            raise ValueError("bars must contain BacktestInstrumentBar objects only.")


def _reject_duplicate_bars(bars: tuple[BacktestInstrumentBar, ...]) -> None:
    seen: set[tuple[object, ...]] = set()
    for bar in bars:
        key = (bar.date, bar.symbol, bar.market, bar.source_name, bar.as_of)
        if key in seen:
            raise ValueError(
                "duplicate instrument bar for "
                f"date={bar.date.isoformat()} symbol={bar.symbol!r} "
                f"market={bar.market!r} source_name={bar.source_name!r} "
                f"as_of={bar.as_of.isoformat()}"
            )
        seen.add(key)


def _bars_with_deterministic_sequence(
    bars: tuple[BacktestInstrumentBar, ...],
) -> tuple[tuple[BacktestInstrumentBar, int], ...]:
    by_date: dict[object, list[BacktestInstrumentBar]] = defaultdict(list)
    for bar in bars:
        by_date[bar.date].append(bar)

    sequenced: list[tuple[BacktestInstrumentBar, int]] = []
    for bar_date in sorted(by_date):
        sorted_bars = sorted(by_date[bar_date], key=_stable_bar_sort_key)
        sequenced.extend((bar, index) for index, bar in enumerate(sorted_bars, start=1))
    return tuple(sequenced)


def _stable_bar_sort_key(bar: BacktestInstrumentBar) -> tuple[str, str, str, str, str, str]:
    return (
        bar.date.isoformat(),
        bar.market,
        bar.symbol,
        bar.source_name,
        bar.as_of.isoformat(),
        str(bar.close_adjusted),
    )


def _bar_to_source_record(
    bar: BacktestInstrumentBar,
    *,
    date_sequence: int,
    created_at: datetime,
) -> DateIdSourceRecord:
    date_id = DateId(f"{bar.date:%y%m%d}-{date_sequence}")
    return DateIdSourceRecord(
        date_id=date_id,
        fact_type=FactType.PRICE,
        source_name=bar.source_name,
        source_timestamp=bar.as_of,
        created_at=created_at,
        summary=f"Synthetic/offline adjusted close for {bar.symbol} on {bar.date.isoformat()}.",
        payload={
            "close_adjusted": str(bar.close_adjusted),
            "date": bar.date.isoformat(),
            "market": bar.market,
            "schema_name": BACKTEST_INSTRUMENT_PRICE_SCHEMA,
            "symbol": bar.symbol,
        },
        symbol=bar.symbol,
        market=bar.market,
    )
