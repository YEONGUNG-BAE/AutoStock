from __future__ import annotations

import ast
import inspect
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_data import (
    BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    AsOfFilteredSourceView,
    BacktestInstrumentBar,
    InMemoryDateIdSourceReader,
    instrument_bars_to_source_records,
    load_benchmark_krw_unhedged,
    load_instrument_bars,
)
from domain import DateIdSourceRecord, FactType
from scout import ScoutInputBuilder

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "backtest_data"
INSTRUMENT_CSV = FIXTURES / "instrument_prices_synthetic.csv"
SP500_CSV = FIXTURES / "sp500_tr_usd_synthetic.csv"
USDKRW_CSV = FIXTURES / "usdkrw_synthetic.csv"
KST = timezone(timedelta(hours=9))
CREATED_AT = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _bar(
    *,
    bar_date: date = date(2026, 5, 22),
    as_of: datetime = CREATED_AT,
    symbol: str = "SYN_US_PROXY",
    market: str = "US",
    close_adjusted: Decimal = Decimal("100.10"),
    source_name: str = "synthetic_fixture_v1",
) -> BacktestInstrumentBar:
    return BacktestInstrumentBar(
        date=bar_date,
        as_of=as_of,
        symbol=symbol,
        market=market,
        close_adjusted=close_adjusted,
        source_name=source_name,
    )


def test_converts_committed_synthetic_instrument_fixture_bars() -> None:
    bars = load_instrument_bars(INSTRUMENT_CSV)

    records = instrument_bars_to_source_records(bars, created_at=CREATED_AT)

    assert len(records) == len(bars)
    assert all(type(record) is DateIdSourceRecord for record in records)
    records_by_key = {
        (
            record.payload["date"],
            record.symbol,
            record.market,
            record.source_name,
            record.source_timestamp,
        ): record
        for record in records
    }
    for bar in bars:
        record = records_by_key[
            (bar.date.isoformat(), bar.symbol, bar.market, bar.source_name, bar.as_of)
        ]
        assert record.fact_type == FactType.PRICE
        assert record.source_timestamp == bar.as_of
        assert record.created_at == CREATED_AT


def test_created_at_is_explicit_metadata_and_naive_created_at_is_rejected() -> None:
    bar = _bar(as_of=datetime(2026, 5, 22, 10, 0, tzinfo=KST))
    explicit_created_at = datetime(2026, 5, 30, 9, 0, tzinfo=UTC)

    record = instrument_bars_to_source_records((bar,), created_at=explicit_created_at)[0]

    assert record.created_at == explicit_created_at
    assert record.source_timestamp == bar.as_of
    assert record.created_at != record.source_timestamp
    signature = inspect.signature(instrument_bars_to_source_records)
    assert signature.parameters["created_at"].default is inspect.Parameter.empty
    with pytest.raises(ValueError, match="created_at"):
        instrument_bars_to_source_records((bar,), created_at=datetime(2026, 5, 30, 9, 0))


def test_payload_is_json_compatible_and_preserves_decimal_exactness() -> None:
    bar = _bar(close_adjusted=Decimal("100.10"))

    record = instrument_bars_to_source_records((bar,), created_at=CREATED_AT)[0]

    assert record.payload == {
        "close_adjusted": "100.10",
        "date": "2026-05-22",
        "market": "US",
        "schema_name": BACKTEST_INSTRUMENT_PRICE_SCHEMA,
        "symbol": "SYN_US_PROXY",
    }
    assert isinstance(record.payload["close_adjusted"], str)
    assert not isinstance(record.payload["close_adjusted"], float)


def test_symbol_market_source_name_and_summary_are_preserved_unmasked() -> None:
    bar = _bar(symbol="SYN_STOCK_001", market="KR", source_name="offline_source")

    record = instrument_bars_to_source_records((bar,), created_at=CREATED_AT)[0]

    assert record.symbol == "SYN_STOCK_001"
    assert record.market == "KR"
    assert record.source_name == "offline_source"
    assert record.summary == "Synthetic/offline adjusted close for SYN_STOCK_001 on 2026-05-22."


def test_date_id_generation_is_deterministic_independent_of_input_order() -> None:
    bars = (
        _bar(symbol="B", market="US", close_adjusted=Decimal("101")),
        _bar(symbol="A", market="KR", close_adjusted=Decimal("102")),
        _bar(symbol="C", market="GOLD", close_adjusted=Decimal("103")),
    )

    records_a = instrument_bars_to_source_records(bars, created_at=CREATED_AT)
    records_b = instrument_bars_to_source_records(tuple(reversed(bars)), created_at=CREATED_AT)

    assert records_a == records_b
    assert [record.date_id.value for record in records_a] == [
        "260522-1",
        "260522-2",
        "260522-3",
    ]
    assert [(record.market, record.symbol) for record in records_a] == [
        ("GOLD", "C"),
        ("KR", "A"),
        ("US", "B"),
    ]


