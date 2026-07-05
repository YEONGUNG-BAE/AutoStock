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
    InMemoryDateIdSourceReader,
    instrument_bars_to_source_records,
    load_benchmark_krw_unhedged,
    load_instrument_bars,
)
from backtest_engine import (  # noqa: E402
    BacktestFeatureSnapshot,
    RollingLongMaAssetConfig,
    SnapshotAssetConfig,
    build_feature_snapshot_from_source_records,
    build_snapshot_configs_with_rolling_long_ma,
)
from domain import DateId, DateIdSourceRecord, FactType  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "backtest_data"
INSTRUMENT_CSV = FIXTURES / "instrument_prices_synthetic.csv"
SP500_CSV = FIXTURES / "sp500_tr_usd_synthetic.csv"
USDKRW_CSV = FIXTURES / "usdkrw_synthetic.csv"
DECISION_TIME = datetime(2020, 1, 4, 0, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _config(
    asset_id: str = "asset_A",
    *,
    symbol: str = "SYN_US_PROXY",
    market: str = "US",
    lookback_count: int = 2,
    risk_on_weight: Decimal = Decimal("0.70"),
    risk_off_weight: Decimal = Decimal("0.35"),
    min_weight: Decimal = Decimal("0"),
    max_weight: Decimal = Decimal("0.80"),
) -> RollingLongMaAssetConfig:
    return RollingLongMaAssetConfig(
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        lookback_count=lookback_count,
        risk_on_weight=risk_on_weight,
        risk_off_weight=risk_off_weight,
        min_weight=min_weight,
        max_weight=max_weight,
    )


def _record(
    *,
    date_id: str,
    source_timestamp: datetime,
    source_name: str = "synthetic_fixture_v1",
    symbol: str = "SYN_US_PROXY",
    market: str = "US",
    close_adjusted: object = "100",
    schema_name: object = BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    fact_type: FactType = FactType.PRICE,
    payload_overrides: dict[str, object] | None = None,
    record_symbol: str | None = None,
    record_market: str | None = None,
) -> DateIdSourceRecord:
    payload: dict[str, object] = {
        "schema_name": schema_name,
        "date": "2020-01-04",
        "symbol": symbol,
        "market": market,
        "close_adjusted": close_adjusted,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    fields = {
        "date_id": DateId(date_id),
        "fact_type": fact_type,
        "source_name": source_name,
        "source_timestamp": source_timestamp,
        "created_at": CREATED_AT,
        "summary": "synthetic price record",
        "payload": payload,
        "symbol": symbol if record_symbol is None else record_symbol,
        "market": market if record_market is None else record_market,
    }
    if isinstance(payload.get("close_adjusted"), float):
        return DateIdSourceRecord.model_construct(**fields)
    return DateIdSourceRecord(**fields)


def _records_for_prices(*prices: str) -> tuple[DateIdSourceRecord, ...]:
    base_time = DECISION_TIME - timedelta(days=len(prices))
    return tuple(
        _record(
            date_id=f"20010{index + 1}-1",
            source_timestamp=base_time + timedelta(days=index),
            close_adjusted=price,
        )
        for index, price in enumerate(prices)
    )


def _build_configs(
    records: tuple[DateIdSourceRecord, ...],
    *,
    asset_configs: tuple[RollingLongMaAssetConfig, ...] = (_config(),),
    decision_time: datetime = DECISION_TIME,
) -> tuple[SnapshotAssetConfig, ...]:
    return build_snapshot_configs_with_rolling_long_ma(
        records,
        decision_time=decision_time,
        asset_configs=asset_configs,
    )


def test_builds_snapshot_asset_configs_with_computed_long_ma() -> None:
    configs = _build_configs(_records_for_prices("100", "102", "104"))

    assert isinstance(configs[0], SnapshotAssetConfig)
    assert configs[0].long_ma == Decimal("103")
    assert configs[0].asset_id == "asset_A"
    assert configs[0].symbol == "SYN_US_PROXY"
    assert configs[0].market == "US"


def test_uses_asof_guard_and_reads_only_price_records() -> None:
    price = _record(
        date_id="200103-1",
        source_timestamp=DECISION_TIME - timedelta(days=1),
        close_adjusted="100",
    )
    boundary = _record(
        date_id="200104-1",
        source_timestamp=DECISION_TIME,
        close_adjusted="104",
    )
    future = _record(
        date_id="200105-1",
        source_timestamp=DECISION_TIME + timedelta(microseconds=1),
        close_adjusted="999",
    )
    news = _record(
        date_id="200104-2",
        source_timestamp=DECISION_TIME,
        close_adjusted="999",
        fact_type=FactType.NEWS,
    )

    configs = _build_configs((future, price, news, boundary))

    assert configs[0].long_ma == Decimal("102")


def test_rejects_naive_decision_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        _build_configs(_records_for_prices("100", "102"), decision_time=datetime(2020, 1, 4))


def test_rejects_lookback_count_below_two() -> None:
    with pytest.raises(ValueError, match="lookback_count must be >= 2"):
        _config(lookback_count=1)


def test_uses_latest_lookback_count_visible_observations() -> None:
    configs = _build_configs(
        _records_for_prices("10", "20", "30", "40"),
        asset_configs=(_config(lookback_count=3),),
    )

    assert configs[0].long_ma == Decimal("30")


def test_tie_breaking_is_deterministic_by_timestamp_date_id_source_name() -> None:
    same_time = DECISION_TIME
    records = (
        _record(
            date_id="200104-1",
            source_timestamp=same_time,
            source_name="z_source",
            close_adjusted="100",
        ),
        _record(
            date_id="200104-2",
            source_timestamp=same_time,
            source_name="a_source",
            close_adjusted="200",
        ),
        _record(
            date_id="200104-2",
            source_timestamp=same_time,
            source_name="z_source",
            close_adjusted="300",
        ),
    )

    configs = _build_configs(records, asset_configs=(_config(lookback_count=2),))

    assert configs[0].long_ma == Decimal("250")


def test_insufficient_visible_observations_raise_value_error_without_forward_fill() -> None:
    with pytest.raises(ValueError, match="insufficient visible price observations"):
        _build_configs(_records_for_prices("100"), asset_configs=(_config(lookback_count=2),))


def test_missing_visible_data_raises_value_error() -> None:
    other = _record(
        date_id="200103-1",
        source_timestamp=DECISION_TIME - timedelta(days=1),
        symbol="OTHER",
        market="US",
    )

    with pytest.raises(ValueError, match="no visible price records"):
        _build_configs((other,))


def test_wrong_schema_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="schema_name"):
        _build_configs((_record(date_id="200103-1", source_timestamp=DECISION_TIME, schema_name="bad"),))


def test_missing_close_adjusted_is_rejected() -> None:
    record = _record(date_id="200103-1", source_timestamp=DECISION_TIME)
    payload = dict(record.payload)
    del payload["close_adjusted"]
    record = record.model_copy(update={"payload": payload})

    with pytest.raises(ValueError, match="close_adjusted"):
        _build_configs((record,))


@pytest.mark.parametrize("close_adjusted", ("bad-decimal", None))
def test_malformed_close_adjusted_is_rejected(close_adjusted: object) -> None:
    with pytest.raises(ValueError, match="valid Decimal"):
        _build_configs(
            (
                _record(
                    date_id="200103-1",
                    source_timestamp=DECISION_TIME,
                    close_adjusted=close_adjusted,
                ),
            )
        )


def test_float_close_adjusted_is_rejected() -> None:
    with pytest.raises(ValueError, match="floats are not accepted"):
        _build_configs(
            (
                _record(
                    date_id="200103-1",
                    source_timestamp=DECISION_TIME,
                    close_adjusted=100.1,
                ),
            )
        )


@pytest.mark.parametrize("close_adjusted", ("0", "-1"))
def test_non_positive_close_adjusted_is_rejected(close_adjusted: str) -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        _build_configs(
            (
                _record(
                    date_id="200103-1",
                    source_timestamp=DECISION_TIME,
                    close_adjusted=close_adjusted,
                ),
            )
        )


def test_record_field_payload_symbol_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="symbol"):
        _build_configs(
            (
                _record(
                    date_id="200103-1",
                    source_timestamp=DECISION_TIME,
                    record_symbol="DIFFERENT",
                ),
            )
        )


