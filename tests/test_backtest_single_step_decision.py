from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
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
    RULES_ALLOCATOR_V1,
    BacktestSingleStepDecision,
    RollingLongMaAssetConfig,
    build_single_step_rules_decision,
    make_rules_only_single_step_decision,
)
from domain import DateId, DateIdSourceRecord, FactType  # noqa: E402

DECISION_TIME = datetime(2020, 4, 30, 0, 0, tzinfo=UTC)
INTENDED_EXECUTION_TIME = DECISION_TIME + timedelta(days=1)
CREATED_AT = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _config(
    asset_id: str = "asset_A",
    *,
    symbol: str = "SYN_US_PROXY",
    market: str = "US",
    lookback_count: int = 3,
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
    payload_date: str,
    source_timestamp: datetime,
    close_adjusted: object,
    source_name: str = "monthly_synthetic",
    symbol: str = "SYN_US_PROXY",
    market: str = "US",
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


def _monthly_records(
    prices_by_period: tuple[tuple[str, str], ...] = (
        ("2020-01", "100"),
        ("2020-02", "102"),
        ("2020-03", "104"),
    ),
    *,
    symbol: str = "SYN_US_PROXY",
    market: str = "US",
) -> tuple[DateIdSourceRecord, ...]:
    records: list[DateIdSourceRecord] = []
    for index, (period, close_adjusted) in enumerate(prices_by_period):
        year_text, month_text = period.split("-")
        records.append(
            _record(
                date_id=f"{year_text[2:4]}{month_text}28-{index + 1}",
                payload_date=f"{period}-28",
                source_timestamp=datetime(int(year_text), int(month_text), 28, tzinfo=UTC),
                close_adjusted=close_adjusted,
                symbol=symbol,
                market=market,
            )
        )
    return tuple(records)


def _decision(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    asset_configs: tuple[RollingLongMaAssetConfig, ...] = (_config(),),
    decision_time: datetime = DECISION_TIME,
    intended_execution_time: datetime = INTENDED_EXECUTION_TIME,
) -> BacktestSingleStepDecision:
    return make_rules_only_single_step_decision(
        source,
        decision_time=decision_time,
        intended_execution_time=intended_execution_time,
        rolling_asset_configs=asset_configs,
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )


def test_builds_single_step_decision_artifact_from_records() -> None:
    decision = _decision(_monthly_records())

    assert isinstance(decision, BacktestSingleStepDecision)
    assert decision.decision_time == DECISION_TIME
    assert decision.intended_execution_time == INTENDED_EXECUTION_TIME
    assert decision.allocator_version == RULES_ALLOCATOR_V1
    assert decision.allocator_version == decision.target_weights.allocator_version
    assert decision.observation_spacing_reports[0].period_keys == (
        "2020-01",
        "2020-02",
        "2020-03",
    )
    assert decision.snapshot_asset_configs[0].long_ma == Decimal("102")
    assert decision.feature_snapshot.assets[0].current_price == Decimal("104")
    assert {weight.asset_id: weight.weight for weight in decision.target_weights.weights} == {
        "asset_A": Decimal("0.70"),
        "cash": Decimal("0.30"),
    }


def test_official_function_is_exported_from_backtest_engine() -> None:
    assert callable(make_rules_only_single_step_decision)


def test_compatibility_wrapper_still_builds_same_decision() -> None:
    records = _monthly_records()
    official = _decision(records)
    compatibility = build_single_step_rules_decision(
        records,
        decision_time=DECISION_TIME,
        intended_execution_time=INTENDED_EXECUTION_TIME,
        asset_configs=(_config(),),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )

    assert compatibility == official


def test_accepts_read_only_in_memory_source_reader() -> None:
    reader = InMemoryDateIdSourceReader(_monthly_records())

    decision = _decision(reader)

    assert decision.feature_snapshot.assets[0].as_of == datetime(2020, 3, 28, tzinfo=UTC)
    assert decision.target_weights.decision_time == DECISION_TIME


def test_snapshots_plain_iterable_once_for_composed_steps() -> None:
    decision = _decision(record for record in _monthly_records())

    assert decision.observation_spacing_reports[0].period_keys == (
        "2020-01",
        "2020-02",
        "2020-03",
    )
    assert decision.snapshot_asset_configs[0].long_ma == Decimal("102")
    assert decision.feature_snapshot.assets[0].current_price == Decimal("104")


def test_future_records_are_excluded_without_forward_fill() -> None:
    future = _record(
        date_id="200428-4",
        payload_date="2020-04-28",
        source_timestamp=DECISION_TIME + timedelta(microseconds=1),
        close_adjusted="999",
    )

    decision = _decision((*_monthly_records(), future))

    assert decision.observation_spacing_reports[0].period_keys == (
        "2020-01",
        "2020-02",
        "2020-03",
    )
    assert decision.feature_snapshot.assets[0].current_price == Decimal("104")


def test_duplicate_month_fails_before_decision_artifact_is_built() -> None:
    records = _monthly_records((("2020-01", "100"), ("2020-01", "101"), ("2020-02", "102")))

    with pytest.raises(ValueError, match="duplicate period"):
        _decision(records)


def test_intended_execution_time_must_be_after_decision_time() -> None:
    with pytest.raises(ValidationError, match="decision_time must be before intended_execution_time"):
        _decision(_monthly_records(), intended_execution_time=DECISION_TIME)


def test_naive_intended_execution_time_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        _decision(_monthly_records(), intended_execution_time=datetime(2020, 5, 1))


def test_decision_artifact_is_frozen_and_forbids_extra_fields() -> None:
    decision = _decision(_monthly_records())

    with pytest.raises(ValidationError, match="frozen"):
        decision.intended_execution_time = INTENDED_EXECUTION_TIME + timedelta(days=1)  # type: ignore[misc]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BacktestSingleStepDecision(
            decision_time=decision.decision_time,
            intended_execution_time=decision.intended_execution_time,
            allocator_version=decision.allocator_version,
            observation_spacing_reports=decision.observation_spacing_reports,
            snapshot_asset_configs=decision.snapshot_asset_configs,
            feature_snapshot=decision.feature_snapshot,
            target_weights=decision.target_weights,
            fills=(),
        )


