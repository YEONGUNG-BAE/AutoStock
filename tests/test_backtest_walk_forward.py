from __future__ import annotations

import ast
import os
import subprocess
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
    COST_MODEL_V1,
    PERIOD_STEP_POLICY_V1,
    WALK_FORWARD_POLICY_V1,
    BacktestCostModel,
    BacktestHolding,
    BacktestNavPoint,
    BacktestPeriodSpec,
    BacktestPortfolioState,
    BacktestWalkForwardResult,
    RollingLongMaAssetConfig,
    run_explicit_schedule_rules_walk_forward_nav,
    run_single_period_rules_rebalance_step,
)
from backtest_engine.rules_allocator import RULES_ALLOCATOR_V2_POLICY  # noqa: E402
from domain import DateId, DateIdSourceRecord, FactType  # noqa: E402

INITIAL_AS_OF = datetime(2020, 4, 1, 0, 0, tzinfo=UTC)
PERIOD_1_DECISION = datetime(2020, 4, 30, 0, 0, tzinfo=UTC)
PERIOD_1_EXECUTION = datetime(2020, 5, 31, 0, 0, tzinfo=UTC)
PERIOD_2_DECISION = datetime(2020, 6, 30, 0, 0, tzinfo=UTC)
PERIOD_2_EXECUTION = datetime(2020, 7, 31, 0, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
USDKRW_PERIOD_1 = Decimal("1300")
USDKRW_PERIOD_2 = Decimal("1500")

SYMBOL_A = "SYN_US_PROXY"
MARKET_A = "US"

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "backtest_engine" / "walk_forward.py"
)

FOCUSED_TEST_FILES = (
    "tests/test_backtest_walk_forward.py",
    "tests/test_backtest_period_step.py",
    "tests/test_backtest_rebalance.py",
    "tests/test_backtest_execution_prices.py",
    "tests/test_backtest_single_step_decision.py",
    "tests/test_backtest_observation_spacing.py",
    "tests/test_backtest_rolling_features.py",
    "tests/test_backtest_snapshot_builder.py",
    "tests/test_backtest_step_contract.py",
    "tests/test_rules_allocator.py",
    "tests/test_backtest_data_loader.py",
    "tests/test_backtest_asof_guard.py",
    "tests/test_backtest_source_record_conversion.py",
    "tests/test_scout_input_builder.py",
    "tests/test_backtest_design_freeze_docs.py",
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
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.PRICE,
        source_name=source_name,
        source_timestamp=source_timestamp,
        created_at=CREATED_AT,
        summary="synthetic price record",
        payload={
            "schema_name": BACKTEST_INSTRUMENT_PRICE_SCHEMA,
            "date": payload_date,
            "symbol": symbol,
            "market": market,
            "close_adjusted": close_adjusted,
        },
        symbol=symbol,
        market=market,
    )


def _two_period_records() -> tuple[DateIdSourceRecord, ...]:
    monthly_signal_specs = (
        ("200228-1", "2020-02-28", datetime(2020, 2, 28, tzinfo=UTC), "100"),
        ("200328-2", "2020-03-28", datetime(2020, 3, 28, tzinfo=UTC), "102"),
        ("200428-3", "2020-04-28", datetime(2020, 4, 28, tzinfo=UTC), "104"),
        ("200628-5", "2020-06-28", datetime(2020, 6, 28, tzinfo=UTC), "108"),
    )
    records = [
        _record(
            date_id=date_id,
            payload_date=payload_date,
            source_timestamp=source_timestamp,
            close_adjusted=close_adjusted,
        )
        for date_id, payload_date, source_timestamp, close_adjusted in monthly_signal_specs
    ]
    records.append(
        _record(
            date_id="200531-6",
            payload_date="2020-05-31",
            source_timestamp=PERIOD_1_EXECUTION,
            close_adjusted="110",
        )
    )
    records.append(
        _record(
            date_id="200731-7",
            payload_date="2020-07-31",
            source_timestamp=PERIOD_2_EXECUTION,
            close_adjusted="112",
        )
    )
    return tuple(records)


