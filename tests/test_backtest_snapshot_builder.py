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
    load_instrument_bars,
)
from backtest_engine import (  # noqa: E402
    BacktestFeatureSnapshot,
    BacktestTargetWeights,
    SnapshotAssetConfig,
    allocate_rules_only_v1,
    build_feature_snapshot_from_source_records,
)
from domain import DateId, DateIdSourceRecord, FactType  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "backtest_data"
INSTRUMENT_CSV = FIXTURES / "instrument_prices_synthetic.csv"
DECISION_TIME = datetime(2020, 1, 4, 6, 30, tzinfo=UTC)
CREATED_AT = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _config(
    asset_id: str = "asset_A",
    *,
    symbol: str = "SYN_US_PROXY",
    market: str = "US",
    long_ma: Decimal = Decimal("99.99"),
    risk_on_weight: Decimal = Decimal("0.70"),
    risk_off_weight: Decimal = Decimal("0.35"),
    min_weight: Decimal = Decimal("0"),
    max_weight: Decimal = Decimal("0.80"),
) -> SnapshotAssetConfig:
    return SnapshotAssetConfig(
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        long_ma=long_ma,
        risk_on_weight=risk_on_weight,
        risk_off_weight=risk_off_weight,
        min_weight=min_weight,
        max_weight=max_weight,
    )


def _record(
    *,
    date_id: str = "200104-1",
    source_timestamp: datetime = DECISION_TIME,
    source_name: str = "synthetic_fixture_v1",
    symbol: str = "SYN_US_PROXY",
    market: str = "US",
    close_adjusted: object = "101.10",
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
    record_fields = {
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
        return DateIdSourceRecord.model_construct(**record_fields)
    return DateIdSourceRecord(**record_fields)


def _snapshot(
    source: InMemoryDateIdSourceReader | tuple[DateIdSourceRecord, ...],
    *,
    asset_configs: tuple[SnapshotAssetConfig, ...] = (_config(),),
    decision_time: datetime = DECISION_TIME,
) -> BacktestFeatureSnapshot:
    return build_feature_snapshot_from_source_records(
        source,
        decision_time=decision_time,
        asset_configs=asset_configs,
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )


def test_builds_snapshot_from_committed_synthetic_fixture_bars_with_whole_slice_conversion() -> None:
    bars = load_instrument_bars(INSTRUMENT_CSV)
    records = instrument_bars_to_source_records(bars, created_at=CREATED_AT)
    reader = InMemoryDateIdSourceReader(records)

    snapshot = _snapshot(
        reader,
        asset_configs=(
            _config("asset_A", symbol="SYN_US_PROXY", market="US", long_ma=Decimal("101")),
            _config(
                "asset_B",
                symbol="SYN_GOLD_PROXY",
                market="GOLD",
                long_ma=Decimal("151"),
                risk_on_weight=Decimal("0.15"),
                risk_off_weight=Decimal("0.05"),
                max_weight=Decimal("0.25"),
            ),
        ),
    )

    assert isinstance(snapshot, BacktestFeatureSnapshot)
    assert [asset.asset_id for asset in snapshot.assets] == ["asset_A", "asset_B"]
    assert all(asset.as_of <= snapshot.decision_time for asset in snapshot.assets)
    assert [asset.current_price for asset in snapshot.assets] == [
        Decimal("101.10"),
        Decimal("151.25"),
    ]


def test_snapshot_uses_generic_asset_ids_without_required_bucket_fields() -> None:
    snapshot = _snapshot((_record(),), asset_configs=(_config(asset_id="asset_alpha"),))

    assert snapshot.assets[0].asset_id == "asset_alpha"
    assert not hasattr(snapshot.assets[0], "kr")
    assert not hasattr(snapshot.assets[0], "us")
    assert not hasattr(snapshot.assets[0], "gold")


def test_future_records_are_excluded_and_boundary_record_is_included() -> None:
    boundary = _record(
        date_id="200104-1",
        source_timestamp=DECISION_TIME,
        close_adjusted="101.10",
    )
    future = _record(
        date_id="200104-2",
        source_timestamp=DECISION_TIME + timedelta(microseconds=1),
        close_adjusted="999.99",
    )

    snapshot = _snapshot((future, boundary))

    assert snapshot.assets[0].current_price == Decimal("101.10")
    assert snapshot.assets[0].as_of == DECISION_TIME


def test_latest_visible_record_is_selected_for_symbol_market() -> None:
    older = _record(
        date_id="200103-1",
        source_timestamp=DECISION_TIME - timedelta(days=1),
        close_adjusted="100.00",
    )
    latest = _record(
        date_id="200104-1",
        source_timestamp=DECISION_TIME,
        close_adjusted="101.10",
    )

    snapshot = _snapshot((older, latest))

    assert snapshot.assets[0].current_price == Decimal("101.10")
    assert snapshot.assets[0].as_of == DECISION_TIME


def test_tie_breaking_is_deterministic_by_date_id_then_source_name() -> None:
    lower_date_id = _record(
        date_id="200104-1",
        source_timestamp=DECISION_TIME,
        source_name="z_source",
        close_adjusted="101.10",
    )
    higher_date_id = _record(
        date_id="200104-2",
        source_timestamp=DECISION_TIME,
        source_name="a_source",
        close_adjusted="102.20",
    )
    higher_source_name = _record(
        date_id="200104-2",
        source_timestamp=DECISION_TIME,
        source_name="z_source",
        close_adjusted="103.30",
    )

    snapshot = _snapshot((higher_date_id, higher_source_name, lower_date_id))

    assert snapshot.assets[0].current_price == Decimal("103.30")


def test_missing_visible_price_for_configured_asset_raises_value_error() -> None:
    with pytest.raises(ValueError, match="no visible price record"):
        _snapshot((_record(symbol="OTHER", market="US"),))


@pytest.mark.parametrize(
    ("payload_overrides", "match"),
    (
        ({"close_adjusted": "not-a-decimal"}, "valid Decimal"),
        ({"close_adjusted": None}, "valid Decimal"),
    ),
)
def test_malformed_payload_raises_value_error(
    payload_overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        _snapshot((_record(payload_overrides=payload_overrides),))


def test_missing_close_adjusted_raises_value_error() -> None:
    record = _record(payload_overrides={"close_adjusted": "101.10"})
    payload = dict(record.payload)
    del payload["close_adjusted"]
    record = record.model_copy(update={"payload": payload})

    with pytest.raises(ValueError, match="close_adjusted"):
        _snapshot((record,))


@pytest.mark.parametrize("close_adjusted", ("0", "-1"))
def test_non_positive_close_adjusted_raises_value_error(close_adjusted: str) -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        _snapshot((_record(close_adjusted=close_adjusted),))


def test_float_close_adjusted_payload_is_rejected() -> None:
    with pytest.raises(ValueError, match="floats are not accepted"):
        _snapshot((_record(close_adjusted=101.10),))


def test_wrong_schema_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="schema_name"):
        _snapshot((_record(schema_name="wrong.schema"),))


def test_record_field_payload_symbol_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="symbol"):
        _snapshot((_record(record_symbol="DIFFERENT"),))


def test_record_field_payload_market_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="market"):
        _snapshot((_record(record_market="DIFFERENT"),))


