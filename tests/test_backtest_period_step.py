from __future__ import annotations

import ast
import os
import subprocess
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
    COST_MODEL_V1,
    EXECUTION_PRICE_POLICY_V1,
    PERIOD_STEP_POLICY_V1,
    REBALANCE_ACCOUNTING_POLICY_V1,
    BacktestCostModel,
    BacktestExecutionPriceSlice,
    BacktestHolding,
    BacktestPortfolioState,
    BacktestRebalanceResult,
    BacktestSinglePeriodStepResult,
    BacktestSingleStepDecision,
    RollingLongMaAssetConfig,
    run_single_period_rules_rebalance_step,
)
from backtest_engine.rules_allocator import RULES_ALLOCATOR_V2_POLICY  # noqa: E402
from domain import DateId, DateIdSourceRecord, FactType  # noqa: E402

DECISION_TIME = datetime(2020, 4, 30, 0, 0, tzinfo=UTC)
INTENDED_EXECUTION_TIME = datetime(2020, 5, 31, 0, 0, tzinfo=UTC)
PORTFOLIO_AS_OF = datetime(2020, 4, 1, 0, 0, tzinfo=UTC)
CREATED_AT = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
USDKRW = Decimal("1300")

SYMBOL_A = "SYN_US_PROXY"
MARKET_A = "US"

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "backtest_engine" / "period_step.py"
)