def _period_specs(
    *,
    usdkrw_period_1: Decimal = USDKRW_PERIOD_1,
    usdkrw_period_2: Decimal = USDKRW_PERIOD_2,
) -> tuple[BacktestPeriodSpec, ...]:
    return (
        BacktestPeriodSpec(
            decision_time=PERIOD_1_DECISION,
            intended_execution_time=PERIOD_1_EXECUTION,
            usdkrw_rate=usdkrw_period_1,
        ),
        BacktestPeriodSpec(
            decision_time=PERIOD_2_DECISION,
            intended_execution_time=PERIOD_2_EXECUTION,
            usdkrw_rate=usdkrw_period_2,
        ),
    )


def _portfolio(
    *,
    cash_krw: Decimal = Decimal("1000000"),
    holdings: tuple[BacktestHolding, ...] = (),
) -> BacktestPortfolioState:
    return BacktestPortfolioState(
        as_of=INITIAL_AS_OF,
        cash_krw=cash_krw,
        holdings=holdings,
    )


def _cost_model() -> BacktestCostModel:
    return BacktestCostModel(
        cost_model_version=COST_MODEL_V1,
        fee_bps=Decimal("10"),
        kr_sell_tax_bps=Decimal("23"),
        fx_spread_bps=Decimal("15"),
    )


def _run_walk_forward(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    period_specs: tuple[BacktestPeriodSpec, ...] | None = None,
    portfolio_state: BacktestPortfolioState | None = None,
) -> BacktestWalkForwardResult:
    return run_explicit_schedule_rules_walk_forward_nav(
        source,
        period_specs=period_specs or _period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=portfolio_state or _portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )


def _v2_asset_configs() -> tuple[RollingLongMaAssetConfig, ...]:
    return (
        _config(
            asset_id="asset_us",
            symbol="SP500TR",
            market="US",
            risk_on_weight=Decimal("0.55"),
            risk_off_weight=Decimal("0.30"),
        ),
        _config(
            asset_id="asset_kr",
            symbol="KOSPI",
            market="KR",
            risk_on_weight=Decimal("0.20"),
            risk_off_weight=Decimal("0.05"),
            max_weight=Decimal("0.40"),
        ),
        _config(
            asset_id="asset_gold",
            symbol="GLD",
            market="US",
            risk_on_weight=Decimal("0.15"),
            risk_off_weight=Decimal("0.20"),
            max_weight=Decimal("0.35"),
        ),
    )


def _v2_records() -> tuple[DateIdSourceRecord, ...]:
    records: list[DateIdSourceRecord] = []
    suffix = 1
    for symbol, market in (
        ("SP500TR", "US"),
        ("KOSPI", "KR"),
        ("GLD", "US"),
    ):
        for base_record in _two_period_records():
            payload_date = base_record.payload["date"]
            yymmdd = payload_date[2:4] + payload_date[5:7] + payload_date[8:10]
            records.append(
                _record(
                    date_id=f"{yymmdd}-{suffix}",
                    payload_date=payload_date,
                    source_timestamp=base_record.source_timestamp,
                    close_adjusted=base_record.payload["close_adjusted"],
                    symbol=symbol,
                    market=market,
                )
            )
            suffix += 1
    return tuple(records)


def test_builds_walk_forward_result_from_synthetic_records() -> None:
    result = _run_walk_forward(_two_period_records())

    assert isinstance(result, BacktestWalkForwardResult)
    assert result.walk_forward_policy == WALK_FORWARD_POLICY_V1
    assert len(result.period_specs) == 2


def test_produces_one_step_per_period_spec() -> None:
    result = _run_walk_forward(_two_period_records())

    assert len(result.steps) == len(result.period_specs) == 2
    assert result.steps[0].period_step_policy == PERIOD_STEP_POLICY_V1


def test_produces_one_nav_point_per_step() -> None:
    result = _run_walk_forward(_two_period_records())

    assert len(result.nav_points) == len(result.steps) == 2


