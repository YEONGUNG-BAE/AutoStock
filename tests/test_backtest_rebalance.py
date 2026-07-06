from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_engine import (  # noqa: E402
    COST_MODEL_V1,
    EXECUTION_PRICE_POLICY_V1,
    REBALANCE_ACCOUNTING_POLICY_V1,
    RULES_ALLOCATOR_V1,
    BacktestAssetFeature,
    BacktestCostModel,
    BacktestExecutionPrice,
    BacktestExecutionPriceSlice,
    BacktestFeatureSnapshot,
    BacktestHolding,
    BacktestPortfolioState,
    BacktestRebalanceResult,
    BacktestSingleStepDecision,
    BacktestTargetWeight,
    BacktestTargetWeights,
    BacktestTrade,
    ObservationSpacingReport,
    SnapshotAssetConfig,
    apply_single_rebalance_accounting,
)
from backtest_engine.rebalance import (  # noqa: E402
    _canonical_total_cost_krw,
    _sum_decimal,
)

DECISION_TIME = datetime(2020, 4, 30, 0, 0, tzinfo=UTC)
INTENDED_EXECUTION_TIME = datetime(2020, 5, 31, 0, 0, tzinfo=UTC)
PORTFOLIO_AS_OF = datetime(2020, 5, 30, 12, 0, tzinfo=UTC)
USDKRW = Decimal("1300")

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "backtest_engine" / "rebalance.py"

