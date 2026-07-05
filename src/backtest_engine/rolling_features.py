"""As-of-safe rolling feature building blocks for one decision time.

This module computes one simple observation-count moving average per configured
asset from visible price source records. It uses AsOfFilteredSourceView, reads
only FactType.PRICE, and returns SnapshotAssetConfig objects that can be passed
to the Phase 2c-1 snapshot builder.

The moving average is based only on the latest lookback_count visible
observations ordered by (source_timestamp, date_id.value, source_name). No
forward-fill, interpolation, full-sample fitting, normalization, date loop,
decision artifact, execution artifact, or external data access is implemented.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_data.asof_guard import AsOfFilteredSourceView
from backtest_data.source_records import (
    BACKTEST_INSTRUMENT_PRICE_SCHEMA,
    InMemoryDateIdSourceReader,
)
from backtest_engine.snapshot_builder import SnapshotAssetConfig
from domain.source import DateIdSourceRecord, FactType

ONE = Decimal("1")
ZERO = Decimal("0")


class RollingLongMaAssetConfig(BaseModel):
    """Per-asset inputs for computing one rolling long_ma value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    symbol: str
    market: str
    lookback_count: int
    risk_on_weight: Decimal
    risk_off_weight: Decimal
    min_weight: Decimal
    max_weight: Decimal

    @field_validator("asset_id", "symbol", "market", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{info.field_name} must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be blank.")
        return normalized

    @field_validator("lookback_count", mode="before")
    @classmethod
    def validate_lookback_count(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("lookback_count must be an integer.")
        if value < 2:
            raise ValueError("lookback_count must be >= 2.")
        return value

    @field_validator(
        "risk_on_weight",
        "risk_off_weight",
        "min_weight",
        "max_weight",
        mode="before",
    )
    @classmethod
    def validate_decimal_fields(cls, value: Any, info) -> Decimal:
        return _to_decimal_no_float(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_weights(self) -> Self:
        _validate_unit_interval(self.risk_on_weight, field_name="risk_on_weight")
        _validate_unit_interval(self.risk_off_weight, field_name="risk_off_weight")
        _validate_unit_interval(self.min_weight, field_name="min_weight")
        _validate_unit_interval(self.max_weight, field_name="max_weight")
        if self.min_weight > self.max_weight:
            raise ValueError("min_weight must be <= max_weight.")
        return self


def build_snapshot_configs_with_rolling_long_ma(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    decision_time: datetime,
    asset_configs: Iterable[RollingLongMaAssetConfig],
) -> tuple[SnapshotAssetConfig, ...]:
    """Compute rolling long_ma values and return SnapshotAssetConfig objects."""

    guarded_source = AsOfFilteredSourceView(source, decision_time=decision_time)
    visible_price_records = guarded_source.list_records(fact_type=FactType.PRICE)
    configs = tuple(asset_configs)
    if not configs:
        raise ValueError("asset_configs must contain at least one asset config.")

    return tuple(
        _build_snapshot_config(config, visible_price_records=visible_price_records)
        for config in configs
    )


def _build_snapshot_config(
    config: RollingLongMaAssetConfig,
    *,
    visible_price_records: tuple[DateIdSourceRecord, ...],
) -> SnapshotAssetConfig:
    matching_observations = tuple(
        observation
        for observation in (
            _validated_price_observation(
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
            f"asset_id={config.asset_id!r}: required {config.lookback_count}, "
            f"found {len(matching_observations)}."
        )

    selected = tuple(sorted(matching_observations, key=lambda item: item[0])[-config.lookback_count :])
    long_ma = sum((close_adjusted for _, close_adjusted in selected), ZERO) / Decimal(
        config.lookback_count
    )
    return SnapshotAssetConfig(
        asset_id=config.asset_id,
        symbol=config.symbol,
        market=config.market,
        long_ma=long_ma,
        risk_on_weight=config.risk_on_weight,
        risk_off_weight=config.risk_off_weight,
        min_weight=config.min_weight,
        max_weight=config.max_weight,
    )


def _validated_price_observation(
    record: DateIdSourceRecord,
    *,
    expected_symbol: str,
    expected_market: str,
) -> tuple[tuple[datetime, str, str], Decimal] | None:
    payload = record.payload
    if payload.get("schema_name") != BACKTEST_INSTRUMENT_PRICE_SCHEMA:
        raise ValueError("price record payload schema_name must be backtest.instrument_price.v1.")
    if "close_adjusted" not in payload:
        raise ValueError("price record payload must include close_adjusted.")

    payload_symbol = _required_payload_string(payload, "symbol")
    payload_market = _required_payload_string(payload, "market")
    if record.symbol is not None and record.symbol != payload_symbol:
        raise ValueError("price record symbol must match payload symbol.")
    if record.market is not None and record.market != payload_market:
        raise ValueError("price record market must match payload market.")

    close_adjusted = _to_decimal_no_float(payload["close_adjusted"], field_name="close_adjusted")
    if close_adjusted <= ZERO:
        raise ValueError("close_adjusted must be greater than 0.")

    if payload_symbol != expected_symbol or payload_market != expected_market:
        return None

    return ((record.source_timestamp, record.date_id.value, record.source_name), close_adjusted)


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
        raise ValueError(f"{field_name} must be finite.")
    return parsed


def _validate_unit_interval(value: Decimal, *, field_name: str) -> None:
    if value < ZERO or value > ONE:
        raise ValueError(f"{field_name} must be between 0 and 1 inclusive.")
