"""Single-step rules decision composition for Phase 2c-3.

This module composes already-validated building blocks for exactly one
decision time. It records a later intended run time for a future phase, while
staying limited to deterministic rules decision construction.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_data.source_records import InMemoryDateIdSourceReader
from backtest_engine.observation_spacing import (
    ObservationSpacingReport,
    validate_uniform_observation_spacing_for_count_based_ma,
)
from backtest_engine.rolling_features import (
    RollingLongMaAssetConfig,
    build_snapshot_configs_with_rolling_long_ma,
)
from backtest_engine.rules_allocator import (
    RULES_ALLOCATOR_V2_POLICY,
    RulesAllocatorV2StateInput,
    RulesAllocatorV2TargetWeights,
    allocate_rules_only_v1,
    allocate_rules_v2_target_weights,
)
from backtest_engine.snapshot_builder import (
    SnapshotAssetConfig,
    build_feature_snapshot_from_source_records,
)
from backtest_engine.step_contract import (
    RULES_ALLOCATOR_V1,
    BacktestFeatureSnapshot,
    BacktestTargetWeights,
)
from domain.source import DateIdSourceRecord


class BacktestSingleStepDecision(BaseModel):
    """Deterministic one-step rules-only decision artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_time: datetime
    intended_execution_time: datetime
    allocator_version: str
    observation_spacing_reports: tuple[ObservationSpacingReport, ...]
    snapshot_asset_configs: tuple[SnapshotAssetConfig, ...]
    feature_snapshot: BacktestFeatureSnapshot
    target_weights: BacktestTargetWeights | RulesAllocatorV2TargetWeights

    @field_validator("decision_time", "intended_execution_time", mode="before")
    @classmethod
    def validate_timestamps(cls, value: Any, info) -> datetime:
        return _parse_timezone_aware_datetime(value, field_name=info.field_name)

    @field_validator("allocator_version", mode="before")
    @classmethod
    def validate_allocator_version(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("allocator_version must be a string.")
        normalized = value.strip()
        if normalized not in {RULES_ALLOCATOR_V1, RULES_ALLOCATOR_V2_POLICY}:
            raise ValueError(
                "allocator_version must be a supported rules allocator version."
            )
        return normalized

    @model_validator(mode="after")
    def validate_decision_artifact(self) -> Self:
        if self.decision_time >= self.intended_execution_time:
            raise ValueError("decision_time must be before intended_execution_time.")
        if not self.observation_spacing_reports:
            raise ValueError("observation_spacing_reports must not be empty.")
        if not self.snapshot_asset_configs:
            raise ValueError("snapshot_asset_configs must not be empty.")

        if self.feature_snapshot.decision_time != self.decision_time:
            raise ValueError("feature_snapshot.decision_time must equal decision_time.")
        if self.target_weights.decision_time != self.decision_time:
            raise ValueError("target_weights.decision_time must equal decision_time.")
        if self.allocator_version != self.target_weights.allocator_version:
            raise ValueError("allocator_version must equal target_weights.allocator_version.")

        config_asset_ids = tuple(config.asset_id for config in self.snapshot_asset_configs)
        report_asset_ids = tuple(report.asset_id for report in self.observation_spacing_reports)
        feature_asset_ids = tuple(asset.asset_id for asset in self.feature_snapshot.assets)
        target_asset_ids = tuple(weight.asset_id for weight in self.target_weights.weights)

        if report_asset_ids != config_asset_ids:
            raise ValueError("observation_spacing_reports must match snapshot_asset_configs order.")
        if feature_asset_ids != config_asset_ids:
            raise ValueError("feature_snapshot assets must match snapshot_asset_configs order.")
        if target_asset_ids != (*feature_asset_ids, self.feature_snapshot.cash_asset_id):
            raise ValueError("target_weights must match feature_snapshot assets plus cash.")
        return self


def make_rules_only_single_step_decision(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    decision_time: datetime,
    intended_execution_time: datetime,
    rolling_asset_configs: Iterable[RollingLongMaAssetConfig],
    cash_asset_id: str,
    cash_min_weight: Decimal,
    rules_allocator_version: str = RULES_ALLOCATOR_V1,
) -> BacktestSingleStepDecision:
    """Build one deterministic rules-only decision artifact for one timestamp."""

    allocator_version = _validate_rules_allocator_version(rules_allocator_version)
    configs = tuple(rolling_asset_configs)
    step_source: InMemoryDateIdSourceReader | tuple[DateIdSourceRecord, ...]
    if isinstance(source, InMemoryDateIdSourceReader):
        step_source = source
    else:
        step_source = tuple(source)

    observation_spacing_reports = validate_uniform_observation_spacing_for_count_based_ma(
        step_source,
        decision_time=decision_time,
        asset_configs=configs,
    )
    snapshot_asset_configs = build_snapshot_configs_with_rolling_long_ma(
        step_source,
        decision_time=decision_time,
        asset_configs=configs,
    )
    feature_snapshot = build_feature_snapshot_from_source_records(
        step_source,
        decision_time=decision_time,
        asset_configs=snapshot_asset_configs,
        cash_asset_id=cash_asset_id,
        cash_min_weight=cash_min_weight,
    )
    if allocator_version == RULES_ALLOCATOR_V2_POLICY:
        target_weights = allocate_rules_v2_target_weights(
            state=RulesAllocatorV2StateInput(),
            cash_asset_id=cash_asset_id,
            decision_time=decision_time,
        )
    else:
        target_weights = allocate_rules_only_v1(feature_snapshot)

    return BacktestSingleStepDecision(
        decision_time=decision_time,
        intended_execution_time=intended_execution_time,
        allocator_version=target_weights.allocator_version,
        observation_spacing_reports=observation_spacing_reports,
        snapshot_asset_configs=snapshot_asset_configs,
        feature_snapshot=feature_snapshot,
        target_weights=target_weights,
    )


def build_single_step_rules_decision(
    source: InMemoryDateIdSourceReader | Iterable[DateIdSourceRecord],
    *,
    decision_time: datetime,
    intended_execution_time: datetime,
    asset_configs: Iterable[RollingLongMaAssetConfig],
    cash_asset_id: str,
    cash_min_weight: Decimal,
    rules_allocator_version: str = RULES_ALLOCATOR_V1,
) -> BacktestSingleStepDecision:
    """Compatibility wrapper for the Phase 2c-3 public builder."""

    return make_rules_only_single_step_decision(
        source,
        decision_time=decision_time,
        intended_execution_time=intended_execution_time,
        rolling_asset_configs=asset_configs,
        cash_asset_id=cash_asset_id,
        cash_min_weight=cash_min_weight,
        rules_allocator_version=rules_allocator_version,
    )


def _validate_rules_allocator_version(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("rules_allocator_version must be a string.")
    normalized = value.strip()
    if normalized not in {RULES_ALLOCATOR_V1, RULES_ALLOCATOR_V2_POLICY}:
        raise ValueError("rules_allocator_version must be a supported rules allocator version.")
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
