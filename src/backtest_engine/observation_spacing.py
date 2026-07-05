"""Observation spacing guard for count-based rolling features.

Count-based moving averages only have the intended monthly meaning when the
selected observations map to distinct consecutive monthly periods. This module
validates that precondition for one decision time. It does not compute moving
averages, implement a time-window average, compose decisions, execute trades,
or access external data.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_data.asof_guard import AsOfFilteredSourceView
from backtest_data.source_records import (
    BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    InMemoryDateIdSourceReader,
)
from backtest_engine.rolling_features import RollingLongMaAssetConfig
from domain.source import DateIdSourceRecord, FactType


class ObservationSpacingReport(BaseModel):
    """Report for one asset's selected count-based monthly observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    symbol: str
    market: str
    frequency: str
    lookback_count: int
    period_keys: tuple[str, ...]

    @field_validator("asset_id", "symbol", "market", "frequency", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{info.field_name} must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be blank.")
        return normalized

    @field_validator("period_keys", mode="before")
    @classmethod
    def validate_period_keys(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, list):
            return tuple(value)
        if isinstance(value, tuple):
            return value
        raise ValueError("period_keys must be a list or tuple.")

    @model_validator(mode="after")
    def validate_report(self) -> "ObservationSpacingReport":
        if self.frequency != "monthly":
            raise ValueError("frequency must be monthly.")
        if self.lookback_count < 2:
            raise ValueError("lookback_count must be >= 2.")
        if len(self.period_keys) != self.lookback_count:
            raise ValueError("period_keys length must equal lookback_count.")
        for period_key in self.period_keys:
            _parse_monthly_period_key(period_key)
        return self


def validate_uniform_observation_spacing_for_count_based_ma(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    decision_time: datetime,
    asset_configs: Iterable[RollingLongMaAssetConfig],
    frequency: Literal["monthly"] = "monthly",
) -> tuple[ObservationSpacingReport, ...]:
    """Validate spacing for one decision time before count-based rolling MA."""

    if frequency != "monthly":
        raise ValueError("unsupported observation spacing frequency: expected monthly.")

    guarded_source = AsOfFilteredSourceView(source, decision_time=decision_time)
    visible_price_records = guarded_source.list_records(fact_type=FactType.PRICE)
    configs = tuple(asset_configs)
    if not configs:
        raise ValueError("asset_configs must contain at least one asset config.")

    return tuple(
        _build_spacing_report(config, visible_price_records=visible_price_records)
        for config in configs
    )


def _build_spacing_report(
    config: RollingLongMaAssetConfig,
    *,
    visible_price_records: tuple[DateIdSourceRecord, ...],
) -> ObservationSpacingReport:
    matching_observations = tuple(
        observation
        for observation in (
            _validated_period_observation(
                record,
                expected_symbol=config.symbol,
                expected_market=config.market,
            )
            for record in visible_price_records
        )
        if observation is not None
    )
    if not matching_observations:
        raise ValueError(
            "no visible price records for configured asset "
            f"asset_id={config.asset_id!r} symbol={config.symbol!r} market={config.market!r}."
        )
    if len(matching_observations) < config.lookback_count:
        raise ValueError(
            "insufficient visible price observations for configured asset "
            f"asset_id={config.asset_id!r} symbol={config.symbol!r} market={config.market!r}: "
            f"required {config.lookback_count}, found {len(matching_observations)}."
        )

    selected = tuple(sorted(matching_observations, key=lambda item: item[0])[-config.lookback_count :])
    period_keys = tuple(period_key for _, period_key in selected)
    _validate_distinct_consecutive_months(config, period_keys=period_keys)
    return ObservationSpacingReport(
        asset_id=config.asset_id,
        symbol=config.symbol,
        market=config.market,
        frequency="monthly",
        lookback_count=config.lookback_count,
        period_keys=period_keys,
    )


def _validated_period_observation(
    record: DateIdSourceRecord,
    *,
    expected_symbol: str,
    expected_market: str,
) -> tuple[tuple[datetime, str, str], str] | None:
    payload = record.payload
    if payload.get("schema_name") != BACKTEST_INSTRUMENT_PRICE_SCHEMA:
        raise ValueError("price record payload schema_name must be backtest.instrument_price.v1.")

    payload_symbol = _required_payload_string(payload, "symbol")
    payload_market = _required_payload_string(payload, "market")
    payload_date = _required_payload_date(payload)

    if record.symbol is not None and record.symbol != payload_symbol:
        raise ValueError("price record symbol must match payload symbol.")
    if record.market is not None and record.market != payload_market:
        raise ValueError("price record market must match payload market.")
    if payload_symbol != expected_symbol or payload_market != expected_market:
        return None

    period_key = f"{payload_date.year:04d}-{payload_date.month:02d}"
    return ((record.source_timestamp, record.date_id.value, record.source_name), period_key)


def _required_payload_string(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise ValueError(f"price record payload {field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"price record payload {field_name} must not be blank.")
    return normalized


def _required_payload_date(payload: dict[str, Any]) -> date:
    value = payload.get("date")
    if not isinstance(value, str):
        raise ValueError("price record payload date must be a string.")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("price record payload date must be a valid ISO date string.") from exc


def _validate_distinct_consecutive_months(
    config: RollingLongMaAssetConfig,
    *,
    period_keys: tuple[str, ...],
) -> None:
    if len(set(period_keys)) != len(period_keys):
        raise ValueError(
            "duplicate period for configured asset "
            f"asset_id={config.asset_id!r} symbol={config.symbol!r} market={config.market!r}."
        )

    ordinals = sorted(_monthly_period_ordinal(period_key) for period_key in period_keys)
    expected = tuple(range(ordinals[0], ordinals[0] + len(ordinals)))
    if tuple(ordinals) != expected:
        raise ValueError(
            "skipped period for configured asset "
            f"asset_id={config.asset_id!r} symbol={config.symbol!r} market={config.market!r}."
        )


def _monthly_period_ordinal(period_key: str) -> int:
    year, month = _parse_monthly_period_key(period_key)
    return year * 12 + month


def _parse_monthly_period_key(period_key: str) -> tuple[int, int]:
    parts = period_key.split("-")
    if len(parts) != 2:
        raise ValueError("monthly period key must use YYYY-MM format.")
    year_text, month_text = parts
    if len(year_text) != 4 or len(month_text) != 2:
        raise ValueError("monthly period key must use YYYY-MM format.")
    try:
        year = int(year_text)
        month = int(month_text)
    except ValueError as exc:
        raise ValueError("monthly period key must use YYYY-MM format.") from exc
    if month < 1 or month > 12:
        raise ValueError("monthly period key month must be between 01 and 12.")
    return year, month
