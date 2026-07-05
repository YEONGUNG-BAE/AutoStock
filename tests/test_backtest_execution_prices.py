from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_data import (  # noqa: E402
    BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    InMemoryDateIdSourceReader,
)
from backtest_engine import (  # noqa: E402
    EXECUTION_PRICE_POLICY_V1,
    BacktestExecutionPrice,
    BacktestExecutionPriceSlice,
    BacktestSingleStepDecision,
    RollingLongMaAssetConfig,
    make_rules_only_single_step_decision,
    select_execution_prices_for_single_step_decision,
)
from domain import DateId, DateIdSourceRecord, FactType  # noqa: E402

DECISION_TIME = datetime(2020, 4, 30, 0, 0, tzinfo=UTC)
INTENDED_EXECUTION_TIME = datetime(2020, 5, 31, 0, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)

SYMBOL_A = "SYN_US_PROXY"
MARKET_A = "US"
SYMBOL_B = "SYN_KR_PROXY"
MARKET_B = "KR"

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "backtest_engine" / "execution_prices.py"
)


def _config(
    asset_id: str = "asset_A",
    *,
    symbol: str = SYMBOL_A,
    market: str = MARKET_A,
    lookback_count: int = 3,
    risk_on_weight: Decimal = Decimal("0.60"),
    risk_off_weight: Decimal = Decimal("0.30"),
    max_weight: Decimal = Decimal("0.80"),
) -> RollingLongMaAssetConfig:
    return RollingLongMaAssetConfig(
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        lookback_count=lookback_count,
        risk_on_weight=risk_on_weight,
        risk_off_weight=risk_off_weight,
        min_weight=Decimal("0"),
        max_weight=max_weight,
    )


def _record(
    *,
    date_id: str,
    payload_date: str,
    source_timestamp: datetime,
    close_adjusted: object,
    source_name: str = "monthly_synthetic",
    symbol: str = SYMBOL_A,
    market: str = MARKET_A,
    schema_name: object = BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    fact_type: FactType = FactType.PRICE,
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=fact_type,
        source_name=source_name,
        source_timestamp=source_timestamp,
        created_at=CREATED_AT,
        summary="synthetic price record",
        payload={
            "schema_name": schema_name,
            "date": payload_date,
            "symbol": symbol,
            "market": market,
            "close_adjusted": close_adjusted,
        },
        symbol=symbol,
        market=market,
    )


def _signal_records(
    *,
    symbol: str = SYMBOL_A,
    market: str = MARKET_A,
    seq_offset: int = 0,
) -> tuple[DateIdSourceRecord, ...]:
    """Three monthly bars ending at/before the decision time (signal side)."""

    periods = (("2020-02", "100"), ("2020-03", "102"), ("2020-04", "104"))
    records: list[DateIdSourceRecord] = []
    for index, (period, close_adjusted) in enumerate(periods):
        year_text, month_text = period.split("-")
        records.append(
            _record(
                date_id=f"{year_text[2:4]}{month_text}28-{seq_offset + index + 1}",
                payload_date=f"{period}-28",
                source_timestamp=datetime(int(year_text), int(month_text), 28, tzinfo=UTC),
                close_adjusted=close_adjusted,
                symbol=symbol,
                market=market,
            )
        )
    return tuple(records)


def _execution_record(
    *,
    symbol: str = SYMBOL_A,
    market: str = MARKET_A,
    source_timestamp: datetime = INTENDED_EXECUTION_TIME,
    close_adjusted: object = "110",
    date_id: str = "200531-9",
    source_name: str = "monthly_synthetic",
    payload_date: str = "2020-05-31",
    schema_name: object = BACKTEST_INSTRUMENT_PRICE_SCHEMA,
) -> DateIdSourceRecord:
    return _record(
        date_id=date_id,
        payload_date=payload_date,
        source_timestamp=source_timestamp,
        close_adjusted=close_adjusted,
        source_name=source_name,
        symbol=symbol,
        market=market,
        schema_name=schema_name,
    )


