"""Explicit-schedule walk-forward NAV series for Phase 2c-7.

This module repeats the frozen Phase 2c-6 one-period step over an explicit
schedule, carries portfolio state forward, and produces AutoStock-only NAV
points. It does not generate schedules, load real data, compute returns, or
compute benchmark-relative metrics.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_data.source_records import InMemoryDateIdSourceReader
from backtest_engine.period_step import (
    BacktestSinglePeriodStepResult,
    run_single_period_rules_rebalance_step,
)
from backtest_engine.rebalance import BacktestCostModel, BacktestPortfolioState
from backtest_engine.rolling_features import RollingLongMaAssetConfig
from domain._datetime import require_timezone_aware_datetime
from domain.source import DateIdSourceRecord

WALK_FORWARD_POLICY_V1 = "explicit_schedule_rules_walk_forward_nav.v1"

ZERO = Decimal("0")


class BacktestPeriodSpec(BaseModel):
    """One explicit walk-forward period with decision, execution, and FX rate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_time: datetime
    intended_execution_time: datetime
    usdkrw_rate: Decimal

    @field_validator("decision_time", "intended_execution_time", mode="before")
    @classmethod
    def validate_timestamps(cls, value: Any, info) -> datetime:
        return require_timezone_aware_datetime(value, field_name=info.field_name)

    @field_validator("usdkrw_rate", mode="before")
    @classmethod
    def validate_usdkrw_rate(cls, value: Any) -> Decimal:
        parsed = _to_decimal_no_float(value, field_name="usdkrw_rate")
        if parsed <= ZERO:
            raise ValueError("usdkrw_rate must be greater than 0.")
        return parsed

    @model_validator(mode="after")
    def validate_period_spec(self) -> Self:
        if self.decision_time >= self.intended_execution_time:
            raise ValueError("decision_time must be before intended_execution_time.")
        return self


class BacktestNavPoint(BaseModel):
    """One post-trade AutoStock portfolio NAV observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    portfolio_value_krw: Decimal
    cash_krw: Decimal
    total_cost_krw: Decimal

    @field_validator("as_of", mode="before")
    @classmethod
    def validate_as_of(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="as_of")

    @field_validator(
        "portfolio_value_krw",
        "cash_krw",
        "total_cost_krw",
        mode="before",
    )
    @classmethod
    def validate_non_negative_decimal_fields(cls, value: Any, info) -> Decimal:
        parsed = _to_decimal_no_float(value, field_name=info.field_name)
        if parsed < ZERO:
            raise ValueError(f"{info.field_name} must be >= 0.")
        return parsed


class BacktestWalkForwardResult(BaseModel):
    """Immutable walk-forward result with steps and AutoStock-only NAV points."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    walk_forward_policy: Literal["explicit_schedule_rules_walk_forward_nav.v1"]
    initial_portfolio_state: BacktestPortfolioState
    period_specs: tuple[BacktestPeriodSpec, ...]
    steps: tuple[BacktestSinglePeriodStepResult, ...]
    nav_points: tuple[BacktestNavPoint, ...]
    final_portfolio_state: BacktestPortfolioState
    total_fee_krw: Decimal
    total_tax_krw: Decimal
    total_fx_spread_krw: Decimal
    total_cost_krw: Decimal

    @field_validator(
        "total_fee_krw",
        "total_tax_krw",
        "total_fx_spread_krw",
        "total_cost_krw",
        mode="before",
    )
    @classmethod
    def validate_total_cost_fields(cls, value: Any, info) -> Decimal:
        parsed = _to_decimal_no_float(value, field_name=info.field_name)
        if parsed < ZERO:
            raise ValueError(f"{info.field_name} must be >= 0.")
        return parsed

    @model_validator(mode="after")
    def validate_walk_forward_result(self) -> Self:
        if not self.period_specs:
            raise ValueError("period_specs must not be empty.")
        if len(self.steps) != len(self.period_specs):
            raise ValueError("steps length must equal period_specs length.")
        if len(self.nav_points) != len(self.steps):
            raise ValueError("nav_points length must equal steps length.")

        _validate_explicit_schedule_order(
            self.period_specs,
            initial_as_of=self.initial_portfolio_state.as_of,
        )

        for index, (period, step, nav_point) in enumerate(
            zip(self.period_specs, self.steps, self.nav_points, strict=True)
        ):
            if step.decision_time != period.decision_time:
                raise ValueError(
                    f"step[{index}].decision_time must equal period_specs[{index}].decision_time."
                )
            if step.intended_execution_time != period.intended_execution_time:
                raise ValueError(
                    "step[{index}].intended_execution_time must equal "
                    f"period_specs[{index}].intended_execution_time."
                )
            if nav_point.as_of != step.intended_execution_time:
                raise ValueError(
                    f"nav_points[{index}].as_of must equal step[{index}].intended_execution_time."
                )
            if (
                nav_point.portfolio_value_krw
                != step.rebalance_result.post_trade_portfolio_value_krw
            ):
                raise ValueError(
                    f"nav_points[{index}].portfolio_value_krw must equal "
                    f"step[{index}].rebalance_result.post_trade_portfolio_value_krw."
                )
            if nav_point.cash_krw != step.next_portfolio_state.cash_krw:
                raise ValueError(
                    f"nav_points[{index}].cash_krw must equal "
                    f"step[{index}].next_portfolio_state.cash_krw."
                )
            if nav_point.total_cost_krw != step.rebalance_result.total_cost_krw:
                raise ValueError(
                    f"nav_points[{index}].total_cost_krw must equal "
                    f"step[{index}].rebalance_result.total_cost_krw."
                )

        if self.final_portfolio_state != self.steps[-1].next_portfolio_state:
            raise ValueError(
                "final_portfolio_state must equal steps[-1].next_portfolio_state."
            )

        expected_fee = sum(
            (step.rebalance_result.total_fee_krw for step in self.steps),
            ZERO,
        )
        expected_tax = sum(
            (step.rebalance_result.total_tax_krw for step in self.steps),
            ZERO,
        )
        expected_fx = sum(
            (step.rebalance_result.total_fx_spread_krw for step in self.steps),
            ZERO,
        )
        expected_cost = sum(
            (step.rebalance_result.total_cost_krw for step in self.steps),
            ZERO,
        )
        if self.total_fee_krw != expected_fee:
            raise ValueError("total_fee_krw must equal sum of step rebalance totals.")
        if self.total_tax_krw != expected_tax:
            raise ValueError("total_tax_krw must equal sum of step rebalance totals.")
        if self.total_fx_spread_krw != expected_fx:
            raise ValueError(
                "total_fx_spread_krw must equal sum of step rebalance totals."
            )
        if self.total_cost_krw != expected_cost:
            raise ValueError("total_cost_krw must equal sum of step rebalance totals.")

        return self


