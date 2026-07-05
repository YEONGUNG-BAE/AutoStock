from __future__ import annotations

import ast
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_data import (  # noqa: E402
    BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    BacktestInstrumentBar,
    InMemoryDateIdSourceReader,
    instrument_bars_to_source_records,
    load_benchmark_krw_unhedged,
    load_instrument_bars,
)
from backtest_engine import (  # noqa: E402
    BacktestFeatureSnapshot,
    ObservationSpacingReport,
    RollingLongMaAssetConfig,
    build_feature_snapshot_from_source_records,
    build_snapshot_configs_with_rolling_long_ma,
    validate_uniform_observation_spacing_for_count_based_ma,
)
from domain import DateId, DateIdSourceRecord, FactType  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "backtest_data"
INSTRUMENT_CSV = FIXTURES / "instrument_prices_synthetic.csv"
SP500_CSV = FIXTURES / "sp500_tr_usd_synthetic.csv"
USDKRW_CSV = FIXTURES / "usdkrw_synthetic.csv"
DECISION_TIME = datetime(2020, 4, 30, 0, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _config(
    asset_id: str = "asset_A",
    *,
    symbol: str = "SYN_US_PROXY",
    market: str = "US",
    lookback_count: int = 3,
) -> RollingLongMaAssetConfig:
    return RollingLongMaAssetConfig(
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        lookback_count=lookback_count,
        risk_on_weight=Decimal("0.70"),
        risk_off_weight=Decimal("0.35"),
        min_weight=Decimal("0"),
        max_weight=Decimal("0.80"),
    )


def _record(
    *,
    date_id: str,
    payload_date: object,
    source_timestamp: datetime,
    source_name: str = "synthetic_fixture_v1",
    symbol: str = "SYN_US_PROXY",
    market: str = "US",
    schema_name: object = BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    fact_type: FactType = FactType.PRICE,
    payload_overrides: dict[str, object] | None = None,
    record_symbol: str | None = None,
    record_market: str | None = None,
) -> DateIdSourceRecord:
    payload: dict[str, object] = {
        "schema_name": schema_name,
        "date": payload_date,
        "symbol": symbol,
        "market": market,
        "close_adjusted": "100",
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=fact_type,
        source_name=source_name,
        source_timestamp=source_timestamp,
        created_at=CREATED_AT,
        summary="synthetic price record",
        payload=payload,
        symbol=symbol if record_symbol is None else record_symbol,
        market=market if record_market is None else record_market,
    )


def _monthly_records(
    *periods: str,
) -> tuple[DateIdSourceRecord, ...]:
    records: list[DateIdSourceRecord] = []
    for index, period in enumerate(periods):
        year_text, month_text = period.split("-")
        date_id = f"{year_text[2:4]}{month_text}15-{index + 1}"
        records.append(
            _record(
                date_id=date_id,
                payload_date=f"{period}-15",
                source_timestamp=datetime(int(year_text), int(month_text), 15, tzinfo=UTC),
            )
        )
    return tuple(records)


def _validate(
    records: tuple[DateIdSourceRecord, ...],
    *,
    asset_configs: tuple[RollingLongMaAssetConfig, ...] = (_config(),),
    decision_time: datetime = DECISION_TIME,
    frequency: str = "monthly",
) -> tuple[ObservationSpacingReport, ...]:
    return validate_uniform_observation_spacing_for_count_based_ma(
        records,
        decision_time=decision_time,
        asset_configs=asset_configs,
        frequency=frequency,  # type: ignore[arg-type]
    )


def test_valid_monthly_spacing_passes_and_returns_report() -> None:
    reports = _validate(_monthly_records("2020-01", "2020-02", "2020-03"))

    assert reports == (
        ObservationSpacingReport(
            asset_id="asset_A",
            symbol="SYN_US_PROXY",
            market="US",
            frequency="monthly",
            lookback_count=3,
            period_keys=("2020-01", "2020-02", "2020-03"),
        ),
    )
    assert not hasattr(reports[0], "kr")
    assert not hasattr(reports[0], "us")
    assert not hasattr(reports[0], "gold")


def test_uses_asof_guard_reads_only_price_and_includes_boundary() -> None:
    visible = _monthly_records("2020-01", "2020-02")
    boundary = _record(
        date_id="200430-1",
        payload_date="2020-03-15",
        source_timestamp=DECISION_TIME,
    )
    future = _record(
        date_id="200501-1",
        payload_date="2020-04-15",
        source_timestamp=DECISION_TIME + timedelta(microseconds=1),
    )
    news = _record(
        date_id="200430-2",
        payload_date="2020-04-15",
        source_timestamp=DECISION_TIME,
        fact_type=FactType.NEWS,
    )

    reports = _validate((*visible, future, news, boundary))

    assert reports[0].period_keys == ("2020-01", "2020-02", "2020-03")


def test_rejects_naive_decision_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        _validate(_monthly_records("2020-01", "2020-02", "2020-03"), decision_time=datetime(2020, 4, 30))


def test_rejects_unsupported_frequency() -> None:
    with pytest.raises(ValueError, match="unsupported observation spacing frequency"):
        _validate(_monthly_records("2020-01", "2020-02", "2020-03"), frequency="weekly")


def test_latest_lookback_count_visible_observations_are_selected() -> None:
    reports = _validate(
        _monthly_records("2019-12", "2020-01", "2020-02", "2020-03"),
        asset_configs=(_config(lookback_count=3),),
    )

    assert reports[0].period_keys == ("2020-01", "2020-02", "2020-03")


def test_duplicate_monthly_period_inside_selected_lookback_raises_value_error() -> None:
    records = _monthly_records("2020-01", "2020-01", "2020-02")

    with pytest.raises(ValueError, match="duplicate period.*asset_A.*SYN_US_PROXY.*US"):
        _validate(records)


def test_skipped_monthly_period_inside_selected_lookback_raises_value_error() -> None:
    records = _monthly_records("2020-01", "2020-03", "2020-04")

    with pytest.raises(ValueError, match="skipped period.*asset_A.*SYN_US_PROXY.*US"):
        _validate(records)


def test_insufficient_visible_observations_raise_value_error() -> None:
    with pytest.raises(ValueError, match="insufficient visible price observations"):
        _validate(_monthly_records("2020-01", "2020-02"))


def test_missing_visible_data_raises_value_error() -> None:
    other = _record(
        date_id="200101-1",
        payload_date="2020-01-15",
        source_timestamp=DECISION_TIME,
        symbol="OTHER",
    )

    with pytest.raises(ValueError, match="no visible price records"):
        _validate((other,))


def test_wrong_schema_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="schema_name"):
        _validate(
            (
                _record(
                    date_id="200101-1",
                    payload_date="2020-01-15",
                    source_timestamp=DECISION_TIME,
                    schema_name="bad",
                ),
            )
        )


