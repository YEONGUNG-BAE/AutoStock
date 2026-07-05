"""Execution price selection contract for Phase 2c-4.

This module selects executable prices for one already-built
``BacktestSingleStepDecision``. For each non-cash asset it chooses the first
valid price record whose ``source_timestamp`` is at or after
``decision.intended_execution_time``. Because Phase 2c-3 already enforces
``decision_time < intended_execution_time``, this selector never uses a
same-decision-time price as an execution price.

This module looks forward from the intended execution timestamp, so it does not
use the Phase 2a as-of filtered source view (that as-of filter belongs to the
signal side). It chooses prices only. It does not execute trades, produce
fills, compute target quantities, model transaction costs/slippage/FX/tax,
maintain holdings or a cash ledger, produce NAV, compute benchmark-relative
metrics, iterate over multiple decision dates, fetch data, or use real data.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_data.source_records import (
    BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    InMemoryDateIdSourceReader,
)
from backtest_engine.single_step import BacktestSingleStepDecision
from backtest_engine.snapshot_builder import SnapshotAssetConfig
from domain._datetime import require_timezone_aware_datetime
from domain.source import DateIdSourceRecord, FactType

EXECUTION_PRICE_POLICY_V1 = "first_visible_price_at_or_after_intended_execution_time.v1"

ZERO = Decimal("0")


class BacktestExecutionPrice(BaseModel):
    """One selected future execution price for a single non-cash asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    symbol: str
    market: str
    source_date: date
    source_timestamp: datetime
    execution_price: Decimal
    source_name: str
    date_id: str

    @field_validator(
        "asset_id",
        "symbol",
        "market",
        "source_name",
        "date_id",
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

    @field_validator("source_date", mode="before")
    @classmethod
    def validate_source_date(cls, value: Any) -> date:
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ValueError("source_date must be a date.")
        return value

    @field_validator("source_timestamp", mode="before")
    @classmethod
    def validate_source_timestamp(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="source_timestamp")

    @field_validator("execution_price", mode="before")
    @classmethod
    def validate_execution_price(cls, value: Any) -> Decimal:
        parsed = _to_decimal_no_float(value, field_name="execution_price")
        if parsed <= ZERO:
            raise ValueError("execution_price must be greater than 0.")
        return parsed


class BacktestExecutionPriceSlice(BaseModel):
    """Immutable slice of selected execution prices for one decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_time: datetime
    intended_execution_time: datetime
    execution_policy: Literal[
        "first_visible_price_at_or_after_intended_execution_time.v1"
    ]
    prices: tuple[BacktestExecutionPrice, ...]

    @field_validator("decision_time", "intended_execution_time", mode="before")
    @classmethod
    def validate_timestamps(cls, value: Any, info) -> datetime:
        return require_timezone_aware_datetime(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_slice(self) -> Self:
        if self.decision_time >= self.intended_execution_time:
            raise ValueError("decision_time must be before intended_execution_time.")
        if not self.prices:
            raise ValueError("prices must not be empty.")

        asset_ids = tuple(price.asset_id for price in self.prices)
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("prices must have unique asset ids.")

        for price in self.prices:
            if price.source_timestamp < self.intended_execution_time:
                raise ValueError(
                    "every price.source_timestamp must be >= intended_execution_time."
                )
        return self


def select_execution_prices_for_single_step_decision(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    decision: BacktestSingleStepDecision,
) -> BacktestExecutionPriceSlice:
    """Select one future execution price per non-cash asset in ``decision``.

    The selector looks forward from ``decision.intended_execution_time`` and
    never uses records at or before ``decision.decision_time``. It chooses
    prices only; it does not compute trades, quantities, fills, costs,
    holdings, a cash ledger, NAV, or benchmark-relative metrics.
    """

    reader = (
        source
        if isinstance(source, InMemoryDateIdSourceReader)
        else InMemoryDateIdSourceReader(source)
    )
    price_records = reader.list_records(fact_type=FactType.PRICE)

    intended_execution_time = decision.intended_execution_time
    cash_asset_id = decision.feature_snapshot.cash_asset_id

    selected_prices: list[BacktestExecutionPrice] = []
    for config in decision.snapshot_asset_configs:
        if config.asset_id == cash_asset_id:
            continue
        selected_prices.append(
            _select_price_for_config(
                config,
                price_records=price_records,
                intended_execution_time=intended_execution_time,
            )
        )

    return BacktestExecutionPriceSlice(
        decision_time=decision.decision_time,
        intended_execution_time=intended_execution_time,
        execution_policy=EXECUTION_PRICE_POLICY_V1,
        prices=tuple(selected_prices),
    )


def _select_price_for_config(
    config: SnapshotAssetConfig,
    *,
    price_records: tuple[DateIdSourceRecord, ...],
    intended_execution_time: datetime,
) -> BacktestExecutionPrice:
    eligible: list[tuple[DateIdSourceRecord, datetime, date, Decimal]] = []
    for record in price_records:
        if record.symbol != config.symbol or record.market != config.market:
            continue
        source_timestamp = require_timezone_aware_datetime(
            record.source_timestamp, field_name="source_timestamp"
        )
        if source_timestamp < intended_execution_time:
            continue
        source_date, execution_price = _parse_price_payload(record, config=config)
        eligible.append((record, source_timestamp, source_date, execution_price))

    if not eligible:
        raise ValueError(
            "no future executable price at or after intended execution time for asset "
            f"asset_id={config.asset_id!r} symbol={config.symbol!r} "
            f"market={config.market!r}."
        )

    earliest_timestamp = min(source_timestamp for _, source_timestamp, _, _ in eligible)
    tied = [item for item in eligible if item[1] == earliest_timestamp]
    record, source_timestamp, source_date, execution_price = max(
        tied, key=lambda item: (item[0].date_id.value, item[0].source_name)
    )

    return BacktestExecutionPrice(
        asset_id=config.asset_id,
        symbol=config.symbol,
        market=config.market,
        source_date=source_date,
        source_timestamp=source_timestamp,
        execution_price=execution_price,
        source_name=record.source_name,
        date_id=record.date_id.value,
    )


def _parse_price_payload(
    record: DateIdSourceRecord,
    *,
    config: SnapshotAssetConfig,
) -> tuple[date, Decimal]:
    payload = record.payload
    if payload.get("schema_name") != BACKTEST_INSTRUMENT_PRICE_SCHEMA:
        raise ValueError(
            "price record payload schema_name must be backtest.instrument_price.v1."
        )

    payload_symbol = _required_payload_string(payload, "symbol")
    payload_market = _required_payload_string(payload, "market")
    if payload_symbol != config.symbol:
        raise ValueError("price record payload symbol must match configured symbol.")
    if payload_market != config.market:
        raise ValueError("price record payload market must match configured market.")
    if record.symbol is not None and record.symbol != payload_symbol:
        raise ValueError("price record symbol must match payload symbol.")
    if record.market is not None and record.market != payload_market:
        raise ValueError("price record market must match payload market.")

    source_date = _parse_iso_date(payload)

    if "close_adjusted" not in payload:
        raise ValueError("price record payload must include close_adjusted.")
    execution_price = _to_decimal_no_float(
        payload["close_adjusted"], field_name="close_adjusted"
    )
    if execution_price <= ZERO:
        raise ValueError("close_adjusted must be greater than 0.")

    return source_date, execution_price


def _parse_iso_date(payload: dict[str, Any]) -> date:
    raw = payload.get("date")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("price record payload date must be a non-empty string.")
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError("price record payload date must be an ISO parseable date.") from exc


def _required_payload_string(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise ValueError(f"price record payload {field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"price record payload {field_name} must not be blank.")
    return normalized


def _to_decimal_no_float(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} must be a Decimal string; floats are not accepted.")
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
