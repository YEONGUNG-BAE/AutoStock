"""One-period rules rebalance step composer for Phase 2c-6.

This module composes the already-frozen single-period building blocks for
exactly one period:

``source records -> single-step rules decision -> execution price selection
-> single-rebalance accounting -> next portfolio state``.

It does not iterate over multiple decision dates, generate a rebalance
schedule, produce a NAV series, compute benchmark-relative metrics, fetch
data, or use real data.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_data.source_records import InMemoryDateIdSourceReader
from backtest_engine.execution_prices import (
    BacktestExecutionPriceSlice,
    select_execution_prices_for_single_step_decision,
)
from backtest_engine.rebalance import (
    BacktestCostModel,
    BacktestPortfolioState,
    BacktestRebalanceResult,
    apply_single_rebalance_accounting,
)
from backtest_engine.rolling_features import RollingLongMaAssetConfig
from backtest_engine.single_step import (
    BacktestSingleStepDecision,
    make_rules_only_single_step_decision,
)
from backtest_engine.step_contract import RULES_ALLOCATOR_V1
from domain._datetime import require_timezone_aware_datetime
from domain.source import DateIdSourceRecord

PERIOD_STEP_POLICY_V1 = "single_period_rules_rebalance_step.v1"


class BacktestSinglePeriodStepResult(BaseModel):
    """Immutable result of one rules-only rebalance period step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_time: datetime
    intended_execution_time: datetime
    period_step_policy: Literal["single_period_rules_rebalance_step.v1"]
    decision: BacktestSingleStepDecision
    execution_prices: BacktestExecutionPriceSlice
    rebalance_result: BacktestRebalanceResult
    next_portfolio_state: BacktestPortfolioState

    @field_validator("decision_time", "intended_execution_time", mode="before")
    @classmethod
    def validate_timestamps(cls, value: Any, info) -> datetime:
        return require_timezone_aware_datetime(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_period_step(self) -> Self:
        if self.decision_time >= self.intended_execution_time:
            raise ValueError("decision_time must be before intended_execution_time.")

        if self.decision.decision_time != self.decision_time:
            raise ValueError("decision.decision_time must equal decision_time.")
        if self.decision.intended_execution_time != self.intended_execution_time:
            raise ValueError(
                "decision.intended_execution_time must equal intended_execution_time."
            )
        if self.execution_prices.decision_time != self.decision_time:
            raise ValueError("execution_prices.decision_time must equal decision_time.")
        if self.execution_prices.intended_execution_time != self.intended_execution_time:
            raise ValueError(
                "execution_prices.intended_execution_time must equal "
                "intended_execution_time."
            )
        if self.rebalance_result.decision_time != self.decision_time:
            raise ValueError("rebalance_result.decision_time must equal decision_time.")
        if self.rebalance_result.intended_execution_time != self.intended_execution_time:
            raise ValueError(
                "rebalance_result.intended_execution_time must equal "
                "intended_execution_time."
            )

        if self.rebalance_result.cash_krw_after != self.next_portfolio_state.cash_krw:
            raise ValueError(
                "rebalance_result.cash_krw_after must equal next_portfolio_state.cash_krw."
            )
        if self.rebalance_result.post_trade_holdings != self.next_portfolio_state.holdings:
            raise ValueError(
                "rebalance_result.post_trade_holdings must equal "
                "next_portfolio_state.holdings."
            )
        if self.next_portfolio_state.as_of != self.intended_execution_time:
            raise ValueError(
                "next_portfolio_state.as_of must equal intended_execution_time."
            )
        return self


def run_single_period_rules_rebalance_step(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    decision_time: datetime,
    intended_execution_time: datetime,
    rolling_asset_configs: Iterable[RollingLongMaAssetConfig],
    portfolio_state: BacktestPortfolioState,
    cost_model: BacktestCostModel,
    usdkrw_rate: Decimal,
    cash_asset_id: str,
    cash_min_weight: Decimal,
    rules_allocator_version: str = RULES_ALLOCATOR_V1,
) -> BacktestSinglePeriodStepResult:
    """Compose one rules-only rebalance period from source records.

    Chains the existing single-step decision builder, execution price selector,
    and rebalance accounting function. Does not recompute target weights,
    execution prices, or trades beyond those building blocks. Does not loop
    decision dates, generate a schedule, produce NAV, or fetch data.
    """

    step_source: InMemoryDateIdSourceReader | tuple[DateIdSourceRecord, ...]
    if isinstance(source, InMemoryDateIdSourceReader):
        step_source = source
    else:
        step_source = tuple(source)

    decision = make_rules_only_single_step_decision(
        step_source,
        decision_time=decision_time,
        intended_execution_time=intended_execution_time,
        rolling_asset_configs=rolling_asset_configs,
        cash_asset_id=cash_asset_id,
        cash_min_weight=cash_min_weight,
        rules_allocator_version=rules_allocator_version,
    )
    execution_prices = select_execution_prices_for_single_step_decision(
        step_source,
        decision=decision,
    )
    rebalance_result = apply_single_rebalance_accounting(
        decision=decision,
        execution_prices=execution_prices,
        portfolio_state=portfolio_state,
        cost_model=cost_model,
        usdkrw_rate=usdkrw_rate,
    )
    next_portfolio_state = BacktestPortfolioState(
        as_of=intended_execution_time,
        cash_krw=rebalance_result.cash_krw_after,
        holdings=rebalance_result.post_trade_holdings,
    )

    return BacktestSinglePeriodStepResult(
        decision_time=decision_time,
        intended_execution_time=intended_execution_time,
        period_step_policy=PERIOD_STEP_POLICY_V1,
        decision=decision,
        execution_prices=execution_prices,
        rebalance_result=rebalance_result,
        next_portfolio_state=next_portfolio_state,
    )