def test_missing_payload_date_is_rejected() -> None:
    record = _record(date_id="200101-1", payload_date="2020-01-15", source_timestamp=DECISION_TIME)
    payload = dict(record.payload)
    del payload["date"]
    record = record.model_copy(update={"payload": payload})

    with pytest.raises(ValueError, match="payload date"):
        _validate((record,))


@pytest.mark.parametrize("payload_date", ("bad-date", "2020-13-01"))
def test_malformed_payload_date_is_rejected(payload_date: str) -> None:
    with pytest.raises(ValueError, match="valid ISO date"):
        _validate((_record(date_id="200101-1", payload_date=payload_date, source_timestamp=DECISION_TIME),))


def test_non_string_payload_date_is_rejected() -> None:
    record = _record(
        date_id="200101-1",
        payload_date="2020-01-15",
        source_timestamp=DECISION_TIME,
    )
    record = record.model_copy(update={"payload": {**record.payload, "date": 20200115}})

    with pytest.raises(ValueError, match="payload date must be a string"):
        _validate((record,))


def test_record_field_payload_symbol_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="symbol"):
        _validate(
            (
                _record(
                    date_id="200101-1",
                    payload_date="2020-01-15",
                    source_timestamp=DECISION_TIME,
                    record_symbol="DIFFERENT",
                ),
            )
        )


def test_record_field_payload_market_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="market"):
        _validate(
            (
                _record(
                    date_id="200101-1",
                    payload_date="2020-01-15",
                    source_timestamp=DECISION_TIME,
                    record_market="DIFFERENT",
                ),
            )
        )


def test_payload_symbol_config_symbol_mismatch_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="no visible price records"):
        _validate(
            (
                _record(
                    date_id="200101-1",
                    payload_date="2020-01-15",
                    source_timestamp=DECISION_TIME,
                    symbol="DIFFERENT",
                    payload_overrides={"symbol": "DIFFERENT"},
                ),
            )
        )