def _decision(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    asset_configs: tuple[RollingLongMaAssetConfig, ...] = (_config(),),
) -> BacktestSingleStepDecision:
    return make_rules_only_single_step_decision(
        source,
        decision_time=DECISION_TIME,
        intended_execution_time=INTENDED_EXECUTION_TIME,
        rolling_asset_configs=asset_configs,
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )


def _single_asset_setup(
    *execution_records: DateIdSourceRecord,
) -> tuple[BacktestSingleStepDecision, tuple[DateIdSourceRecord, ...]]:
    signal = _signal_records()
    decision = _decision(signal)
    return decision, (*signal, *execution_records)


# 1, 2, 5, 12(default policy)
def test_builds_execution_price_slice_from_valid_decision() -> None:
    decision, records = _single_asset_setup(_execution_record())

    result = select_execution_prices_for_single_step_decision(records, decision=decision)

    assert isinstance(result, BacktestExecutionPriceSlice)
    assert result.decision_time == DECISION_TIME
    assert result.intended_execution_time == INTENDED_EXECUTION_TIME
    assert result.execution_policy == EXECUTION_PRICE_POLICY_V1
    assert len(result.prices) == 1
    price = result.prices[0]
    assert isinstance(price, BacktestExecutionPrice)
    assert price.asset_id == "asset_A"
    assert price.execution_price == Decimal("110")
    assert price.source_timestamp == INTENDED_EXECUTION_TIME
    assert price.source_date == date(2020, 5, 31)


# 2, 3, 4
def test_selects_one_price_per_non_cash_asset_excludes_cash_and_preserves_order() -> None:
    signal_a = _signal_records(symbol=SYMBOL_A, market=MARKET_A, seq_offset=0)
    signal_b = _signal_records(symbol=SYMBOL_B, market=MARKET_B, seq_offset=3)
    decision = _decision(
        (*signal_a, *signal_b),
        asset_configs=(
            _config(
                "asset_A",
                symbol=SYMBOL_A,
                market=MARKET_A,
                risk_on_weight=Decimal("0.40"),
            ),
            _config(
                "asset_B",
                symbol=SYMBOL_B,
                market=MARKET_B,
                risk_on_weight=Decimal("0.40"),
            ),
        ),
    )
    exec_a = _execution_record(symbol=SYMBOL_A, market=MARKET_A, close_adjusted="111")
    exec_b = _execution_record(
        symbol=SYMBOL_B,
        market=MARKET_B,
        close_adjusted="222",
        date_id="200531-8",
    )
    records = (*signal_a, *signal_b, exec_a, exec_b)

    result = select_execution_prices_for_single_step_decision(records, decision=decision)

    assert tuple(price.asset_id for price in result.prices) == ("asset_A", "asset_B")
    assert result.prices[0].execution_price == Decimal("111")
    assert result.prices[1].execution_price == Decimal("222")
    # cash is never selected
    assert "cash" not in {price.asset_id for price in result.prices}
    assert decision.feature_snapshot.cash_asset_id == "cash"


# 5
def test_allows_record_exactly_at_intended_execution_time() -> None:
    decision, records = _single_asset_setup(
        _execution_record(source_timestamp=INTENDED_EXECUTION_TIME)
    )

    result = select_execution_prices_for_single_step_decision(records, decision=decision)

    assert result.prices[0].source_timestamp == INTENDED_EXECUTION_TIME


# 6
def test_rejects_records_before_intended_execution_time() -> None:
    early = _execution_record(
        source_timestamp=INTENDED_EXECUTION_TIME - timedelta(days=1),
        close_adjusted="999",
    )
    boundary = _execution_record(close_adjusted="110")
    decision, records = _single_asset_setup(early, boundary)

    result = select_execution_prices_for_single_step_decision(records, decision=decision)

    assert result.prices[0].execution_price == Decimal("110")


# 7
def test_does_not_select_records_at_decision_time() -> None:
    at_decision = _execution_record(
        source_timestamp=DECISION_TIME,
        close_adjusted="999",
        date_id="200430-7",
    )
    boundary = _execution_record(close_adjusted="110")
    decision, records = _single_asset_setup(at_decision, boundary)

    result = select_execution_prices_for_single_step_decision(records, decision=decision)

    assert result.prices[0].source_timestamp == INTENDED_EXECUTION_TIME
    assert result.prices[0].execution_price == Decimal("110")