def run_explicit_schedule_rules_walk_forward_nav(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    period_specs: Iterable[BacktestPeriodSpec],
    rolling_asset_configs: Iterable[RollingLongMaAssetConfig],
    initial_portfolio_state: BacktestPortfolioState,
    cost_model: BacktestCostModel,
    cash_asset_id: str,
    cash_min_weight: Decimal,
) -> BacktestWalkForwardResult:
    """Run explicit-schedule walk-forward NAV over frozen one-period steps.

    Repeats ``run_single_period_rules_rebalance_step`` for each supplied period
    spec, carries portfolio state forward, and builds post-trade NAV points.
    Does not generate schedules, compute returns, or load real data.
    """

    step_source: InMemoryDateIdSourceReader | tuple[DateIdSourceRecord, ...]
    if isinstance(source, InMemoryDateIdSourceReader):
        step_source = source
    else:
        step_source = tuple(source)

    materialized_period_specs = tuple(period_specs)
    materialized_configs = tuple(rolling_asset_configs)

    if not materialized_period_specs:
        raise ValueError("period_specs must not be empty.")

    _validate_explicit_schedule_order(
        materialized_period_specs,
        initial_as_of=initial_portfolio_state.as_of,
    )

    steps: list[BacktestSinglePeriodStepResult] = []
    nav_points: list[BacktestNavPoint] = []
    current_portfolio_state = initial_portfolio_state

    for period in materialized_period_specs:
        step = run_single_period_rules_rebalance_step(
            step_source,
            decision_time=period.decision_time,
            intended_execution_time=period.intended_execution_time,
            rolling_asset_configs=materialized_configs,
            portfolio_state=current_portfolio_state,
            cost_model=cost_model,
            usdkrw_rate=period.usdkrw_rate,
            cash_asset_id=cash_asset_id,
            cash_min_weight=cash_min_weight,
        )
        steps.append(step)
        nav_points.append(
            BacktestNavPoint(
                as_of=step.intended_execution_time,
                portfolio_value_krw=step.rebalance_result.post_trade_portfolio_value_krw,
                cash_krw=step.next_portfolio_state.cash_krw,
                total_cost_krw=step.rebalance_result.total_cost_krw,
            )
        )
        current_portfolio_state = step.next_portfolio_state

    total_fee_krw = sum(
        (step.rebalance_result.total_fee_krw for step in steps),
        ZERO,
    )
    total_tax_krw = sum(
        (step.rebalance_result.total_tax_krw for step in steps),
        ZERO,
    )
    total_fx_spread_krw = sum(
        (step.rebalance_result.total_fx_spread_krw for step in steps),
        ZERO,
    )
    total_cost_krw = sum(
        (step.rebalance_result.total_cost_krw for step in steps),
        ZERO,
    )

    return BacktestWalkForwardResult(
        walk_forward_policy=WALK_FORWARD_POLICY_V1,
        initial_portfolio_state=initial_portfolio_state,
        period_specs=materialized_period_specs,
        steps=tuple(steps),
        nav_points=tuple(nav_points),
        final_portfolio_state=current_portfolio_state,
        total_fee_krw=total_fee_krw,
        total_tax_krw=total_tax_krw,
        total_fx_spread_krw=total_fx_spread_krw,
        total_cost_krw=total_cost_krw,
    )


def _validate_explicit_schedule_order(
    period_specs: tuple[BacktestPeriodSpec, ...],
    *,
    initial_as_of: datetime,
) -> None:
    """Validate explicit schedule ordering constraints."""

    if period_specs[0].decision_time < initial_as_of:
        raise ValueError(
            "first period decision_time must be >= initial_portfolio_state.as_of."
        )

    for index in range(1, len(period_specs)):
        previous = period_specs[index - 1]
        current = period_specs[index]
        if current.decision_time <= previous.decision_time:
            raise ValueError("period decision_time values must be strictly increasing.")
        if current.intended_execution_time <= previous.intended_execution_time:
            raise ValueError(
                "period intended_execution_time values must be strictly increasing."
            )
        if current.decision_time < previous.intended_execution_time:
            raise ValueError(
                "each period decision_time must be >= previous intended_execution_time."
            )


def _to_decimal_no_float(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} must be a Decimal value; floats are not accepted.")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{field_name} must be a valid Decimal value.") from exc
    else:
        raise ValueError(f"{field_name} must be a valid Decimal value.")

    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal value.")
    return parsed
