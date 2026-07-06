"""Single-rebalance portfolio accounting for Phase 2c-5.

This module applies one already-built ``BacktestSingleStepDecision`` and one
``BacktestExecutionPriceSlice`` to one ``BacktestPortfolioState`` using an
explicit ``BacktestCostModel`` and ``usdkrw_rate``. It computes trades, costs,
post-trade holdings/cash, and one post-trade portfolio value. It does not
iterate over multiple decision dates, produce a NAV series, compute
benchmark-relative metrics, fetch data, or use real data.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_engine.execution_prices import BacktestExecutionPriceSlice
from backtest_engine.single_step import BacktestSingleStepDecision
from domain._datetime import require_timezone_aware_datetime

REBALANCE_ACCOUNTING_POLICY_V1 = "single_rebalance_target_weight_accounting.v1"
COST_MODEL_V1 = "simple_proportional_fee_sell_tax_fx_spread.v1"

ZERO = Decimal("0")
BPS_DIVISOR = Decimal("10000")
FX_MARKETS = frozenset({"US", "GOLD"})


class BacktestHolding(BaseModel):
    """One post-trade or pre-trade asset quantity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    quantity: Decimal

    @field_validator("asset_id", mode="before")
    @classmethod
    def validate_asset_id(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("asset_id must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValueError("asset_id must not be blank.")
        return normalized

    @field_validator("quantity", mode="before")
    @classmethod
    def validate_quantity(cls, value: Any) -> Decimal:
        parsed = _to_decimal_no_float(value, field_name="quantity")
        if parsed < ZERO:
            raise ValueError("quantity must be >= 0.")
        return parsed


class BacktestPortfolioState(BaseModel):
    """Immutable portfolio state before one rebalance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    cash_krw: Decimal
    holdings: tuple[BacktestHolding, ...]

    @field_validator("as_of", mode="before")
    @classmethod
    def validate_as_of(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="as_of")

    @field_validator("cash_krw", mode="before")
    @classmethod
    def validate_cash_krw(cls, value: Any) -> Decimal:
        parsed = _to_decimal_no_float(value, field_name="cash_krw")
        if parsed < ZERO:
            raise ValueError("cash_krw must be >= 0.")
        return parsed

    @model_validator(mode="after")
    def validate_holdings(self) -> Self:
        asset_ids = tuple(holding.asset_id for holding in self.holdings)
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("holdings must have unique asset ids.")
        return self


class BacktestCostModel(BaseModel):
    """Explicit proportional fee, KR sell tax, and US/GOLD FX spread model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cost_model_version: Literal["simple_proportional_fee_sell_tax_fx_spread.v1"]
    fee_bps: Decimal
    kr_sell_tax_bps: Decimal
    fx_spread_bps: Decimal

    @field_validator("fee_bps", "kr_sell_tax_bps", "fx_spread_bps", mode="before")
    @classmethod
    def validate_bps_fields(cls, value: Any, info) -> Decimal:
        parsed = _to_decimal_no_float(value, field_name=info.field_name)
        if parsed < ZERO:
            raise ValueError(f"{info.field_name} must be >= 0.")
        return parsed


class BacktestTrade(BaseModel):
    """One executed trade with explicit KRW cost breakdown."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    symbol: str
    market: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    execution_price: Decimal
    usdkrw_rate: Decimal | None
    gross_notional_krw: Decimal
    fee_krw: Decimal
    tax_krw: Decimal
    fx_spread_krw: Decimal
    total_cost_krw: Decimal

    @field_validator(
        "asset_id",
        "symbol",
        "market",
        mode="before",
    )
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{info.field_name} must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be blank.")
        return normalized

    @field_validator(
        "quantity",
        "execution_price",
        "gross_notional_krw",
        "fee_krw",
        "tax_krw",
        "fx_spread_krw",
        "total_cost_krw",
        mode="before",
    )
    @classmethod
    def validate_non_negative_decimal_fields(cls, value: Any, info) -> Decimal:
        parsed = _to_decimal_no_float(value, field_name=info.field_name)
        if info.field_name in {"fee_krw", "tax_krw", "fx_spread_krw", "total_cost_krw"}:
            if parsed < ZERO:
                raise ValueError(f"{info.field_name} must be >= 0.")
            return parsed
        if parsed <= ZERO:
            raise ValueError(f"{info.field_name} must be greater than 0.")
        return parsed

    @field_validator("usdkrw_rate", mode="before")
    @classmethod
    def validate_usdkrw_rate(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        parsed = _to_decimal_no_float(value, field_name="usdkrw_rate")
        if parsed <= ZERO:
            raise ValueError("usdkrw_rate must be greater than 0.")
        return parsed

    @model_validator(mode="after")
    def validate_trade(self) -> Self:
        if self.total_cost_krw != self.fee_krw + self.tax_krw + self.fx_spread_krw:
            raise ValueError(
                "total_cost_krw must equal fee_krw + tax_krw + fx_spread_krw."
            )
        if self.market in FX_MARKETS:
            if self.usdkrw_rate is None or self.usdkrw_rate <= ZERO:
                raise ValueError("usdkrw_rate must be present and > 0 for US/GOLD trades.")
        elif self.market == "KR":
            if self.usdkrw_rate is not None:
                raise ValueError("usdkrw_rate must be None for KR trades.")
        return self


class BacktestRebalanceResult(BaseModel):
    """Immutable result of one target-weight rebalance accounting step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_time: datetime
    intended_execution_time: datetime
    accounting_policy: Literal["single_rebalance_target_weight_accounting.v1"]
    cost_model_version: Literal["simple_proportional_fee_sell_tax_fx_spread.v1"]
    pre_trade_portfolio_value_krw: Decimal
    post_trade_portfolio_value_krw: Decimal
    cash_krw_before: Decimal
    cash_krw_after: Decimal
    trades: tuple[BacktestTrade, ...]
    post_trade_holdings: tuple[BacktestHolding, ...]
    total_fee_krw: Decimal
    total_tax_krw: Decimal
    total_fx_spread_krw: Decimal
    total_cost_krw: Decimal

    @field_validator("decision_time", "intended_execution_time", mode="before")
    @classmethod
    def validate_timestamps(cls, value: Any, info) -> datetime:
        return require_timezone_aware_datetime(value, field_name=info.field_name)

    @field_validator(
        "pre_trade_portfolio_value_krw",
        "post_trade_portfolio_value_krw",
        "cash_krw_before",
        "cash_krw_after",
        "total_fee_krw",
        "total_tax_krw",
        "total_fx_spread_krw",
        "total_cost_krw",
        mode="before",
    )
    @classmethod
    def validate_non_negative_decimals(cls, value: Any, info) -> Decimal:
        parsed = _to_decimal_no_float(value, field_name=info.field_name)
        if parsed < ZERO:
            raise ValueError(f"{info.field_name} must be >= 0.")
        return parsed

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.decision_time >= self.intended_execution_time:
            raise ValueError("decision_time must be before intended_execution_time.")

        holding_ids = tuple(holding.asset_id for holding in self.post_trade_holdings)
        if len(holding_ids) != len(set(holding_ids)):
            raise ValueError("post_trade_holdings must have unique asset ids.")

        trade_fee = sum((trade.fee_krw for trade in self.trades), ZERO)
        trade_tax = sum((trade.tax_krw for trade in self.trades), ZERO)
        trade_fx = sum((trade.fx_spread_krw for trade in self.trades), ZERO)
        trade_total = sum((trade.total_cost_krw for trade in self.trades), ZERO)

        if self.total_fee_krw != trade_fee:
            raise ValueError("total_fee_krw must equal the sum of trade fee_krw values.")
        if self.total_tax_krw != trade_tax:
            raise ValueError("total_tax_krw must equal the sum of trade tax_krw values.")
        if self.total_fx_spread_krw != trade_fx:
            raise ValueError(
                "total_fx_spread_krw must equal the sum of trade fx_spread_krw values."
            )
        if self.total_cost_krw != trade_total:
            raise ValueError(
                "total_cost_krw must equal the sum of trade total_cost_krw values."
            )
        if self.total_cost_krw != (
            self.total_fee_krw + self.total_tax_krw + self.total_fx_spread_krw
        ):
            raise ValueError(
                "total_cost_krw must equal total_fee_krw + total_tax_krw + "
                "total_fx_spread_krw."
            )
        return self


def apply_single_rebalance_accounting(
    *,
    decision: BacktestSingleStepDecision,
    execution_prices: BacktestExecutionPriceSlice,
    portfolio_state: BacktestPortfolioState,
    cost_model: BacktestCostModel,
    usdkrw_rate: Decimal,
) -> BacktestRebalanceResult:
    """Apply one target-weight rebalance to one portfolio state.

    Computes trades and costs from ``decision.target_weights`` and
    ``execution_prices.prices``. Does not iterate decision dates, produce NAV,
    or compute benchmark-relative metrics.
    """

    parsed_usdkrw_rate = _to_decimal_no_float(usdkrw_rate, field_name="usdkrw_rate")
    if parsed_usdkrw_rate <= ZERO:
        raise ValueError("usdkrw_rate must be greater than 0.")

    if decision.decision_time != execution_prices.decision_time:
        raise ValueError("decision.decision_time must equal execution_prices.decision_time.")
    if decision.intended_execution_time != execution_prices.intended_execution_time:
        raise ValueError(
            "decision.intended_execution_time must equal "
            "execution_prices.intended_execution_time."
        )

    cash_asset_id = decision.feature_snapshot.cash_asset_id
    non_cash_target_ids = _non_cash_target_asset_ids(decision, cash_asset_id=cash_asset_id)
    price_asset_ids = tuple(price.asset_id for price in execution_prices.prices)
    if price_asset_ids != non_cash_target_ids:
        raise ValueError(
            "execution_prices asset ids must match non-cash target asset ids."
        )

    weight_by_id = {
        weight.asset_id: weight.weight for weight in decision.target_weights.weights
    }
    holdings_by_id = {
        holding.asset_id: holding.quantity for holding in portfolio_state.holdings
    }

    pre_trade_portfolio_value_krw = portfolio_state.cash_krw
    for price_record in execution_prices.prices:
        current_quantity = holdings_by_id.get(price_record.asset_id, ZERO)
        pre_trade_portfolio_value_krw += _holding_value_krw(
            current_quantity,
            price_record.execution_price,
            market=price_record.market,
            usdkrw_rate=parsed_usdkrw_rate,
        )

    trades: list[BacktestTrade] = []
    cash_krw_after = portfolio_state.cash_krw

    for price_record in execution_prices.prices:
        target_notional_krw = weight_by_id[price_record.asset_id] * pre_trade_portfolio_value_krw
        current_quantity = holdings_by_id.get(price_record.asset_id, ZERO)
        current_notional_krw = _holding_value_krw(
            current_quantity,
            price_record.execution_price,
            market=price_record.market,
            usdkrw_rate=parsed_usdkrw_rate,
        )
        diff = target_notional_krw - current_notional_krw
        if diff == ZERO:
            continue

        side: Literal["BUY", "SELL"] = "BUY" if diff > ZERO else "SELL"
        gross_notional_krw = abs(diff)
        quantity = _quantity_from_notional_krw(
            gross_notional_krw,
            execution_price=price_record.execution_price,
            market=price_record.market,
            usdkrw_rate=parsed_usdkrw_rate,
        )

        if side == "SELL" and quantity > current_quantity:
            raise ValueError(
                f"cannot sell more than current holding for asset_id={price_record.asset_id!r}."
            )

        fee_krw = gross_notional_krw * cost_model.fee_bps / BPS_DIVISOR
        tax_krw = ZERO
        if side == "SELL" and price_record.market == "KR":
            tax_krw = gross_notional_krw * cost_model.kr_sell_tax_bps / BPS_DIVISOR
        fx_spread_krw = ZERO
        if price_record.market in FX_MARKETS:
            fx_spread_krw = gross_notional_krw * cost_model.fx_spread_bps / BPS_DIVISOR
        total_cost_krw = fee_krw + tax_krw + fx_spread_krw

        trade_usdkrw_rate = (
            parsed_usdkrw_rate if price_record.market in FX_MARKETS else None
        )
        trades.append(
            BacktestTrade(
                asset_id=price_record.asset_id,
                symbol=price_record.symbol,
                market=price_record.market,
                side=side,
                quantity=quantity,
                execution_price=price_record.execution_price,
                usdkrw_rate=trade_usdkrw_rate,
                gross_notional_krw=gross_notional_krw,
                fee_krw=fee_krw,
                tax_krw=tax_krw,
                fx_spread_krw=fx_spread_krw,
                total_cost_krw=total_cost_krw,
            )
        )

        if side == "BUY":
            cash_krw_after -= gross_notional_krw + total_cost_krw
            holdings_by_id[price_record.asset_id] = current_quantity + quantity
        else:
            cash_krw_after += gross_notional_krw - total_cost_krw
            holdings_by_id[price_record.asset_id] = current_quantity - quantity

    if cash_krw_after < ZERO:
        raise ValueError("cash would become negative after rebalance trades and costs.")

    post_trade_holdings = _build_post_trade_holdings(
        decision,
        holdings_by_id=holdings_by_id,
        cash_asset_id=cash_asset_id,
    )

    post_trade_portfolio_value_krw = cash_krw_after
    for price_record in execution_prices.prices:
        quantity = holdings_by_id.get(price_record.asset_id, ZERO)
        post_trade_portfolio_value_krw += _holding_value_krw(
            quantity,
            price_record.execution_price,
            market=price_record.market,
            usdkrw_rate=parsed_usdkrw_rate,
        )

    total_fee_krw = sum((trade.fee_krw for trade in trades), ZERO)
    total_tax_krw = sum((trade.tax_krw for trade in trades), ZERO)
    total_fx_spread_krw = sum((trade.fx_spread_krw for trade in trades), ZERO)
    total_cost_krw = sum((trade.total_cost_krw for trade in trades), ZERO)

    return BacktestRebalanceResult(
        decision_time=decision.decision_time,
        intended_execution_time=decision.intended_execution_time,
        accounting_policy=REBALANCE_ACCOUNTING_POLICY_V1,
        cost_model_version=cost_model.cost_model_version,
        pre_trade_portfolio_value_krw=pre_trade_portfolio_value_krw,
        post_trade_portfolio_value_krw=post_trade_portfolio_value_krw,
        cash_krw_before=portfolio_state.cash_krw,
        cash_krw_after=cash_krw_after,
        trades=tuple(trades),
        post_trade_holdings=post_trade_holdings,
        total_fee_krw=total_fee_krw,
        total_tax_krw=total_tax_krw,
        total_fx_spread_krw=total_fx_spread_krw,
        total_cost_krw=total_cost_krw,
    )


def _non_cash_target_asset_ids(
    decision: BacktestSingleStepDecision,
    *,
    cash_asset_id: str,
) -> tuple[str, ...]:
    return tuple(
        weight.asset_id
        for weight in decision.target_weights.weights
        if weight.asset_id != cash_asset_id
    )


def _build_post_trade_holdings(
    decision: BacktestSingleStepDecision,
    *,
    holdings_by_id: dict[str, Decimal],
    cash_asset_id: str,
) -> tuple[BacktestHolding, ...]:
    post_trade_holdings: list[BacktestHolding] = []
    for config in decision.snapshot_asset_configs:
        if config.asset_id == cash_asset_id:
            continue
        quantity = holdings_by_id.get(config.asset_id, ZERO)
        if quantity > ZERO:
            post_trade_holdings.append(
                BacktestHolding(asset_id=config.asset_id, quantity=quantity)
            )
    return tuple(post_trade_holdings)


def _holding_value_krw(
    quantity: Decimal,
    execution_price: Decimal,
    *,
    market: str,
    usdkrw_rate: Decimal,
) -> Decimal:
    if quantity == ZERO:
        return ZERO
    if market == "KR":
        return quantity * execution_price
    if market in FX_MARKETS:
        return quantity * execution_price * usdkrw_rate
    raise ValueError(f"unsupported market for valuation: {market!r}.")


def _quantity_from_notional_krw(
    gross_notional_krw: Decimal,
    *,
    execution_price: Decimal,
    market: str,
    usdkrw_rate: Decimal,
) -> Decimal:
    if market == "KR":
        return gross_notional_krw / execution_price
    if market in FX_MARKETS:
        return gross_notional_krw / (execution_price * usdkrw_rate)
    raise ValueError(f"unsupported market for quantity conversion: {market!r}.")


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