# 8
def test_does_not_select_records_between_decision_and_intended_time() -> None:
    between = _execution_record(
        source_timestamp=DECISION_TIME + timedelta(days=5),
        close_adjusted="999",
        date_id="200505-7",
    )
    boundary = _execution_record(close_adjusted="110")
    decision, records = _single_asset_setup(between, boundary)

    result = select_execution_prices_for_single_step_decision(records, decision=decision)

    assert result.prices[0].source_timestamp == INTENDED_EXECUTION_TIME


# 9
def test_selects_earliest_after_intended_time_when_no_boundary_record() -> None:
    later = _execution_record(
        source_timestamp=INTENDED_EXECUTION_TIME + timedelta(days=10),
        close_adjusted="999",
        date_id="200610-7",
        payload_date="2020-06-10",
    )
    earlier = _execution_record(
        source_timestamp=INTENDED_EXECUTION_TIME + timedelta(days=1),
        close_adjusted="110",
        date_id="200601-7",
        payload_date="2020-06-01",
    )
    decision, records = _single_asset_setup(later, earlier)

    result = select_execution_prices_for_single_step_decision(records, decision=decision)

    assert result.prices[0].source_timestamp == INTENDED_EXECUTION_TIME + timedelta(days=1)
    assert result.prices[0].execution_price == Decimal("110")


# 10
def test_tie_breaks_same_timestamp_by_max_date_id_then_source_name() -> None:
    low = _execution_record(
        close_adjusted="100",
        date_id="200531-1",
        source_name="aaa_source",
    )
    high = _execution_record(
        close_adjusted="200",
        date_id="200531-2",
        source_name="aaa_source",
    )
    decision, records = _single_asset_setup(low, high)

    result = select_execution_prices_for_single_step_decision(records, decision=decision)

    # max date_id.value wins: "200531-2" > "200531-1"
    assert result.prices[0].date_id == "200531-2"
    assert result.prices[0].execution_price == Decimal("200")


def test_tie_breaks_same_timestamp_and_date_id_by_max_source_name() -> None:
    low = _execution_record(
        close_adjusted="100",
        date_id="200531-1",
        source_name="aaa_source",
    )
    high = _execution_record(
        close_adjusted="200",
        date_id="200531-1",
        source_name="zzz_source",
    )
    decision, records = _single_asset_setup(low, high)

    result = select_execution_prices_for_single_step_decision(records, decision=decision)

    assert result.prices[0].source_name == "zzz_source"
    assert result.prices[0].execution_price == Decimal("200")


# 11
def test_raises_when_asset_has_no_future_executable_price() -> None:
    decision, records = _single_asset_setup()  # only signal records, all before intended

    with pytest.raises(ValueError, match="no future executable price"):
        select_execution_prices_for_single_step_decision(records, decision=decision)


# 12
def test_raises_on_malformed_matching_payload_schema() -> None:
    bad = _execution_record(schema_name="wrong.schema.v1")
    decision, records = _single_asset_setup(bad)

    with pytest.raises(ValueError, match="schema_name"):
        select_execution_prices_for_single_step_decision(records, decision=decision)


# 13
def test_raises_on_malformed_matching_payload_date() -> None:
    bad = _execution_record(payload_date="not-a-date")
    decision, records = _single_asset_setup(bad)

    with pytest.raises(ValueError, match="ISO parseable date"):
        select_execution_prices_for_single_step_decision(records, decision=decision)


# 14
def test_raises_on_non_positive_matching_price() -> None:
    bad = _execution_record(close_adjusted="0")
    decision, records = _single_asset_setup(bad)

    with pytest.raises(ValueError, match="close_adjusted"):
        select_execution_prices_for_single_step_decision(records, decision=decision)