def test_carries_portfolio_state_forward() -> None:
    result = _run_walk_forward(_two_period_records())
    expected_step_2 = run_single_period_rules_rebalance_step(
        _two_period_records(),
        decision_time=PERIOD_2_DECISION,
        intended_execution_time=PERIOD_2_EXECUTION,
        rolling_asset_configs=(_config(),),
        portfolio_state=result.steps[0].next_portfolio_state,
        cost_model=_cost_model(),
        usdkrw_rate=USDKRW_PERIOD_2,
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )

    assert result.steps[1].next_portfolio_state == expected_step_2.next_portfolio_state
    assert result.final_portfolio_state == result.steps[-1].next_portfolio_state


def test_nav_point_uses_post_trade_portfolio_value() -> None:
    result = _run_walk_forward(_two_period_records())

    for step, nav_point in zip(result.steps, result.nav_points, strict=True):
        assert (
            nav_point.portfolio_value_krw
            == step.rebalance_result.post_trade_portfolio_value_krw
        )


def test_nav_point_as_of_equals_intended_execution_time() -> None:
    result = _run_walk_forward(_two_period_records())

    for step, nav_point in zip(result.steps, result.nav_points, strict=True):
        assert nav_point.as_of == step.intended_execution_time


def test_total_cost_fields_equal_step_rebalance_sums() -> None:
    result = _run_walk_forward(_two_period_records())

    assert result.total_fee_krw == sum(
        step.rebalance_result.total_fee_krw for step in result.steps
    )
    assert result.total_tax_krw == sum(
        step.rebalance_result.total_tax_krw for step in result.steps
    )
    assert result.total_fx_spread_krw == sum(
        step.rebalance_result.total_fx_spread_krw for step in result.steps
    )
    assert result.total_cost_krw == sum(
        step.rebalance_result.total_cost_krw for step in result.steps
    )


def test_uses_explicit_usdkrw_rate_per_period() -> None:
    result = _run_walk_forward(_two_period_records())

    assert result.steps[0].rebalance_result.trades[0].usdkrw_rate == USDKRW_PERIOD_1
    assert result.steps[1].rebalance_result.trades[0].usdkrw_rate == USDKRW_PERIOD_2


def test_v2_walk_forward_uses_static_normal_target_weights() -> None:
    result = run_explicit_schedule_rules_walk_forward_nav(
        _v2_records(),
        period_specs=_period_specs()[:1],
        rolling_asset_configs=_v2_asset_configs(),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
        rules_allocator_version=RULES_ALLOCATOR_V2_POLICY,
    )
    decision = result.steps[0].decision

    assert decision.allocator_version == RULES_ALLOCATOR_V2_POLICY
    assert decision.target_weights.decision_time == PERIOD_1_DECISION
    assert {weight.asset_id: weight.weight for weight in decision.target_weights.weights} == {
        "asset_us": Decimal("0.70"),
        "asset_kr": Decimal("0.15"),
        "asset_gold": Decimal("0.10"),
        "cash": Decimal("0.05"),
    }


def test_v2_walk_forward_is_deterministic() -> None:
    first = run_explicit_schedule_rules_walk_forward_nav(
        _v2_records(),
        period_specs=_period_specs()[:1],
        rolling_asset_configs=_v2_asset_configs(),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
        rules_allocator_version=RULES_ALLOCATOR_V2_POLICY,
    )
    second = run_explicit_schedule_rules_walk_forward_nav(
        _v2_records(),
        period_specs=_period_specs()[:1],
        rolling_asset_configs=_v2_asset_configs(),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
        rules_allocator_version=RULES_ALLOCATOR_V2_POLICY,
    )

    assert first == second


def test_v1_and_v2_walk_forward_target_weights_differ_when_switch_is_effective() -> None:
    v1 = run_explicit_schedule_rules_walk_forward_nav(
        _v2_records(),
        period_specs=_period_specs()[:1],
        rolling_asset_configs=_v2_asset_configs(),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )
    v2 = run_explicit_schedule_rules_walk_forward_nav(
        _v2_records(),
        period_specs=_period_specs()[:1],
        rolling_asset_configs=_v2_asset_configs(),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
        rules_allocator_version=RULES_ALLOCATOR_V2_POLICY,
    )

    assert v1.steps[0].decision.allocator_version != v2.steps[0].decision.allocator_version
    assert v1.steps[0].decision.target_weights.weights != (
        v2.steps[0].decision.target_weights.weights
    )