FOCUSED_TEST_FILES = (
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


def _signal_records() -> tuple[DateIdSourceRecord, ...]:
    periods = (("2020-02", "100"), ("2020-03", "102"), ("2020-04", "104"))
    records: list[DateIdSourceRecord] = []
    for index, (period, close_adjusted) in enumerate(periods):
        year_text, month_text = period.split("-")
        records.append(
            _record(
                date_id=f"{year_text[2:4]}{month_text}28-{index + 1}",
                payload_date=f"{period}-28",
                source_timestamp=datetime(int(year_text), int(month_text), 28, tzinfo=UTC),
                close_adjusted=close_adjusted,
            )
        )
    return tuple(records)


def _execution_record(
    *,
    source_timestamp: datetime = INTENDED_EXECUTION_TIME,
    close_adjusted: object = "110",
    date_id: str = "200531-9",
    payload_date: str = "2020-05-31",
) -> DateIdSourceRecord:
    return _record(
        date_id=date_id,
        payload_date=payload_date,
        source_timestamp=source_timestamp,
        close_adjusted=close_adjusted,
    )


def _portfolio(
    *,
    cash_krw: Decimal = Decimal("1000000"),
    holdings: tuple[BacktestHolding, ...] = (),
) -> BacktestPortfolioState:
    return BacktestPortfolioState(
        as_of=PORTFOLIO_AS_OF,
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


def _records_with_execution(
    *extra_execution: DateIdSourceRecord,
) -> tuple[DateIdSourceRecord, ...]:
    return (*_signal_records(), *extra_execution)


def _v2_asset_configs() -> tuple[RollingLongMaAssetConfig, ...]:
    return (
        _config(asset_id="asset_us", symbol="SP500TR", market="US"),
        _config(asset_id="asset_kr", symbol="KOSPI", market="KR"),
        _config(asset_id="asset_gold", symbol="GLD", market="US"),
    )


def _v2_records_with_execution() -> tuple[DateIdSourceRecord, ...]:
    records: list[DateIdSourceRecord] = []
    suffix = 1
    for symbol, market in (
        ("SP500TR", "US"),
        ("KOSPI", "KR"),
        ("GLD", "US"),
    ):
        for base_record in _records_with_execution(_execution_record()):
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


def _run_step(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    portfolio_state: BacktestPortfolioState | None = None,
    usdkrw_rate: Decimal = USDKRW,
    cash_krw: Decimal = Decimal("1000000"),
) -> BacktestSinglePeriodStepResult:
    return run_single_period_rules_rebalance_step(
        source,
        decision_time=DECISION_TIME,
        intended_execution_time=INTENDED_EXECUTION_TIME,
        rolling_asset_configs=(_config(),),
        portfolio_state=portfolio_state or _portfolio(cash_krw=cash_krw),
        cost_model=_cost_model(),
        usdkrw_rate=usdkrw_rate,
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )


def test_builds_one_period_result_from_synthetic_records() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    assert isinstance(result, BacktestSinglePeriodStepResult)
    assert result.decision_time == DECISION_TIME
    assert result.intended_execution_time == INTENDED_EXECUTION_TIME
    assert result.period_step_policy == PERIOD_STEP_POLICY_V1


def test_produces_backtest_single_step_decision() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    assert isinstance(result.decision, BacktestSingleStepDecision)
    assert result.decision.decision_time == DECISION_TIME
    assert result.decision.intended_execution_time == INTENDED_EXECUTION_TIME


def test_produces_backtest_execution_price_slice() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    assert isinstance(result.execution_prices, BacktestExecutionPriceSlice)
    assert result.execution_prices.execution_policy == EXECUTION_PRICE_POLICY_V1
    assert len(result.execution_prices.prices) == 1
    assert result.execution_prices.prices[0].execution_price == Decimal("110")


def test_produces_backtest_rebalance_result() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    assert isinstance(result.rebalance_result, BacktestRebalanceResult)
    assert result.rebalance_result.accounting_policy == REBALANCE_ACCOUNTING_POLICY_V1
    assert len(result.rebalance_result.trades) == 1


def test_builds_next_portfolio_state() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    assert isinstance(result.next_portfolio_state, BacktestPortfolioState)


def test_next_portfolio_state_as_of_equals_intended_execution_time() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    assert result.next_portfolio_state.as_of == INTENDED_EXECUTION_TIME


def test_next_portfolio_state_cash_equals_rebalance_cash_after() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    assert result.next_portfolio_state.cash_krw == result.rebalance_result.cash_krw_after


def test_next_portfolio_state_holdings_equal_rebalance_post_trade_holdings() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    assert (
        result.next_portfolio_state.holdings
        == result.rebalance_result.post_trade_holdings
    )


def test_uses_decision_time_for_signal_side_as_of_decision() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    assert result.decision.feature_snapshot.decision_time == DECISION_TIME
    assert result.decision.feature_snapshot.assets[0].as_of <= DECISION_TIME


def test_v2_period_step_uses_static_normal_target_weights() -> None:
    result = run_single_period_rules_rebalance_step(
        _v2_records_with_execution(),
        decision_time=DECISION_TIME,
        intended_execution_time=INTENDED_EXECUTION_TIME,
        rolling_asset_configs=_v2_asset_configs(),
        portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        usdkrw_rate=USDKRW,
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
        rules_allocator_version=RULES_ALLOCATOR_V2_POLICY,
    )

    assert result.decision.allocator_version == RULES_ALLOCATOR_V2_POLICY
    assert result.decision.target_weights.decision_time == DECISION_TIME
    assert {weight.asset_id: weight.weight for weight in result.decision.target_weights.weights} == {
        "asset_us": Decimal("0.70"),
        "asset_kr": Decimal("0.15"),
        "asset_gold": Decimal("0.10"),
        "cash": Decimal("0.05"),
    }


def test_uses_intended_execution_time_for_execution_price_selection() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    assert result.execution_prices.intended_execution_time == INTENDED_EXECUTION_TIME
    assert result.execution_prices.prices[0].source_timestamp == INTENDED_EXECUTION_TIME


def test_does_not_use_prices_before_intended_execution_time() -> None:
    early = _execution_record(
        source_timestamp=INTENDED_EXECUTION_TIME - timedelta(days=1),
        close_adjusted="999",
        date_id="200530-8",
    )
    boundary = _execution_record(close_adjusted="110")
    result = _run_step(_records_with_execution(early, boundary))

    assert result.execution_prices.prices[0].execution_price == Decimal("110")
    assert result.execution_prices.prices[0].source_timestamp >= INTENDED_EXECUTION_TIME


def test_applies_fee_tax_fx_spread_through_rebalance_result() -> None:
    result = _run_step(_records_with_execution(_execution_record()))
    trade = result.rebalance_result.trades[0]

    assert trade.fee_krw > Decimal("0")
    assert trade.fx_spread_krw > Decimal("0")
    assert result.rebalance_result.total_cost_krw > Decimal("0")


def test_explicit_usdkrw_rate_affects_us_gold_valuation() -> None:
    low = _run_step(_records_with_execution(_execution_record()), usdkrw_rate=Decimal("1000"))
    high = _run_step(_records_with_execution(_execution_record()), usdkrw_rate=Decimal("1500"))

    assert low.rebalance_result.trades[0].quantity != high.rebalance_result.trades[0].quantity


def test_raises_when_observation_spacing_guard_fails() -> None:
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
        _execution_record(),
    )

    with pytest.raises(ValueError, match="skipped period"):
        _run_step(skipped_month)


def test_raises_when_no_future_execution_price_exists() -> None:
    with pytest.raises(ValueError, match="no future executable price"):
        _run_step(_signal_records())


def test_raises_when_rebalance_cash_would_go_negative() -> None:
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
        run_single_period_rules_rebalance_step(
            _records_with_execution(_execution_record()),
            decision_time=DECISION_TIME,
            intended_execution_time=INTENDED_EXECUTION_TIME,
            rolling_asset_configs=(full_risk_on_config,),
            portfolio_state=_portfolio(cash_krw=Decimal("1000")),
            cost_model=_cost_model(),
            usdkrw_rate=USDKRW,
            cash_asset_id="cash",
            cash_min_weight=Decimal("0"),
        )


def test_works_with_in_memory_source_reader() -> None:
    reader = InMemoryDateIdSourceReader(_records_with_execution(_execution_record()))

    result = _run_step(reader)

    assert result.execution_prices.prices[0].execution_price == Decimal("110")


def test_works_with_one_shot_generator_by_materializing_once() -> None:
    records = _records_with_execution(_execution_record())

    result = run_single_period_rules_rebalance_step(
        (record for record in records),
        decision_time=DECISION_TIME,
        intended_execution_time=INTENDED_EXECUTION_TIME,
        rolling_asset_configs=(_config(),),
        portfolio_state=_portfolio(),
        cost_model=_cost_model(),
        usdkrw_rate=USDKRW,
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )

    assert result.execution_prices.prices[0].execution_price == Decimal("110")


def test_result_model_is_frozen_and_forbids_extra_fields() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    with pytest.raises(ValidationError):
        result.decision_time = INTENDED_EXECUTION_TIME  # type: ignore[misc]

    with pytest.raises(ValidationError):
        BacktestSinglePeriodStepResult(
            decision_time=DECISION_TIME,
            intended_execution_time=INTENDED_EXECUTION_TIME,
            period_step_policy=PERIOD_STEP_POLICY_V1,
            decision=result.decision,
            execution_prices=result.execution_prices,
            rebalance_result=result.rebalance_result,
            next_portfolio_state=result.next_portfolio_state,
            nav_series=(),  # type: ignore[call-arg]
        )


def test_result_model_rejects_timestamp_mismatch() -> None:
    result = _run_step(_records_with_execution(_execution_record()))

    with pytest.raises(ValidationError):
        BacktestSinglePeriodStepResult(
            decision_time=DECISION_TIME + timedelta(days=1),
            intended_execution_time=INTENDED_EXECUTION_TIME,
            period_step_policy=PERIOD_STEP_POLICY_V1,
            decision=result.decision,
            execution_prices=result.execution_prices,
            rebalance_result=result.rebalance_result,
            next_portfolio_state=result.next_portfolio_state,
        )


def test_result_model_rejects_next_portfolio_state_mismatch() -> None:
    result = _run_step(_records_with_execution(_execution_record()))
    bad_next = BacktestPortfolioState(
        as_of=INTENDED_EXECUTION_TIME,
        cash_krw=Decimal("0"),
        holdings=(),
    )

    with pytest.raises(ValidationError):
        BacktestSinglePeriodStepResult(
            decision_time=DECISION_TIME,
            intended_execution_time=INTENDED_EXECUTION_TIME,
            period_step_policy=PERIOD_STEP_POLICY_V1,
            decision=result.decision,
            execution_prices=result.execution_prices,
            rebalance_result=result.rebalance_result,
            next_portfolio_state=bad_next,
        )


def test_result_has_no_nav_series_benchmark_or_performance_fields() -> None:
    forbidden = {
        "nav",
        "nav_series",
        "portfolio_value_series",
        "benchmark",
        "benchmark_relative",
        "benchmark_relative_metrics",
        "performance",
    }
    result_fields = set(BacktestSinglePeriodStepResult.model_fields)
    assert result_fields.isdisjoint(forbidden)


def test_module_does_not_implement_date_loop() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.For):
            assert "decision_date" not in ast.unparse(node.target)
            iter_text = ast.unparse(node.iter)
            assert "for date in" not in f"for {ast.unparse(node.target)} in {iter_text}"


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
        "benchmark_relative",
        "portfolio_value_series",
        "nav_series",
        "walk_forward",
        "for decision_date",
        "for date in",
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
        *[path for path in FOCUSED_TEST_FILES if path != "tests/test_backtest_period_step.py"],
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
