"""As-of-safe single-decision snapshot builder for Phase 2c-1.

This module builds one BacktestFeatureSnapshot from already-converted
DateIdSourceRecord inputs or a read-only in-memory source reader. It uses the
Phase 2a AsOfFilteredSourceView so only records with
source_timestamp <= decision_time are visible.

This phase does not compute rolling features or long MA. long_ma is supplied by
SnapshotAssetConfig because rolling feature calculation belongs to a later
phase; computing it here risks accidental full-sample leakage. This module also
does not implement a walk-forward loop, execution, NAV, benchmark scoring,
network fetching, LLM calls, masking, or runtime store writes.

If multiple visible price records match one configured asset, selection is
deterministic: choose the maximum (source_timestamp, date_id.value,
source_name). Missing visible data raises ValueError; no forward-fill policy is
implemented in this phase.
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
from backtest_engine.step_contract import BacktestAssetFeature, BacktestFeatureSnapshot
from domain.source import DateIdSourceRecord, FactType

ONE = Decimal("1")
ZERO = Decimal("0")


class SnapshotAssetConfig(BaseModel):
    """Static per-asset snapshot configuration for one decision step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    symbol: str
    market: str
    long_ma: Decimal
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

    @field_validator(
        "long_ma",
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
    def validate_values(self) -> Self:
        if self.long_ma <= ZERO:
            raise ValueError("long_ma must be greater than 0.")
        _validate_unit_interval(self.risk_on_weight, field_name="risk_on_weight")
        _validate_unit_interval(self.risk_off_weight, field_name="risk_off_weight")
        _validate_unit_interval(self.min_weight, field_name="min_weight")
        _validate_unit_interval(self.max_weight, field_name="max_weight")
        if self.min_weight > self.max_weight:
            raise ValueError("min_weight must be <= max_weight.")
        return self


def build_feature_snapshot_from_source_records(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    decision_time: datetime,
    asset_configs: Iterable[SnapshotAssetConfig],
    cash_asset_id: str,
    cash_min_weight: Decimal,
) -> BacktestFeatureSnapshot:
    """Build one as-of-safe feature snapshot for one decision time."""

    guarded_source = AsOfFilteredSourceView(source, decision_time=decision_time)
    visible_price_records = guarded_source.list_records(fact_type=FactType.PRICE)
    configs = tuple(asset_configs)
    if not configs:
        raise ValueError("asset_configs must contain at least one asset config.")

    features = tuple(
        _feature_from_config(config, visible_price_records=visible_price_records)
        for config in configs
    )

    return BacktestFeatureSnapshot(
        decision_time=decision_time,
        assets=features,
        cash_asset_id=cash_asset_id,
        cash_min_weight=cash_min_weight,
    )


def _feature_from_config(
    config: SnapshotAssetConfig,
    *,
    visible_price_records: tuple[DateIdSourceRecord, ...],
) -> BacktestAssetFeature:
    matching_records = tuple(
        record
        for record in visible_price_records
        if record.symbol == config.symbol and record.market == config.market
    )
    if not matching_records:
        raise ValueError(
            "no visible price record for configured asset "
            f"asset_id={config.asset_id!r} symbol={config.symbol!r} market={config.market!r}."
        )

    selected = max(matching_records, key=_record_selection_key)
    current_price = _extract_close_adjusted(
        selected,
        expected_symbol=config.symbol,
        expected_market=config.market,
    )
    return BacktestAssetFeature(
        asset_id=config.asset_id,
        as_of=selected.source_timestamp,
        current_price=current_price,
        long_ma=config.long_ma,
        risk_on_weight=config.risk_on_weight,
        risk_off_weight=config.risk_off_weight,
        min_weight=config.min_weight,
        max_weight=config.max_weight,
    )


def _record_selection_key(record: DateIdSourceRecord) -> tuple[datetime, str, str]:
    return (record.source_timestamp, record.date_id.value, record.source_name)


def _extract_close_adjusted(
    record: DateIdSourceRecord,
    *,
    expected_symbol: str,
    expected_market: str,
) -> Decimal:
    payload = record.payload
    if payload.get("schema_name") != BACKTEST_INSTRUMENT_PRICE_SCHEMA:
        raise ValueError("price record payload schema_name must be backtest.instrument_price.v1.")
    if "close_adjusted" not in payload:
        raise ValueError("price record payload must include close_adjusted.")

    _validate_payload_identity(
        record,
        payload=payload,
        expected_symbol=expected_symbol,
        expected_market=expected_market,
    )

    close_adjusted = _to_decimal_no_float(
        payload["close_adjusted"],
        field_name="close_adjusted",
    )
    if close_adjusted <= ZERO:
        raise ValueError("close_adjusted must be greater than 0.")
    return close_adjusted


def _validate_payload_identity(
    record: DateIdSourceRecord,
    *,
    payload: dict[str, Any],
    expected_symbol: str,
    expected_market: str,
) -> None:
    payload_symbol = _required_payload_string(payload, "symbol")
    payload_market = _required_payload_string(payload, "market")

    if payload_symbol != expected_symbol:
        raise ValueError("price record payload symbol must match configured symbol.")
    if payload_market != expected_market:
        raise ValueError("price record payload market must match configured market.")
    if record.symbol is not None and record.symbol != payload_symbol:
        raise ValueError("price record symbol must match payload symbol.")
    if record.market is not None and record.market != payload_market:
        raise ValueError("price record market must match payload market.")


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