def test_changing_period_usdkrw_changes_trade_quantities() -> None:
    low = _run_walk_forward(
        _two_period_records(),
        period_specs=_period_specs(
            usdkrw_period_1=Decimal("1000"),
            usdkrw_period_2=Decimal("1000"),
        ),
    )
    high = _run_walk_forward(
        _two_period_records(),
        period_specs=_period_specs(
            usdkrw_period_1=Decimal("1500"),
            usdkrw_period_2=Decimal("1500"),
        ),
    )

    assert (
        low.steps[0].rebalance_result.trades[0].quantity
        != high.steps[0].rebalance_result.trades[0].quantity
    )
    assert (
        low.steps[1].rebalance_result.trades[0].quantity
        != high.steps[1].rebalance_result.trades[0].quantity
    )


def test_raises_on_empty_period_specs() -> None:
    with pytest.raises(ValueError, match="period_specs must not be empty"):
        run_explicit_schedule_rules_walk_forward_nav(
            _two_period_records(),
            period_specs=(),
            rolling_asset_configs=(_config(),),
            initial_portfolio_state=_portfolio(),
            cost_model=_cost_model(),
            cash_asset_id="cash",
            cash_min_weight=Decimal("0.05"),
        )


def test_raises_when_first_decision_time_before_initial_as_of() -> None:
    bad_period = (
        BacktestPeriodSpec(
            decision_time=INITIAL_AS_OF - timedelta(days=1),
            intended_execution_time=PERIOD_1_EXECUTION,
            usdkrw_rate=USDKRW_PERIOD_1,
        ),
    )

    with pytest.raises(ValueError, match="first period decision_time"):
        _run_walk_forward(_two_period_records(), period_specs=bad_period)


def test_raises_when_decision_times_not_strictly_increasing() -> None:
    bad_period = (
        BacktestPeriodSpec(
            decision_time=PERIOD_1_DECISION,
            intended_execution_time=PERIOD_1_EXECUTION,
            usdkrw_rate=USDKRW_PERIOD_1,
        ),
        BacktestPeriodSpec(
            decision_time=PERIOD_1_DECISION,
            intended_execution_time=PERIOD_2_EXECUTION,
            usdkrw_rate=USDKRW_PERIOD_2,
        ),
    )

    with pytest.raises(ValueError, match="decision_time values must be strictly increasing"):
        _run_walk_forward(_two_period_records(), period_specs=bad_period)


def test_raises_when_intended_execution_times_not_strictly_increasing() -> None:
    from backtest_engine.walk_forward import _validate_explicit_schedule_order

    bad_period = (
        BacktestPeriodSpec.model_construct(
            decision_time=PERIOD_1_DECISION,
            intended_execution_time=datetime(2020, 10, 31, tzinfo=UTC),
            usdkrw_rate=USDKRW_PERIOD_1,
        ),
        BacktestPeriodSpec.model_construct(
            decision_time=datetime(2020, 11, 1, tzinfo=UTC),
            intended_execution_time=datetime(2020, 9, 30, tzinfo=UTC),
            usdkrw_rate=USDKRW_PERIOD_2,
        ),
    )

    with pytest.raises(
        ValueError,
        match="intended_execution_time values must be strictly increasing",
    ):
        _validate_explicit_schedule_order(bad_period, initial_as_of=INITIAL_AS_OF)


def test_raises_when_later_decision_before_previous_execution() -> None:
    bad_period = (
        BacktestPeriodSpec(
            decision_time=PERIOD_1_DECISION,
            intended_execution_time=PERIOD_1_EXECUTION,
            usdkrw_rate=USDKRW_PERIOD_1,
        ),
        BacktestPeriodSpec(
            decision_time=PERIOD_1_EXECUTION - timedelta(days=1),
            intended_execution_time=PERIOD_2_EXECUTION,
            usdkrw_rate=USDKRW_PERIOD_2,
        ),
    )

    with pytest.raises(
        ValueError,
        match="decision_time must be >= previous intended_execution_time",
    ):
        _run_walk_forward(_two_period_records(), period_specs=bad_period)