FOCUSED_TEST_FILES = (
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


def _cost_model(
    *,
    fee_bps: Decimal = Decimal("10"),
    kr_sell_tax_bps: Decimal = Decimal("23"),
    fx_spread_bps: Decimal = Decimal("15"),
) -> BacktestCostModel:
    return BacktestCostModel(
        cost_model_version=COST_MODEL_V1,
        fee_bps=fee_bps,
        kr_sell_tax_bps=kr_sell_tax_bps,
        fx_spread_bps=fx_spread_bps,
    )


def _execution_price(
    *,
    asset_id: str = "asset_us",
    symbol: str = "SYN_US",
    market: str = "US",
    execution_price: Decimal = Decimal("100"),
) -> BacktestExecutionPrice:
    return BacktestExecutionPrice(
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        source_date=date(2020, 5, 31),
        source_timestamp=INTENDED_EXECUTION_TIME,
        execution_price=execution_price,
        source_name="monthly_synthetic",
        date_id="200531-1",
    )


def _execution_slice(
    *prices: BacktestExecutionPrice,
) -> BacktestExecutionPriceSlice:
    return BacktestExecutionPriceSlice(
        decision_time=DECISION_TIME,
        intended_execution_time=INTENDED_EXECUTION_TIME,
        execution_policy=EXECUTION_PRICE_POLICY_V1,
        prices=prices,
    )


def _target_weights(
    weights: dict[str, Decimal],
    *,
    asset_id: str,
    cash_asset_id: str = "cash",
) -> BacktestTargetWeights:
    asset_weight = weights[asset_id]
    cash_weight = weights.get(cash_asset_id, Decimal("1") - asset_weight)
    return BacktestTargetWeights(
        decision_time=DECISION_TIME,
        allocator_version=RULES_ALLOCATOR_V1,
        weights=(
            BacktestTargetWeight(asset_id=asset_id, weight=asset_weight),
            BacktestTargetWeight(asset_id=cash_asset_id, weight=cash_weight),
        ),
    )


def _decision(
    *,
    target: dict[str, Decimal] | None = None,
    asset_id: str = "asset_us",
    symbol: str = "SYN_US",
    market: str = "US",
    long_ma: Decimal = Decimal("95"),
) -> BacktestSingleStepDecision:
    target = target or {asset_id: Decimal("0.70"), "cash": Decimal("0.30")}
    configs = (
        SnapshotAssetConfig(
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            long_ma=long_ma,
            risk_on_weight=Decimal("0.70"),
            risk_off_weight=Decimal("0.35"),
            min_weight=Decimal("0"),
            max_weight=Decimal("0.80"),
        ),
    )
    feature = BacktestFeatureSnapshot(
        decision_time=DECISION_TIME,
        assets=(
            BacktestAssetFeature(
                asset_id=asset_id,
                as_of=DECISION_TIME,
                current_price=Decimal("104"),
                long_ma=long_ma,
                risk_on_weight=Decimal("0.70"),
                risk_off_weight=Decimal("0.35"),
                min_weight=Decimal("0"),
                max_weight=Decimal("0.80"),
            ),
        ),
        cash_asset_id="cash",
        cash_min_weight=Decimal("0.05"),
    )
    return BacktestSingleStepDecision(
        decision_time=DECISION_TIME,
        intended_execution_time=INTENDED_EXECUTION_TIME,
        allocator_version=RULES_ALLOCATOR_V1,
        observation_spacing_reports=(
            ObservationSpacingReport(
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                frequency="monthly",
                lookback_count=3,
                period_keys=("2020-02", "2020-03", "2020-04"),
            ),
        ),
        snapshot_asset_configs=configs,
        feature_snapshot=feature,
        target_weights=_target_weights(target, asset_id=asset_id),
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


def _rebalance(
    *,
    decision: BacktestSingleStepDecision | None = None,
    execution_prices: BacktestExecutionPriceSlice | None = None,
    portfolio_state: BacktestPortfolioState | None = None,
    cost_model: BacktestCostModel | None = None,
    usdkrw_rate: Decimal = USDKRW,
) -> BacktestRebalanceResult:
    decision = decision or _decision()
    execution_prices = execution_prices or _execution_slice(_execution_price())
    portfolio_state = portfolio_state or _portfolio()
    cost_model = cost_model or _cost_model()
    return apply_single_rebalance_accounting(
        decision=decision,
        execution_prices=execution_prices,
        portfolio_state=portfolio_state,
        cost_model=cost_model,
        usdkrw_rate=usdkrw_rate,
    )


def test_builds_rebalance_result_from_decision_and_execution_prices() -> None:
    result = _rebalance()

    assert isinstance(result, BacktestRebalanceResult)
    assert result.decision_time == DECISION_TIME
    assert result.intended_execution_time == INTENDED_EXECUTION_TIME
    assert result.accounting_policy == REBALANCE_ACCOUNTING_POLICY_V1
    assert result.cost_model_version == COST_MODEL_V1
    assert len(result.trades) == 1
    assert result.trades[0].side == "BUY"


def test_pre_trade_portfolio_value_from_cash_plus_holdings() -> None:
    portfolio = _portfolio(
        cash_krw=Decimal("500000"),
        holdings=(BacktestHolding(asset_id="asset_us", quantity=Decimal("1")),),
    )
    result = _rebalance(portfolio_state=portfolio)

    assert result.pre_trade_portfolio_value_krw == Decimal("500000") + Decimal("1") * Decimal(
        "100"
    ) * USDKRW


def test_kr_asset_valuation_uses_execution_price_directly() -> None:
    decision = _decision(
        target={"asset_kr": Decimal("1.0")},
        asset_id="asset_kr",
        symbol="SYN_KR",
        market="KR",
        long_ma=Decimal("50000"),
    )
    portfolio = _portfolio(
        cash_krw=Decimal("0"),
        holdings=(BacktestHolding(asset_id="asset_kr", quantity=Decimal("2")),),
    )
    prices = _execution_slice(
        _execution_price(
            asset_id="asset_kr",
            symbol="SYN_KR",
            market="KR",
            execution_price=Decimal("100000"),
        )
    )

    result = _rebalance(
        decision=decision,
        execution_prices=prices,
        portfolio_state=portfolio,
    )

    assert result.pre_trade_portfolio_value_krw == Decimal("200000")


def test_us_gold_valuation_uses_execution_price_times_usdkrw() -> None:
    portfolio = _portfolio(
        cash_krw=Decimal("0"),
        holdings=(BacktestHolding(asset_id="asset_us", quantity=Decimal("3")),),
    )

    result = _rebalance(portfolio_state=portfolio)

    assert result.pre_trade_portfolio_value_krw == Decimal("3") * Decimal("100") * USDKRW


def test_target_notional_uses_decision_target_weights() -> None:
    decision = _decision(target={"asset_us": Decimal("0.50"), "cash": Decimal("0.50")})
    portfolio = _portfolio(cash_krw=Decimal("1000000"))

    result = _rebalance(decision=decision, portfolio_state=portfolio)

    assert result.trades[0].gross_notional_krw == Decimal("500000")


def test_buy_trade_decreases_cash_by_gross_notional_plus_costs() -> None:
    portfolio = _portfolio(cash_krw=Decimal("1000000"))
    cost_model = _cost_model(fee_bps=Decimal("100"), fx_spread_bps=Decimal("50"))

    result = _rebalance(portfolio_state=portfolio, cost_model=cost_model)
    trade = result.trades[0]

    expected_cash = portfolio.cash_krw - trade.gross_notional_krw - trade.total_cost_krw
    assert result.cash_krw_after == expected_cash


def test_sell_trade_increases_cash_by_gross_notional_minus_costs() -> None:
    decision = _decision(target={"asset_us": Decimal("0.30"), "cash": Decimal("0.70")})
    portfolio = _portfolio(
        cash_krw=Decimal("0"),
        holdings=(BacktestHolding(asset_id="asset_us", quantity=Decimal("10")),),
    )
    cost_model = _cost_model(fee_bps=Decimal("100"), fx_spread_bps=Decimal("50"))

    result = _rebalance(
        decision=decision,
        portfolio_state=portfolio,
        cost_model=cost_model,
    )
    trade = result.trades[0]

    assert trade.side == "SELL"
    expected_cash = trade.gross_notional_krw - trade.total_cost_krw
    assert result.cash_krw_after == expected_cash


def test_buy_increases_quantity() -> None:
    result = _rebalance()

    assert len(result.post_trade_holdings) == 1
    assert result.post_trade_holdings[0].quantity == result.trades[0].quantity


def test_sell_decreases_quantity() -> None:
    decision = _decision(target={"asset_us": Decimal("0.20"), "cash": Decimal("0.80")})
    portfolio = _portfolio(
        cash_krw=Decimal("0"),
        holdings=(BacktestHolding(asset_id="asset_us", quantity=Decimal("10")),),
    )

    result = _rebalance(decision=decision, portfolio_state=portfolio)

    assert result.post_trade_holdings[0].quantity < Decimal("10")


def test_fee_applies_to_all_trades() -> None:
    cost_model = _cost_model(fee_bps=Decimal("25"))
    result = _rebalance(cost_model=cost_model)

    assert result.trades[0].fee_krw == result.trades[0].gross_notional_krw * Decimal("25") / Decimal(
        "10000"
    )


def test_kr_sell_tax_applies_only_to_kr_sells() -> None:
    decision = _decision(
        target={"asset_kr": Decimal("0.20"), "cash": Decimal("0.80")},
        asset_id="asset_kr",
        symbol="SYN_KR",
        market="KR",
        long_ma=Decimal("50000"),
    )
    portfolio = _portfolio(
        cash_krw=Decimal("0"),
        holdings=(BacktestHolding(asset_id="asset_kr", quantity=Decimal("10")),),
    )
    prices = _execution_slice(
        _execution_price(
            asset_id="asset_kr",
            symbol="SYN_KR",
            market="KR",
            execution_price=Decimal("100000"),
        )
    )
    cost_model = _cost_model(kr_sell_tax_bps=Decimal("230"))

    result = _rebalance(
        decision=decision,
        execution_prices=prices,
        portfolio_state=portfolio,
        cost_model=cost_model,
    )

    assert result.trades[0].side == "SELL"
    assert result.trades[0].tax_krw > Decimal("0")


def test_kr_buy_has_no_sell_tax() -> None:
    decision = _decision(
        target={"asset_kr": Decimal("0.70"), "cash": Decimal("0.30")},
        asset_id="asset_kr",
        symbol="SYN_KR",
        market="KR",
        long_ma=Decimal("50000"),
    )
    prices = _execution_slice(
        _execution_price(
            asset_id="asset_kr",
            symbol="SYN_KR",
            market="KR",
            execution_price=Decimal("100000"),
        )
    )
    cost_model = _cost_model(kr_sell_tax_bps=Decimal("230"))

    result = _rebalance(
        decision=decision,
        execution_prices=prices,
        portfolio_state=_portfolio(cash_krw=Decimal("10000000")),
        cost_model=cost_model,
    )

    assert result.trades[0].side == "BUY"
    assert result.trades[0].tax_krw == Decimal("0")


def test_us_gold_trades_have_fx_spread() -> None:
    cost_model = _cost_model(fx_spread_bps=Decimal("40"))

    result = _rebalance(cost_model=cost_model)

    assert result.trades[0].fx_spread_krw == result.trades[0].gross_notional_krw * Decimal(
        "40"
    ) / Decimal("10000")


def test_kr_trades_have_no_fx_spread() -> None:
    decision = _decision(
        target={"asset_kr": Decimal("0.70"), "cash": Decimal("0.30")},
        asset_id="asset_kr",
        symbol="SYN_KR",
        market="KR",
        long_ma=Decimal("50000"),
    )
    prices = _execution_slice(
        _execution_price(
            asset_id="asset_kr",
            symbol="SYN_KR",
            market="KR",
            execution_price=Decimal("100000"),
        )
    )
    cost_model = _cost_model(fx_spread_bps=Decimal("40"))

    result = _rebalance(
        decision=decision,
        execution_prices=prices,
        portfolio_state=_portfolio(cash_krw=Decimal("10000000")),
        cost_model=cost_model,
    )

    assert result.trades[0].fx_spread_krw == Decimal("0")


def test_cash_target_respected_through_residual_cash_after_trades_and_costs() -> None:
    decision = _decision(target={"asset_us": Decimal("0.40"), "cash": Decimal("0.60")})
    portfolio = _portfolio(cash_krw=Decimal("1000000"))

    result = _rebalance(decision=decision, portfolio_state=portfolio)

    cash_target = Decimal("0.60") * result.pre_trade_portfolio_value_krw
    assert result.cash_krw_after == cash_target - result.total_cost_krw


def test_total_cost_fields_equal_sum_of_trade_costs() -> None:
    result = _rebalance()

    assert result.total_fee_krw == _sum_decimal(trade.fee_krw for trade in result.trades)
    assert result.total_tax_krw == _sum_decimal(trade.tax_krw for trade in result.trades)
    assert result.total_fx_spread_krw == _sum_decimal(
        trade.fx_spread_krw for trade in result.trades
    )
    assert result.total_cost_krw == _sum_decimal(
        trade.total_cost_krw for trade in result.trades
    )
    assert result.total_cost_krw == _canonical_total_cost_krw(
        result.total_fee_krw,
        result.total_tax_krw,
        result.total_fx_spread_krw,
    )
    assert result.total_cost_krw == _canonical_total_cost_krw(
        result.total_fee_krw,
        result.total_tax_krw,
        result.total_fx_spread_krw,
    )


def test_post_trade_value_equals_pre_trade_value_minus_total_costs() -> None:
    result = _rebalance()

    assert (
        result.post_trade_portfolio_value_krw
        == result.pre_trade_portfolio_value_krw - result.total_cost_krw
    )


def test_raises_on_negative_cash_after_costs() -> None:
    decision = _decision(target={"asset_us": Decimal("1.0")})
    portfolio = _portfolio(cash_krw=Decimal("1000"))

    with pytest.raises(ValueError, match="cash would become negative"):
        _rebalance(decision=decision, portfolio_state=portfolio)


def test_raises_on_selling_more_than_current_holdings() -> None:
    from unittest.mock import patch

    decision = _decision(target={"asset_us": Decimal("0.10"), "cash": Decimal("0.90")})
    portfolio = _portfolio(
        cash_krw=Decimal("0"),
        holdings=(BacktestHolding(asset_id="asset_us", quantity=Decimal("1")),),
    )

    with patch(
        "backtest_engine.rebalance._quantity_from_notional_krw",
        return_value=Decimal("2"),
    ):
        with pytest.raises(ValueError, match="cannot sell more than current holding"):
            _rebalance(decision=decision, portfolio_state=portfolio)


def test_raises_when_execution_price_asset_ids_do_not_match_non_cash_targets() -> None:
    decision = _decision()
    prices = _execution_slice(
        _execution_price(asset_id="wrong_asset"),
    )

    with pytest.raises(ValueError, match="execution_prices asset ids must match"):
        _rebalance(decision=decision, execution_prices=prices)


def test_raises_on_decision_time_mismatch() -> None:
    prices = BacktestExecutionPriceSlice(
        decision_time=DECISION_TIME + timedelta(days=1),
        intended_execution_time=INTENDED_EXECUTION_TIME,
        execution_policy=EXECUTION_PRICE_POLICY_V1,
        prices=(_execution_price(),),
    )

    with pytest.raises(ValueError, match="decision.decision_time"):
        _rebalance(execution_prices=prices)


def test_raises_on_intended_execution_time_mismatch() -> None:
    prices = BacktestExecutionPriceSlice.model_construct(
        decision_time=DECISION_TIME,
        intended_execution_time=INTENDED_EXECUTION_TIME + timedelta(days=1),
        execution_policy=EXECUTION_PRICE_POLICY_V1,
        prices=(_execution_price(),),
    )

    with pytest.raises(ValueError, match="decision.intended_execution_time"):
        _rebalance(execution_prices=prices)


def test_raises_on_non_positive_usdkrw_rate() -> None:
    with pytest.raises(ValueError, match="usdkrw_rate must be greater than 0"):
        _rebalance(usdkrw_rate=Decimal("0"))


def test_raises_on_float_usdkrw_rate() -> None:
    with pytest.raises(ValueError, match="floats are not accepted"):
        apply_single_rebalance_accounting(
            decision=_decision(),
            execution_prices=_execution_slice(_execution_price()),
            portfolio_state=_portfolio(),
            cost_model=_cost_model(),
            usdkrw_rate=1300.0,  # type: ignore[arg-type]
        )


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    holding = BacktestHolding(asset_id="asset_us", quantity=Decimal("1"))
    with pytest.raises(ValidationError):
        holding.quantity = Decimal("2")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        BacktestHolding(asset_id="asset_us", quantity=Decimal("1"), extra=1)  # type: ignore[call-arg]

    result = _rebalance()
    with pytest.raises(ValidationError):
        result.cash_krw_after = Decimal("0")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        BacktestRebalanceResult(
            decision_time=DECISION_TIME,
            intended_execution_time=INTENDED_EXECUTION_TIME,
            accounting_policy=REBALANCE_ACCOUNTING_POLICY_V1,
            cost_model_version=COST_MODEL_V1,
            pre_trade_portfolio_value_krw=Decimal("1"),
            post_trade_portfolio_value_krw=Decimal("1"),
            cash_krw_before=Decimal("1"),
            cash_krw_after=Decimal("1"),
            trades=(),
            post_trade_holdings=(),
            total_fee_krw=Decimal("0"),
            total_tax_krw=Decimal("0"),
            total_fx_spread_krw=Decimal("0"),
            total_cost_krw=Decimal("0"),
            nav_series=(),  # type: ignore[call-arg]
        )


def test_models_reject_floats_in_decimal_fields() -> None:
    with pytest.raises(ValidationError):
        BacktestHolding(asset_id="asset_us", quantity=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BacktestPortfolioState(
            as_of=PORTFOLIO_AS_OF,
            cash_krw=1000.0,  # type: ignore[arg-type]
            holdings=(),
        )


def test_holdings_require_unique_asset_ids() -> None:
    with pytest.raises(ValidationError):
        BacktestPortfolioState(
            as_of=PORTFOLIO_AS_OF,
            cash_krw=Decimal("0"),
            holdings=(
                BacktestHolding(asset_id="asset_us", quantity=Decimal("1")),
                BacktestHolding(asset_id="asset_us", quantity=Decimal("2")),
            ),
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
    result_fields = set(BacktestRebalanceResult.model_fields)
    assert result_fields.isdisjoint(forbidden)


def test_function_does_not_implement_date_loop() -> None:
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


def test_trade_model_validates_total_cost_sum_and_market_usdkrw_rules() -> None:
    with pytest.raises(ValidationError):
        BacktestTrade(
            asset_id="asset_us",
            symbol="SYN_US",
            market="US",
            side="BUY",
            quantity=Decimal("1"),
            execution_price=Decimal("100"),
            usdkrw_rate=USDKRW,
            gross_notional_krw=Decimal("130000"),
            fee_krw=Decimal("10"),
            tax_krw=Decimal("0"),
            fx_spread_krw=Decimal("5"),
            total_cost_krw=Decimal("20"),
        )

    with pytest.raises(ValidationError):
        BacktestTrade(
            asset_id="asset_kr",
            symbol="SYN_KR",
            market="KR",
            side="SELL",
            quantity=Decimal("1"),
            execution_price=Decimal("100000"),
            usdkrw_rate=USDKRW,
            gross_notional_krw=Decimal("100000"),
            fee_krw=Decimal("10"),
            tax_krw=Decimal("0"),
            fx_spread_krw=Decimal("0"),
            total_cost_krw=Decimal("10"),
        )


def test_post_trade_holdings_exclude_zero_quantities_and_follow_decision_order() -> None:
    decision = _decision(target={"asset_us": Decimal("0.30"), "cash": Decimal("0.70")})
    portfolio = _portfolio(
        cash_krw=Decimal("0"),
        holdings=(BacktestHolding(asset_id="asset_us", quantity=Decimal("10")),),
    )

    result = _rebalance(decision=decision, portfolio_state=portfolio)

    assert tuple(h.asset_id for h in result.post_trade_holdings) == ("asset_us",)
    assert result.post_trade_holdings[0].quantity > Decimal("0")


def test_gold_market_uses_fx_spread_and_usdkrw_rate() -> None:
    decision = _decision(
        target={"asset_gold": Decimal("0.70"), "cash": Decimal("0.30")},
        asset_id="asset_gold",
        symbol="SYN_GOLD",
        market="GOLD",
        long_ma=Decimal("1800"),
    )
    prices = _execution_slice(
        _execution_price(
            asset_id="asset_gold",
            symbol="SYN_GOLD",
            market="GOLD",
            execution_price=Decimal("2000"),
        )
    )
    cost_model = _cost_model(fx_spread_bps=Decimal("30"))

    result = _rebalance(
        decision=decision,
        execution_prices=prices,
        portfolio_state=_portfolio(cash_krw=Decimal("5000000")),
        cost_model=cost_model,
    )

    assert result.trades[0].usdkrw_rate == USDKRW
    assert result.trades[0].fx_spread_krw > Decimal("0")


def _observation_report(
    *,
    asset_id: str,
    symbol: str,
    market: str,
) -> ObservationSpacingReport:
    return ObservationSpacingReport(
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        frequency="monthly",
        lookback_count=3,
        period_keys=("2020-02", "2020-03", "2020-04"),
    )


def _multi_market_decision() -> BacktestSingleStepDecision:
    configs = (
        SnapshotAssetConfig(
            asset_id="asset_kr",
            symbol="SYN_KR",
            market="KR",
            long_ma=Decimal("50000"),
            risk_on_weight=Decimal("0.30"),
            risk_off_weight=Decimal("0.20"),
            min_weight=Decimal("0"),
            max_weight=Decimal("0.50"),
        ),
        SnapshotAssetConfig(
            asset_id="asset_us",
            symbol="SYN_US",
            market="US",
            long_ma=Decimal("95"),
            risk_on_weight=Decimal("0.25"),
            risk_off_weight=Decimal("0.15"),
            min_weight=Decimal("0"),
            max_weight=Decimal("0.50"),
        ),
        SnapshotAssetConfig(
            asset_id="asset_gold",
            symbol="SYN_GOLD",
            market="GOLD",
            long_ma=Decimal("1800"),
            risk_on_weight=Decimal("0.25"),
            risk_off_weight=Decimal("0.15"),
            min_weight=Decimal("0"),
            max_weight=Decimal("0.50"),
        ),
    )
    features = tuple(
        BacktestAssetFeature(
            asset_id=config.asset_id,
            as_of=DECISION_TIME,
            current_price=Decimal("100"),
            long_ma=config.long_ma,
            risk_on_weight=config.risk_on_weight,
            risk_off_weight=config.risk_off_weight,
            min_weight=config.min_weight,
            max_weight=config.max_weight,
        )
        for config in configs
    )
    return BacktestSingleStepDecision(
        decision_time=DECISION_TIME,
        intended_execution_time=INTENDED_EXECUTION_TIME,
        allocator_version=RULES_ALLOCATOR_V1,
        observation_spacing_reports=tuple(
            _observation_report(
                asset_id=config.asset_id,
                symbol=config.symbol,
                market=config.market,
            )
            for config in configs
        ),
        snapshot_asset_configs=configs,
        feature_snapshot=BacktestFeatureSnapshot(
            decision_time=DECISION_TIME,
            assets=features,
            cash_asset_id="cash",
            cash_min_weight=Decimal("0.05"),
        ),
        target_weights=_multi_market_target_weights(),
    )


def _multi_market_execution_slice() -> BacktestExecutionPriceSlice:
    return _execution_slice(
        _execution_price(
            asset_id="asset_kr",
            symbol="SYN_KR",
            market="KR",
            execution_price=Decimal("98765.432109876543210987654321987654321"),
        ),
        _execution_price(
            asset_id="asset_us",
            symbol="SYN_US",
            market="US",
            execution_price=Decimal("100"),
        ),
        _execution_price(
            asset_id="asset_gold",
            symbol="SYN_GOLD",
            market="GOLD",
            execution_price=Decimal("2000"),
        ),
    )


def _multi_market_target_weights() -> BacktestTargetWeights:
    return BacktestTargetWeights(
        decision_time=DECISION_TIME,
        allocator_version=RULES_ALLOCATOR_V1,
        weights=(
            BacktestTargetWeight(asset_id="asset_kr", weight=Decimal("0.20")),
            BacktestTargetWeight(asset_id="asset_us", weight=Decimal("0.30")),
            BacktestTargetWeight(asset_id="asset_gold", weight=Decimal("0.20")),
            BacktestTargetWeight(asset_id="cash", weight=Decimal("0.30")),
        ),
    )


def _multi_market_portfolio() -> BacktestPortfolioState:
    return _portfolio(
        cash_krw=Decimal("10000000"),
        holdings=(
            BacktestHolding(asset_id="asset_kr", quantity=Decimal("200")),
            BacktestHolding(asset_id="asset_us", quantity=Decimal("2")),
            BacktestHolding(asset_id="asset_gold", quantity=Decimal("10")),
        ),
    )


LONG_USDKRW = Decimal(
    "1345.678901234567890123456789012345678901234567890123456789012345678901234567890"
)


def test_long_decimal_prices_preserve_aggregate_cost_invariants() -> None:
    decision = _multi_market_decision()
    execution_prices = _multi_market_execution_slice()
    portfolio = _multi_market_portfolio()
    cost_model = _cost_model(
        fee_bps=Decimal("10"),
        kr_sell_tax_bps=Decimal("23"),
        fx_spread_bps=Decimal("15"),
    )

    result = apply_single_rebalance_accounting(
        decision=decision,
        execution_prices=execution_prices,
        portfolio_state=portfolio,
        cost_model=cost_model,
        usdkrw_rate=LONG_USDKRW,
    )

    assert len(result.trades) == 3
    for trade in result.trades:
        assert trade.total_cost_krw == _canonical_total_cost_krw(
            trade.fee_krw,
            trade.tax_krw,
            trade.fx_spread_krw,
        )
    assert result.total_cost_krw == _canonical_total_cost_krw(
        result.total_fee_krw,
        result.total_tax_krw,
        result.total_fx_spread_krw,
    )
    assert result.total_cost_krw == _sum_decimal(
        trade.total_cost_krw for trade in result.trades
    )


def test_multi_market_mixed_side_trades_preserve_aggregate_cost_invariants() -> None:
    decision = _multi_market_decision()
    execution_prices = _multi_market_execution_slice()
    portfolio = _multi_market_portfolio()
    cost_model = _cost_model(
        fee_bps=Decimal("10"),
        kr_sell_tax_bps=Decimal("23"),
        fx_spread_bps=Decimal("15"),
    )

    result = apply_single_rebalance_accounting(
        decision=decision,
        execution_prices=execution_prices,
        portfolio_state=portfolio,
        cost_model=cost_model,
        usdkrw_rate=LONG_USDKRW,
    )

    trade_by_market = {trade.market: trade for trade in result.trades}
    assert trade_by_market["KR"].side == "SELL"
    assert trade_by_market["US"].side == "BUY"
    assert trade_by_market["GOLD"].side == "SELL"
    assert trade_by_market["KR"].tax_krw > Decimal("0")
    assert trade_by_market["US"].fx_spread_krw > Decimal("0")
    assert trade_by_market["GOLD"].fx_spread_krw > Decimal("0")
    assert all(trade.fee_krw > Decimal("0") for trade in result.trades)
    assert result.total_cost_krw == _canonical_total_cost_krw(
        result.total_fee_krw,
        result.total_tax_krw,
        result.total_fx_spread_krw,
    )


def test_focused_regression_suite_passes() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    command = [
        "uv",
        "run",
        "pytest",
        *[path for path in FOCUSED_TEST_FILES if path != "tests/test_backtest_rebalance.py"],
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