def test_payload_symbol_market_mismatch_with_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="configured symbol"):
        _snapshot((_record(payload_overrides={"symbol": "DIFFERENT"}),))
    with pytest.raises(ValueError, match="configured market"):
        _snapshot((_record(payload_overrides={"market": "DIFFERENT"}),))


def test_long_ma_is_taken_from_config_not_computed_from_fixture_history() -> None:
    bars = load_instrument_bars(INSTRUMENT_CSV)
    records = instrument_bars_to_source_records(bars, created_at=CREATED_AT)
    configured_long_ma = Decimal("123456.789")

    snapshot = _snapshot(
        InMemoryDateIdSourceReader(records),
        asset_configs=(
            _config(
                asset_id="asset_A",
                symbol="SYN_US_PROXY",
                market="US",
                long_ma=configured_long_ma,
            ),
        ),
    )

    assert snapshot.assets[0].long_ma == configured_long_ma


def test_builder_rejects_naive_decision_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        _snapshot((_record(),), decision_time=datetime(2020, 1, 4, 6, 30))


def test_builder_reads_only_price_records() -> None:
    price = _record(close_adjusted="101.10")
    newer_news = _record(
        fact_type=FactType.NEWS,
        date_id="200104-2",
        close_adjusted="999.99",
    )

    snapshot = _snapshot((price, newer_news))

    assert snapshot.assets[0].current_price == Decimal("101.10")


def test_built_snapshot_can_be_allocated_by_rules_allocator_v1() -> None:
    snapshot = _snapshot((_record(),))

    target = allocate_rules_only_v1(snapshot)

    assert isinstance(target, BacktestTargetWeights)
    assert target.decision_time == snapshot.decision_time
    assert sum(weight.weight for weight in target.weights) == Decimal("1")


def test_builder_produces_no_nav_execution_or_benchmark_scoring() -> None:
    snapshot = _snapshot((_record(),))

    assert isinstance(snapshot, BacktestFeatureSnapshot)
    assert not hasattr(snapshot, "nav")
    assert not hasattr(snapshot, "fills")
    assert not hasattr(snapshot, "benchmark_relative_metrics")


def test_snapshot_builder_has_no_forbidden_imports_or_calls() -> None:
    module_path = Path(__file__).resolve().parents[1] / "src" / "backtest_engine" / "snapshot_builder.py"
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
        "SQLiteDateIdSourceStore",
        ".save_record(",
        "uv run",
        "subprocess",
        "os.system",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots

    for token in forbidden_text:
        assert token not in text