def test_propagates_observation_spacing_guard_failure() -> None:
    skipped_month = (
        _record(
            date_id="200128-1",
            payload_date="2020-01-28",
            source_timestamp=datetime(2020, 1, 28, tzinfo=UTC),
            close_adjusted="100",
        ),
        _record(
            date_id="200328-2",
            payload_date="2020-03-28",
            source_timestamp=datetime(2020, 3, 28, tzinfo=UTC),
            close_adjusted="102",
        ),
        _record(
            date_id="200428-3",
            payload_date="2020-04-28",
            source_timestamp=datetime(2020, 4, 28, tzinfo=UTC),
            close_adjusted="104",
        ),
        _record(
            date_id="200531-4",
            payload_date="2020-05-31",
            source_timestamp=PERIOD_1_EXECUTION,
            close_adjusted="110",
        ),
    )

    with pytest.raises(ValueError, match="skipped period"):
        _run_walk_forward(skipped_month, period_specs=_period_specs()[:1])


def test_propagates_missing_future_execution_price_failure() -> None:
    signal_only = (
        _record(
            date_id="200228-1",
            payload_date="2020-02-28",
            source_timestamp=datetime(2020, 2, 28, tzinfo=UTC),
            close_adjusted="100",
        ),
        _record(
            date_id="200328-2",
            payload_date="2020-03-28",
            source_timestamp=datetime(2020, 3, 28, tzinfo=UTC),
            close_adjusted="102",
        ),
        _record(
            date_id="200428-3",
            payload_date="2020-04-28",
            source_timestamp=datetime(2020, 4, 28, tzinfo=UTC),
            close_adjusted="104",
        ),
    )

    with pytest.raises(ValueError, match="no future executable price"):
        _run_walk_forward(signal_only, period_specs=_period_specs()[:1])


def test_propagates_negative_cash_failure() -> None:
    full_risk_on_config = RollingLongMaAssetConfig(
        asset_id="asset_A",
        symbol=SYMBOL_A,
        market=MARKET_A,
        lookback_count=3,
        risk_on_weight=Decimal("1.0"),
        risk_off_weight=Decimal("1.0"),
        min_weight=Decimal("0"),
        max_weight=Decimal("1.0"),
    )

    with pytest.raises(ValueError, match="cash would become negative"):
        run_explicit_schedule_rules_walk_forward_nav(
            _two_period_records(),
            period_specs=_period_specs()[:1],
            rolling_asset_configs=(full_risk_on_config,),
            initial_portfolio_state=_portfolio(cash_krw=Decimal("1000")),
            cost_model=_cost_model(),
            cash_asset_id="cash",
            cash_min_weight=Decimal("0"),
        )


def test_works_with_in_memory_source_reader() -> None:
    reader = InMemoryDateIdSourceReader(_two_period_records())
    result = _run_walk_forward(reader)

    assert result.nav_points[0].portfolio_value_krw > Decimal("0")


def test_works_with_one_shot_generator_by_materializing_once() -> None:
    records = _two_period_records()

    result = run_explicit_schedule_rules_walk_forward_nav(
        (record for record in records),
        period_specs=_period_specs(),
        rolling_asset_configs=(_config(),),
        initial_portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )

    assert len(result.steps) == 2


def test_result_model_is_frozen_and_forbids_extra_fields() -> None:
    result = _run_walk_forward(_two_period_records())

    with pytest.raises(ValidationError):
        result.walk_forward_policy = "other"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        BacktestWalkForwardResult(
            walk_forward_policy=WALK_FORWARD_POLICY_V1,
            initial_portfolio_state=result.initial_portfolio_state,
            period_specs=result.period_specs,
            steps=result.steps,
            nav_points=result.nav_points,
            final_portfolio_state=result.final_portfolio_state,
            total_fee_krw=result.total_fee_krw,
            total_tax_krw=result.total_tax_krw,
            total_fx_spread_krw=result.total_fx_spread_krw,
            total_cost_krw=result.total_cost_krw,
            benchmark_relative_metrics=(),  # type: ignore[call-arg]
        )