def test_record_field_payload_market_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="market"):
        _build_configs(
            (
                _record(
                    date_id="200103-1",
                    source_timestamp=DECISION_TIME,
                    record_market="DIFFERENT",
                ),
            )
        )


def test_payload_symbol_config_symbol_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="no visible price records"):
        _build_configs(
            (
                _record(
                    date_id="200103-1",
                    source_timestamp=DECISION_TIME,
                    symbol="DIFFERENT",
                    payload_overrides={"symbol": "DIFFERENT"},
                ),
            )
        )


def test_payload_market_config_market_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="no visible price records"):
        _build_configs(
            (
                _record(
                    date_id="200103-1",
                    source_timestamp=DECISION_TIME,
                    market="DIFFERENT",
                    payload_overrides={"market": "DIFFERENT"},
                ),
            )
        )


def test_generic_asset_ids_are_preserved_without_required_bucket_fields() -> None:
    configs = _build_configs(
        _records_for_prices("100", "102"),
        asset_configs=(_config(asset_id="asset_alpha"),),
    )

    assert configs[0].asset_id == "asset_alpha"
    assert not hasattr(configs[0], "kr")
    assert not hasattr(configs[0], "us")
    assert not hasattr(configs[0], "gold")


def test_returned_configs_can_build_snapshot_with_computed_long_ma() -> None:
    bars = load_instrument_bars(INSTRUMENT_CSV)
    records = instrument_bars_to_source_records(bars, created_at=CREATED_AT)
    reader = InMemoryDateIdSourceReader(records)

    configs = build_snapshot_configs_with_rolling_long_ma(
        reader,
        decision_time=DECISION_TIME,
        asset_configs=(
            _config(
                asset_id="asset_A",
                symbol="SYN_US_PROXY",
                market="US",
                lookback_count=2,
            ),
        ),
    )
    snapshot = build_feature_snapshot_from_source_records(
        reader,
        decision_time=DECISION_TIME,
        asset_configs=configs,
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )

    assert isinstance(snapshot, BacktestFeatureSnapshot)
    assert snapshot.assets[0].long_ma == Decimal("100.675")
    assert configs[0].long_ma == snapshot.assets[0].long_ma