# 15
def test_raises_on_naive_matching_source_timestamp() -> None:
    naive = DateIdSourceRecord.model_construct(
        date_id=DateId("200531-3"),
        fact_type=FactType.PRICE,
        source_name="monthly_synthetic",
        source_timestamp=datetime(2020, 5, 31, 0, 0),  # naive
        created_at=CREATED_AT,
        summary="synthetic naive record",
        payload={
            "schema_name": BACKTEST_INSTRUMENT_PRICE_SCHEMA,
            "date": "2020-05-31",
            "symbol": SYMBOL_A,
            "market": MARKET_A,
            "close_adjusted": "110",
        },
        symbol=SYMBOL_A,
        market=MARKET_A,
    )
    decision, records = _single_asset_setup(naive)

    with pytest.raises(ValueError, match="timezone-aware"):
        select_execution_prices_for_single_step_decision(records, decision=decision)


# 16
def test_ignores_unrelated_asset_records() -> None:
    unrelated = _execution_record(
        symbol=SYMBOL_B,
        market=MARKET_B,
        close_adjusted="999",
        date_id="200531-6",
    )
    boundary = _execution_record(close_adjusted="110")
    decision, records = _single_asset_setup(unrelated, boundary)

    result = select_execution_prices_for_single_step_decision(records, decision=decision)

    assert len(result.prices) == 1
    assert result.prices[0].execution_price == Decimal("110")


# 17
def test_works_with_in_memory_source_reader() -> None:
    decision, records = _single_asset_setup(_execution_record())
    reader = InMemoryDateIdSourceReader(records)

    result = select_execution_prices_for_single_step_decision(reader, decision=decision)

    assert result.prices[0].execution_price == Decimal("110")


# 18
def test_works_with_one_shot_generator_by_materializing_once() -> None:
    decision, records = _single_asset_setup(_execution_record())

    result = select_execution_prices_for_single_step_decision(
        (record for record in records), decision=decision
    )

    assert result.prices[0].execution_price == Decimal("110")


# 19
def test_execution_price_model_is_frozen_and_forbids_extra_fields() -> None:
    price = BacktestExecutionPrice(
        asset_id="asset_A",
        symbol=SYMBOL_A,
        market=MARKET_A,
        source_date=date(2020, 5, 31),
        source_timestamp=INTENDED_EXECUTION_TIME,
        execution_price=Decimal("110"),
        source_name="monthly_synthetic",
        date_id="200531-9",
    )
    with pytest.raises(ValidationError):
        price.execution_price = Decimal("120")  # type: ignore[misc]

    with pytest.raises(ValidationError):
        BacktestExecutionPrice(
            asset_id="asset_A",
            symbol=SYMBOL_A,
            market=MARKET_A,
            source_date=date(2020, 5, 31),
            source_timestamp=INTENDED_EXECUTION_TIME,
            execution_price=Decimal("110"),
            source_name="monthly_synthetic",
            date_id="200531-9",
            quantity=Decimal("10"),  # type: ignore[call-arg]
        )


def test_slice_model_is_frozen_and_forbids_extra_fields() -> None:
    price = BacktestExecutionPrice(
        asset_id="asset_A",
        symbol=SYMBOL_A,
        market=MARKET_A,
        source_date=date(2020, 5, 31),
        source_timestamp=INTENDED_EXECUTION_TIME,
        execution_price=Decimal("110"),
        source_name="monthly_synthetic",
        date_id="200531-9",
    )
    slice_ = BacktestExecutionPriceSlice(
        decision_time=DECISION_TIME,
        intended_execution_time=INTENDED_EXECUTION_TIME,
        execution_policy=EXECUTION_PRICE_POLICY_V1,
        prices=(price,),
    )
    with pytest.raises(ValidationError):
        slice_.prices = ()  # type: ignore[misc]

    with pytest.raises(ValidationError):
        BacktestExecutionPriceSlice(
            decision_time=DECISION_TIME,
            intended_execution_time=INTENDED_EXECUTION_TIME,
            execution_policy=EXECUTION_PRICE_POLICY_V1,
            prices=(price,),
            nav=Decimal("100"),  # type: ignore[call-arg]
        )