def test_period_spec_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        BacktestPeriodSpec(
            decision_time=datetime(2020, 4, 30),
            intended_execution_time=PERIOD_1_EXECUTION,
            usdkrw_rate=USDKRW_PERIOD_1,
        )


def test_period_spec_rejects_non_positive_or_float_usdkrw() -> None:
    with pytest.raises(ValidationError):
        BacktestPeriodSpec(
            decision_time=PERIOD_1_DECISION,
            intended_execution_time=PERIOD_1_EXECUTION,
            usdkrw_rate=Decimal("0"),
        )

    with pytest.raises(ValidationError):
        BacktestPeriodSpec(
            decision_time=PERIOD_1_DECISION,
            intended_execution_time=PERIOD_1_EXECUTION,
            usdkrw_rate=1300.0,  # type: ignore[arg-type]
        )


def test_result_rejects_mismatched_step_nav_lengths() -> None:
    result = _run_walk_forward(_two_period_records())

    with pytest.raises(ValidationError):
        BacktestWalkForwardResult(
            walk_forward_policy=WALK_FORWARD_POLICY_V1,
            initial_portfolio_state=result.initial_portfolio_state,
            period_specs=result.period_specs,
            steps=result.steps[:1],
            nav_points=result.nav_points,
            final_portfolio_state=result.final_portfolio_state,
            total_fee_krw=result.total_fee_krw,
            total_tax_krw=result.total_tax_krw,
            total_fx_spread_krw=result.total_fx_spread_krw,
            total_cost_krw=result.total_cost_krw,
        )


def test_result_rejects_nav_point_mismatch() -> None:
    result = _run_walk_forward(_two_period_records())
    bad_nav = BacktestNavPoint(
        as_of=result.nav_points[0].as_of,
        portfolio_value_krw=Decimal("0"),
        cash_krw=result.nav_points[0].cash_krw,
        total_cost_krw=result.nav_points[0].total_cost_krw,
    )

    with pytest.raises(ValidationError):
        BacktestWalkForwardResult(
            walk_forward_policy=WALK_FORWARD_POLICY_V1,
            initial_portfolio_state=result.initial_portfolio_state,
            period_specs=result.period_specs,
            steps=result.steps,
            nav_points=(bad_nav, *result.nav_points[1:]),
            final_portfolio_state=result.final_portfolio_state,
            total_fee_krw=result.total_fee_krw,
            total_tax_krw=result.total_tax_krw,
            total_fx_spread_krw=result.total_fx_spread_krw,
            total_cost_krw=result.total_cost_krw,
        )


def test_result_has_no_benchmark_report_or_performance_fields() -> None:
    forbidden = {
        "benchmark",
        "benchmark_relative",
        "benchmark_relative_metrics",
        "performance",
        "report",
        "report_markdown",
        "sp500",
    }
    result_fields = set(BacktestWalkForwardResult.model_fields)
    assert result_fields.isdisjoint(forbidden)


def test_module_does_not_fetch_or_read_real_data() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    forbidden = (
        "yfinance",
        "fred",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "read_csv",
        "load_csv",
        "monthly.csv",
        "get_data.py",
    )
    for token in forbidden:
        assert token not in text, f"forbidden token present: {token}"


def test_module_does_not_import_forbidden_runtime_packages() -> None:
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
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots


def test_module_has_no_forbidden_runtime_or_imports() -> None:
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
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".")[0] for alias in node.names}
            assert imported.isdisjoint(forbidden_import_roots)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            assert node.module.split(".")[0] not in forbidden_import_roots

    forbidden_text = (
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
        "BenchmarkReturnPoint",
        "compute_benchmark",
        "benchmark_relative",
        "render_benchmark",
        "report_markdown",
        "sp500",
        "S&P",
    )
    for token in forbidden_text:
        assert token not in text, f"forbidden token present: {token}"


def test_focused_regression_suite_passes() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    command = [
        "uv",
        "run",
        "pytest",
        *[
            path
            for path in FOCUSED_TEST_FILES
            if path != "tests/test_backtest_walk_forward.py"
        ],
        "-q",
    ]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