def test_date_id_sequence_is_deterministic_within_each_date() -> None:
    bars = (
        _bar(bar_date=date(2026, 5, 23), symbol="B", market="US"),
        _bar(bar_date=date(2026, 5, 22), symbol="B", market="US"),
        _bar(bar_date=date(2026, 5, 22), symbol="A", market="US"),
        _bar(bar_date=date(2026, 5, 23), symbol="A", market="US"),
    )

    records = instrument_bars_to_source_records(bars, created_at=CREATED_AT)

    assert [(record.payload["date"], record.symbol, record.date_id.value) for record in records] == [
        ("2026-05-22", "A", "260522-1"),
        ("2026-05-22", "B", "260522-2"),
        ("2026-05-23", "A", "260523-1"),
        ("2026-05-23", "B", "260523-2"),
    ]


def test_duplicate_bars_are_rejected_fail_fast() -> None:
    bar = _bar()

    with pytest.raises(ValueError, match="duplicate instrument bar"):
        instrument_bars_to_source_records((bar, bar), created_at=CREATED_AT)

    changed_close_same_duplicate_key = bar.model_copy(update={"close_adjusted": Decimal("100.11")})
    with pytest.raises(ValueError, match="duplicate instrument bar"):
        instrument_bars_to_source_records(
            (bar, changed_close_same_duplicate_key),
            created_at=CREATED_AT,
        )


def test_output_can_be_wrapped_by_asof_filtered_source_view() -> None:
    bar = _bar(as_of=CREATED_AT)
    records = instrument_bars_to_source_records((bar,), created_at=CREATED_AT)

    view = AsOfFilteredSourceView(records, decision_time=CREATED_AT)

    assert view.list_records() == records


def test_guarded_output_composes_with_unmodified_scout_input_builder() -> None:
    visible = _bar(as_of=CREATED_AT, symbol="VISIBLE")
    future = _bar(as_of=CREATED_AT + timedelta(microseconds=1), symbol="FUTURE")
    records = instrument_bars_to_source_records((future, visible), created_at=CREATED_AT)
    reader = InMemoryDateIdSourceReader(records)
    view = AsOfFilteredSourceView(reader, decision_time=CREATED_AT)

    scout_input = ScoutInputBuilder(view).build_input(universe="US", now=CREATED_AT)

    assert [record.symbol for record in scout_input.records] == ["VISIBLE"]
    assert all(record.source_timestamp <= CREATED_AT for record in scout_input.records)


def test_future_records_are_excluded_and_boundary_records_are_included() -> None:
    boundary = _bar(as_of=CREATED_AT, symbol="BOUNDARY")
    future = _bar(as_of=CREATED_AT + timedelta(seconds=1), symbol="FUTURE")
    records = instrument_bars_to_source_records((future, boundary), created_at=CREATED_AT)

    filtered = AsOfFilteredSourceView(records, decision_time=CREATED_AT).list_records()

    assert [record.symbol for record in filtered] == ["BOUNDARY"]
    assert filtered[0].source_timestamp == CREATED_AT


def test_benchmark_points_are_not_converted_in_this_phase() -> None:
    benchmark = load_benchmark_krw_unhedged(SP500_CSV, USDKRW_CSV)

    with pytest.raises(ValueError, match="BacktestInstrumentBar"):
        instrument_bars_to_source_records(benchmark.benchmark_points, created_at=CREATED_AT)  # type: ignore[arg-type]


def test_in_memory_reader_is_read_only_and_filters_by_fact_type() -> None:
    price = instrument_bars_to_source_records((_bar(),), created_at=CREATED_AT)[0]
    reader = InMemoryDateIdSourceReader((price,))

    assert reader.list_records() == (price,)
    assert reader.list_records(fact_type=FactType.PRICE) == (price,)
    assert reader.list_records(fact_type=FactType.FX) == ()


def test_conversion_module_has_no_forbidden_imports() -> None:
    forbidden = {
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
    module_path = Path(__file__).resolve().parents[1] / "src" / "backtest_data" / "source_records.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not (imported & forbidden), f"forbidden imports found: {imported & forbidden}"


def test_conversion_module_has_no_current_time_or_runtime_store_usage() -> None:
    module_path = Path(__file__).resolve().parents[1] / "src" / "backtest_data" / "source_records.py"
    source = module_path.read_text(encoding="utf-8")

    for token in (
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "SQLiteDateIdSourceStore",
        ".save_record(",
    ):
        assert token not in source