def _price(
    *,
    asset_id: str = "asset_A",
    source_timestamp: datetime = INTENDED_EXECUTION_TIME,
) -> BacktestExecutionPrice:
    return BacktestExecutionPrice(
        asset_id=asset_id,
        symbol=SYMBOL_A,
        market=MARKET_A,
        source_date=date(2020, 5, 31),
        source_timestamp=source_timestamp,
        execution_price=Decimal("110"),
        source_name="monthly_synthetic",
        date_id="200531-9",
    )


# 20
def test_slice_rejects_naive_decision_time() -> None:
    with pytest.raises(ValidationError):
        BacktestExecutionPriceSlice(
            decision_time=datetime(2020, 4, 30, 0, 0),  # naive
            intended_execution_time=INTENDED_EXECUTION_TIME,
            execution_policy=EXECUTION_PRICE_POLICY_V1,
            prices=(_price(),),
        )


# 21
def test_slice_rejects_naive_intended_execution_time() -> None:
    with pytest.raises(ValidationError):
        BacktestExecutionPriceSlice(
            decision_time=DECISION_TIME,
            intended_execution_time=datetime(2020, 5, 31, 0, 0),  # naive
            execution_policy=EXECUTION_PRICE_POLICY_V1,
            prices=(_price(),),
        )


# 22
def test_slice_rejects_decision_time_not_before_intended() -> None:
    with pytest.raises(ValidationError):
        BacktestExecutionPriceSlice(
            decision_time=INTENDED_EXECUTION_TIME,
            intended_execution_time=INTENDED_EXECUTION_TIME,
            execution_policy=EXECUTION_PRICE_POLICY_V1,
            prices=(_price(),),
        )


# 23
def test_slice_rejects_duplicate_asset_ids() -> None:
    with pytest.raises(ValidationError):
        BacktestExecutionPriceSlice(
            decision_time=DECISION_TIME,
            intended_execution_time=INTENDED_EXECUTION_TIME,
            execution_policy=EXECUTION_PRICE_POLICY_V1,
            prices=(_price(asset_id="asset_A"), _price(asset_id="asset_A")),
        )


# 24
def test_slice_rejects_price_before_intended_execution_time() -> None:
    with pytest.raises(ValidationError):
        BacktestExecutionPriceSlice(
            decision_time=DECISION_TIME,
            intended_execution_time=INTENDED_EXECUTION_TIME,
            execution_policy=EXECUTION_PRICE_POLICY_V1,
            prices=(_price(source_timestamp=INTENDED_EXECUTION_TIME - timedelta(days=1)),),
        )


def test_slice_rejects_empty_prices() -> None:
    with pytest.raises(ValidationError):
        BacktestExecutionPriceSlice(
            decision_time=DECISION_TIME,
            intended_execution_time=INTENDED_EXECUTION_TIME,
            execution_policy=EXECUTION_PRICE_POLICY_V1,
            prices=(),
        )


# 25
def test_models_have_no_trade_or_portfolio_fields() -> None:
    forbidden = {
        "quantity",
        "target_quantity",
        "order_id",
        "order",
        "fill",
        "fills",
        "cost",
        "transaction_cost",
        "slippage",
        "holdings",
        "cash",
        "cash_ledger",
        "nav",
        "portfolio_value_series",
        "benchmark",
        "benchmark_relative_metrics",
        "performance",
    }
    price_fields = set(BacktestExecutionPrice.model_fields)
    slice_fields = set(BacktestExecutionPriceSlice.model_fields)
    assert price_fields.isdisjoint(forbidden)
    assert slice_fields.isdisjoint(forbidden)


# 26, 27, 28 — static scans on the new module
def test_module_does_not_import_asof_view_or_forbidden_names() -> None:
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
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots

    forbidden_text = (
        "AsOfFilteredSourceView",
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "random",
        "numpy.random",
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
    )
    for token in forbidden_text:
        assert token not in text, f"forbidden token present: {token}"


# 29
def test_existing_single_step_decision_tests_still_importable() -> None:
    # Sanity: the composed decision still builds with the shared fixtures.
    decision = _decision(_signal_records())
    assert isinstance(decision, BacktestSingleStepDecision)
    assert decision.decision_time < decision.intended_execution_time