def test_artifact_rejects_mismatched_nested_decision_time() -> None:
    decision = _decision(_monthly_records())
    shifted_snapshot = decision.feature_snapshot.model_copy(
        update={"decision_time": DECISION_TIME - timedelta(days=1)}
    )

    with pytest.raises(ValidationError, match="feature_snapshot.decision_time"):
        BacktestSingleStepDecision(
            decision_time=decision.decision_time,
            intended_execution_time=decision.intended_execution_time,
            allocator_version=decision.allocator_version,
            observation_spacing_reports=decision.observation_spacing_reports,
            snapshot_asset_configs=decision.snapshot_asset_configs,
            feature_snapshot=shifted_snapshot,
            target_weights=decision.target_weights,
        )


def test_artifact_rejects_target_weights_that_do_not_match_snapshot_assets() -> None:
    decision = _decision(_monthly_records())
    reversed_weights = decision.target_weights.model_copy(
        update={"weights": tuple(reversed(decision.target_weights.weights))}
    )

    with pytest.raises(ValidationError, match="target_weights must match feature_snapshot"):
        BacktestSingleStepDecision(
            decision_time=decision.decision_time,
            intended_execution_time=decision.intended_execution_time,
            allocator_version=decision.allocator_version,
            observation_spacing_reports=decision.observation_spacing_reports,
            snapshot_asset_configs=decision.snapshot_asset_configs,
            feature_snapshot=decision.feature_snapshot,
            target_weights=reversed_weights,
        )


def test_artifact_rejects_allocator_version_mismatch_with_target_weights() -> None:
    decision = _decision(_monthly_records())
    mismatched_target = decision.target_weights.model_copy(
        update={"allocator_version": "other_allocator.v1"}
    )

    with pytest.raises(ValidationError, match="allocator_version must equal target_weights"):
        BacktestSingleStepDecision(
            decision_time=decision.decision_time,
            intended_execution_time=decision.intended_execution_time,
            allocator_version=decision.allocator_version,
            observation_spacing_reports=decision.observation_spacing_reports,
            snapshot_asset_configs=decision.snapshot_asset_configs,
            feature_snapshot=decision.feature_snapshot,
            target_weights=mismatched_target,
        )


def test_artifact_rejects_allocator_version_other_than_rules_allocator_v1() -> None:
    decision = _decision(_monthly_records())

    with pytest.raises(ValidationError, match="allocator_version must be rules_allocator.v1"):
        BacktestSingleStepDecision(
            decision_time=decision.decision_time,
            intended_execution_time=decision.intended_execution_time,
            allocator_version="other_allocator.v1",
            observation_spacing_reports=decision.observation_spacing_reports,
            snapshot_asset_configs=decision.snapshot_asset_configs,
            feature_snapshot=decision.feature_snapshot,
            target_weights=decision.target_weights,
        )


def test_no_execution_nav_or_benchmark_fields_are_produced() -> None:
    decision = _decision(_monthly_records())

    assert not hasattr(decision, "execution_price")
    assert not hasattr(decision, "fills")
    assert not hasattr(decision, "transaction_costs")
    assert not hasattr(decision, "holdings")
    assert not hasattr(decision, "cash_ledger")
    assert not hasattr(decision, "nav")
    assert not hasattr(decision, "portfolio_value_series")
    assert not hasattr(decision, "benchmark_relative_metrics")


def test_single_step_module_has_no_forbidden_imports_or_calls() -> None:
    module_path = Path(__file__).resolve().parents[1] / "src" / "backtest_engine" / "single_step.py"
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
        "subprocess",
    }
    forbidden_text = {
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        "random",
        "numpy.random",
        "SQLiteDateIdSourceStore",
        ".save_record(",
        "ScoutInputBuilder",
        "AllocatorDecision",
        "AllocationRegime",
        "execution_price",
        "fills",
        "transaction_cost",
        "slippage",
        "holdings",
        "cash_ledger",
        "portfolio_value_series",
        "benchmark_relative",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots

    for token in forbidden_text:
        assert token not in text