def test_whole_synthetic_fixture_slice_is_converted_once_for_rolling_features() -> None:
    bars = load_instrument_bars(INSTRUMENT_CSV)
    records = instrument_bars_to_source_records(bars, created_at=CREATED_AT)

    configs = build_snapshot_configs_with_rolling_long_ma(
        records,
        decision_time=DECISION_TIME,
        asset_configs=(
            _config(asset_id="asset_A", symbol="SYN_US_PROXY", market="US"),
            _config(asset_id="asset_B", symbol="SYN_GOLD_PROXY", market="GOLD"),
        ),
    )

    assert [config.asset_id for config in configs] == ["asset_A", "asset_B"]
    assert [config.long_ma for config in configs] == [Decimal("100.675"), Decimal("150.875")]


def test_benchmark_points_are_not_consumed() -> None:
    benchmark = load_benchmark_krw_unhedged(SP500_CSV, USDKRW_CSV)

    with pytest.raises(ValueError, match="DateIdSourceRecord"):
        build_snapshot_configs_with_rolling_long_ma(
            benchmark.benchmark_points,  # type: ignore[arg-type]
            decision_time=DECISION_TIME,
            asset_configs=(_config(),),
        )


def test_no_allocator_decision_artifact_nav_execution_fills_or_scoring_is_produced() -> None:
    configs = _build_configs(_records_for_prices("100", "102"))

    assert isinstance(configs[0], SnapshotAssetConfig)
    assert not hasattr(configs[0], "decision_id")
    assert not hasattr(configs[0], "fills")
    assert not hasattr(configs[0], "portfolio_value_series")
    assert not hasattr(configs[0], "benchmark_metrics")


def test_rolling_feature_module_has_no_forbidden_imports_or_calls() -> None:
    module_path = Path(__file__).resolve().parents[1] / "src" / "backtest_engine" / "rolling_features.py"
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
