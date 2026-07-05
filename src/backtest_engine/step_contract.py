"""Frozen single-step backtest input/output models for Phase 2c-0.

BacktestFeatureSnapshot contains already-built as-of-safe features. Building
that snapshot from source records is Phase 2c-1.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

DECIMAL_WEIGHT_TOLERANCE = Decimal("0.00000001")
RULES_ALLOCATOR_V1 = "rules_allocator.v1"
ONE = Decimal("1")
ZERO = Decimal("0")


def _normalize_required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string.")

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank.")
    return normalized


def _parse_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        value = datetime.fromisoformat(normalized)

    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime.")
    return value


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
        raise ValueError(f"{field_name} must be a Decimal value.")

    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal value.")
    return parsed


def _validate_unit_interval(value: Decimal, *, field_name: str) -> Decimal:
    if value < ZERO or value > ONE:
        raise ValueError(f"{field_name} must be between 0 and 1 inclusive.")
    return value


class BacktestAssetFeature(BaseModel):
    """Feature row for one generic non-cash asset at one decision step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    as_of: datetime
    current_price: Decimal
    long_ma: Decimal
    risk_on_weight: Decimal
    risk_off_weight: Decimal
    min_weight: Decimal
    max_weight: Decimal

    @field_validator("asset_id", mode="before")
    @classmethod
    def validate_asset_id(cls, value: Any) -> str:
        return _normalize_required_string(value, field_name="asset_id")

    @field_validator("as_of", mode="before")
    @classmethod
    def validate_as_of(cls, value: Any) -> datetime:
        return _parse_timezone_aware_datetime(value, field_name="as_of")

    @field_validator(
        "current_price",
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
        if self.current_price <= ZERO:
            raise ValueError("current_price must be greater than 0.")
        if self.long_ma <= ZERO:
            raise ValueError("long_ma must be greater than 0.")
        _validate_unit_interval(self.risk_on_weight, field_name="risk_on_weight")
        _validate_unit_interval(self.risk_off_weight, field_name="risk_off_weight")
        _validate_unit_interval(self.min_weight, field_name="min_weight")
        _validate_unit_interval(self.max_weight, field_name="max_weight")
        if self.min_weight > self.max_weight:
            raise ValueError("min_weight must be <= max_weight.")
        return self


class BacktestFeatureSnapshot(BaseModel):
    """Already-built as-of-safe features for a single allocator decision step.

    BacktestFeatureSnapshot contains already-built as-of-safe features. Building
    that snapshot from source records is Phase 2c-1.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_time: datetime
    assets: tuple[BacktestAssetFeature, ...]
    cash_asset_id: str
    cash_min_weight: Decimal

    @field_validator("decision_time", mode="before")
    @classmethod
    def validate_decision_time(cls, value: Any) -> datetime:
        return _parse_timezone_aware_datetime(value, field_name="decision_time")

    @field_validator("cash_asset_id", mode="before")
    @classmethod
    def validate_cash_asset_id(cls, value: Any) -> str:
        return _normalize_required_string(value, field_name="cash_asset_id")

    @field_validator("cash_min_weight", mode="before")
    @classmethod
    def validate_cash_min_weight(cls, value: Any) -> Decimal:
        return _to_decimal_no_float(value, field_name="cash_min_weight")

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if not self.assets:
            raise ValueError("assets must contain at least one non-cash asset.")
        _validate_unit_interval(self.cash_min_weight, field_name="cash_min_weight")

        asset_ids = tuple(asset.asset_id for asset in self.assets)
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("asset_id values must be unique.")
        if self.cash_asset_id in asset_ids:
            raise ValueError("cash_asset_id must not equal any asset_id.")

        for asset in self.assets:
            if asset.as_of > self.decision_time:
                raise ValueError("asset.as_of must be <= decision_time.")

        min_total = sum((asset.min_weight for asset in self.assets), ZERO)
        if min_total + self.cash_min_weight > ONE:
            raise ValueError("sum(asset.min_weight) + cash_min_weight must be <= 1.")
        return self


class BacktestTargetWeight(BaseModel):
    """Target weight for one generic asset id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    weight: Decimal

    @field_validator("asset_id", mode="before")
    @classmethod
    def validate_asset_id(cls, value: Any) -> str:
        return _normalize_required_string(value, field_name="asset_id")

    @field_validator("weight", mode="before")
    @classmethod
    def validate_weight(cls, value: Any) -> Decimal:
        return _to_decimal_no_float(value, field_name="weight")

    @model_validator(mode="after")
    def validate_weight_range(self) -> Self:
        _validate_unit_interval(self.weight, field_name="weight")
        return self


class BacktestTargetWeights(BaseModel):
    """Frozen output contract for one rules-only allocator decision step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_time: datetime
    allocator_version: str
    weights: tuple[BacktestTargetWeight, ...]

    @field_validator("decision_time", mode="before")
    @classmethod
    def validate_decision_time(cls, value: Any) -> datetime:
        return _parse_timezone_aware_datetime(value, field_name="decision_time")

    @field_validator("allocator_version", mode="before")
    @classmethod
    def validate_allocator_version(cls, value: Any) -> str:
        normalized = _normalize_required_string(value, field_name="allocator_version")
        if normalized != RULES_ALLOCATOR_V1:
            raise ValueError(f"allocator_version must be {RULES_ALLOCATOR_V1}.")
        return normalized

    @model_validator(mode="after")
    def validate_target_weights(self) -> Self:
        asset_ids = tuple(weight.asset_id for weight in self.weights)
        if len(set(asset_ids)) != len(asset_ids):
            raise ValueError("asset_id values must be unique.")

        total = sum((weight.weight for weight in self.weights), ZERO)
        if abs(total - ONE) > DECIMAL_WEIGHT_TOLERANCE:
            raise ValueError(
                "total weight must equal 1 within Decimal tolerance "
                f"{DECIMAL_WEIGHT_TOLERANCE}."
            )
        return self