def test_payload_market_config_market_mismatch_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="no visible price records"):
        _validate(
            (
                _record(
                    date_id="200101-1",
                    payload_date="2020-01-15",
                    source_timestamp=DECISION_TIME,
                    market="DIFFERENT",
                    payload_overrides={"market": "DIFFERENT"},
                ),
            )
        )


def test_benchmark_points_are_not_consumed() -> None:
    benchmark = load_benchmark_krw_unhedged(SP500_CSV, USDKRW_CSV)

    with pytest.raises(ValueError, match="DateIdSourceRecord"):
        validate_uniform_observation_spacing_for_count_based_ma(
            benchmark.benchmark_points,  # type: ignore[arg-type]
            decision_time=DECISION_TIME,
            asset_configs=(_config(),),
        )


def test_whole_committed_fixture_slice_is_converted_once_and_rejected_for_duplicate_months() -> None:
    bars = load_instrument_bars(INSTRUMENT_CSV)
    records = instrument_bars_to_source_records(bars, created_at=CREATED_AT)

    with pytest.raises(ValueError, match="duplicate period"):
        validate_uniform_observation_spacing_for_count_based_ma(
            records,
            decision_time=datetime(2020, 1, 4, 6, 30, tzinfo=UTC),
            asset_configs=(
                _config(
                    asset_id="asset_A",
                    symbol="SYN_US_PROXY",
                    market="US",
                    lookback_count=2,
                ),
            ),
        )


def test_guard_output_is_compatible_with_rolling_feature_and_snapshot_builder_sequence() -> None:
    bars = (
        BacktestInstrumentBar(
            date=datetime(2020, 1, 31, tzinfo=UTC).date(),
            as_of=datetime(2020, 1, 31, 0, 0, tzinfo=UTC),
            symbol="SYN_US_PROXY",
            market="US",
            close_adjusted=Decimal("100"),
            source_name="monthly_synthetic",
        ),
        BacktestInstrumentBar(
            date=datetime(2020, 2, 29, tzinfo=UTC).date(),
            as_of=datetime(2020, 2, 29, 0, 0, tzinfo=UTC),
            symbol="SYN_US_PROXY",
            market="US",
            close_adjusted=Decimal("102"),
            source_name="monthly_synthetic",
        ),
        BacktestInstrumentBar(
            date=datetime(2020, 3, 31, tzinfo=UTC).date(),
            as_of=datetime(2020, 3, 31, 0, 0, tzinfo=UTC),
            symbol="SYN_US_PROXY",
            market="US",
            close_adjusted=Decimal("104"),
            source_name="monthly_synthetic",
        ),
    )
    records = instrument_bars_to_source_records(bars, created_at=CREATED_AT)
    reader = InMemoryDateIdSourceReader(records)
    rolling_configs = (_config(lookback_count=3),)

    reports = validate_uniform_observation_spacing_for_count_based_ma(
        reader,
        decision_time=DECISION_TIME,
        asset_configs=rolling_configs,
    )
    snapshot_configs = build_snapshot_configs_with_rolling_long_ma(
        reader,
        decision_time=DECISION_TIME,
        asset_configs=rolling_configs,
    )
    snapshot = build_feature_snapshot_from_source_records(
        reader,
        decision_time=DECISION_TIME,
        asset_configs=snapshot_configs,
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )

    assert reports[0].period_keys == ("2020-01", "2020-02", "2020-03")
    assert snapshot_configs[0].long_ma == Decimal("102")
    assert isinstance(snapshot, BacktestFeatureSnapshot)
    assert snapshot.assets[0].long_ma == Decimal("102")


def test_no_decision_artifact_execution_or_scoring_fields_are_produced() -> None:
    reports = _validate(_monthly_records("2020-01", "2020-02", "2020-03"))

    assert isinstance(reports[0], ObservationSpacingReport)
    assert not hasattr(reports[0], "decision_id")
    assert not hasattr(reports[0], "fills")
    assert not hasattr(reports[0], "portfolio_value_series")
    assert not hasattr(reports[0], "benchmark_metrics")


def test_observation_spacing_module_has_no_forbidden_imports_or_calls() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "backtest_engine"
        / "observation_spacing.py"
    )
    text = module_path.read_text(encoding="utf-8")
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
    }
    forbidden_text = {
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "random",
        "numpy.random",
        "SQLiteDateIdSourceStore",
        ".save_record(",
        "uv run",
        "subprocess",
        "os.system",
        "BacktestSingleStepDecision",
        "NAV",
        "benchmark_relative",
        "performance",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots

    for token in forbidden_text:
        assert token not in text
