"""Frozen local monthly run configuration for Phase 2d-2.

This module turns an assembled ``LocalMonthlyDatasetAssemblyResult`` into a
frozen KOSPI-primary monthly run configuration with explicit period specs. It
does not read CSVs, fetch data, execute backtests, compute NAV, compute
benchmark-relative metrics, or render reports.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from backtest_engine.local_dataset import (
    LOCAL_MONTHLY_DATASET_POLICY_V1,
    LocalMonthlyDatasetAssemblyResult,
)
from backtest_engine.rebalance import COST_MODEL_V1, BacktestCostModel, BacktestPortfolioState
from backtest_engine.rolling_features import RollingLongMaAssetConfig
from backtest_engine.walk_forward import BacktestPeriodSpec
from domain._datetime import require_timezone_aware_datetime
from domain.source import DateIdSourceRecord

LOCAL_MONTHLY_RUN_CONFIG_POLICY_V1 = "kospi_primary_monthly_rules_config.v1"

_FIRST_DAY_OF_MONTH_WARNING_SUBSTRING = "first day of month"

_SIGNAL_WARMUP_WARNING = (
    "first rolling_lookback_count common periods are used as signal warm-up"
)

_KOSPI_PRIMARY_ROLLING_SPECS: tuple[
    tuple[str, str, str, Decimal, Decimal, Decimal, Decimal],
    ...,
] = (
    ("asset_us", "SP500TR", "US", Decimal("0.60"), Decimal("0.30"), Decimal("0"), Decimal("0.80")),
    ("asset_kr", "KOSPI", "KR", Decimal("0.20"), Decimal("0.05"), Decimal("0"), Decimal("0.40")),
    ("asset_gold", "GLD", "US", Decimal("0.15"), Decimal("0.25"), Decimal("0"), Decimal("0.35")),
)

ZERO = Decimal("0")
ONE = Decimal("1")


class LocalMonthlyRunConfig(BaseModel):
    """Frozen KOSPI-primary monthly run configuration from an assembled dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_monthly_run_config_policy: Literal["kospi_primary_monthly_rules_config.v1"]
    dataset: LocalMonthlyDatasetAssemblyResult
    period_specs: tuple[BacktestPeriodSpec, ...]
    rolling_asset_configs: tuple[RollingLongMaAssetConfig, ...]
    initial_portfolio_state: BacktestPortfolioState
    cost_model: BacktestCostModel
    cash_asset_id: str
    cash_min_weight: Decimal
    rolling_lookback_count: int
    warnings: tuple[str, ...]

    @field_validator("rolling_lookback_count", mode="before")
    @classmethod
    def validate_rolling_lookback_count(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("rolling_lookback_count must be an integer.")
        if value < 2:
            raise ValueError("rolling_lookback_count must be >= 2.")
        return value

    @field_validator("cash_asset_id")
    @classmethod
    def validate_cash_asset_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cash_asset_id must not be empty.")
        return value

    @field_validator("cash_min_weight", mode="before")
    @classmethod
    def validate_cash_min_weight(cls, value: Any) -> Decimal:
        parsed = _to_decimal_no_float(value, field_name="cash_min_weight")
        if parsed < ZERO or parsed > ONE:
            raise ValueError("cash_min_weight must be between 0 and 1.")
        return parsed

    @model_validator(mode="after")
    def validate_run_config(self) -> Self:
        if not self.period_specs:
            raise ValueError("period_specs must not be empty.")
        if not self.rolling_asset_configs:
            raise ValueError("rolling_asset_configs must not be empty.")

        expected_period_count = (
            len(self.dataset.common_periods) - self.rolling_lookback_count
        )
        if len(self.period_specs) != expected_period_count:
            raise ValueError(
                "period_specs count must equal "
                "len(dataset.common_periods) - rolling_lookback_count."
            )

        for previous, current in zip(self.period_specs, self.period_specs[1:], strict=False):
            if previous.decision_time >= current.decision_time:
                raise ValueError("period_specs must be strictly increasing by decision_time.")
            if previous.intended_execution_time >= current.intended_execution_time:
                raise ValueError(
                    "period_specs must be strictly increasing by intended_execution_time."
                )

        return self


def build_kospi_primary_monthly_run_config(
    *,
    dataset: LocalMonthlyDatasetAssemblyResult,
    initial_cash_krw: Decimal = Decimal("100000000"),
    cash_asset_id: str = "cash",
    cash_min_weight: Decimal = Decimal("0.05"),
    rolling_lookback_count: int = 3,
    fee_bps: Decimal = Decimal("10"),
    kr_sell_tax_bps: Decimal = Decimal("23"),
    fx_spread_bps: Decimal = Decimal("15"),
) -> LocalMonthlyRunConfig:
    """Build a frozen KOSPI-primary monthly run config from an assembled dataset."""
    if dataset.local_monthly_dataset_policy != LOCAL_MONTHLY_DATASET_POLICY_V1:
        raise ValueError(f"dataset must use {LOCAL_MONTHLY_DATASET_POLICY_V1} policy.")

    initial_cash = _to_decimal_no_float(initial_cash_krw, field_name="initial_cash_krw")
    if initial_cash <= ZERO:
        raise ValueError("initial_cash_krw must be greater than 0.")

    cash_floor = _to_decimal_no_float(cash_min_weight, field_name="cash_min_weight")
    if cash_floor < ZERO or cash_floor > ONE:
        raise ValueError("cash_min_weight must be between 0 and 1.")

    if isinstance(rolling_lookback_count, bool) or not isinstance(rolling_lookback_count, int):
        raise ValueError("rolling_lookback_count must be an integer.")
    if rolling_lookback_count < 2:
        raise ValueError("rolling_lookback_count must be >= 2.")

    fee = _validate_non_negative_bps(fee_bps, field_name="fee_bps")
    kr_tax = _validate_non_negative_bps(kr_sell_tax_bps, field_name="kr_sell_tax_bps")
    fx_spread = _validate_non_negative_bps(fx_spread_bps, field_name="fx_spread_bps")

    common_periods = dataset.common_periods
    if len(common_periods) < rolling_lookback_count + 1:
        raise ValueError(
            "dataset.common_periods must contain at least rolling_lookback_count + 1 periods."
        )

    latest_timestamps = _latest_source_timestamps_by_period(dataset.source_records)
    fx_rates = {point.period_key: point.usdkrw_rate for point in dataset.fx_points}

    period_specs = _build_period_specs(
        common_periods=common_periods,
        latest_timestamps=latest_timestamps,
        fx_rates=fx_rates,
        warmup_count=rolling_lookback_count,
    )

    first_period = common_periods[0]
    if first_period not in latest_timestamps:
        raise ValueError(f"missing source timestamp for warm-up period: {first_period}")
    initial_as_of = latest_timestamps[first_period]
    require_timezone_aware_datetime(initial_as_of, field_name="initial_portfolio_as_of")
    if initial_as_of <= datetime.min.replace(tzinfo=initial_as_of.tzinfo):
        raise ValueError("initial portfolio as_of must be a positive timezone-aware timestamp.")

    initial_portfolio_state = BacktestPortfolioState(
        as_of=initial_as_of,
        cash_krw=initial_cash,
        holdings=(),
    )

    rolling_asset_configs = _build_kospi_primary_rolling_configs(
        lookback_count=rolling_lookback_count,
    )

    cost_model = BacktestCostModel(
        cost_model_version=COST_MODEL_V1,
        fee_bps=fee,
        kr_sell_tax_bps=kr_tax,
        fx_spread_bps=fx_spread,
    )

    warnings = _collect_warnings(dataset.warnings)

    return LocalMonthlyRunConfig(
        local_monthly_run_config_policy=LOCAL_MONTHLY_RUN_CONFIG_POLICY_V1,
        dataset=dataset,
        period_specs=period_specs,
        rolling_asset_configs=rolling_asset_configs,
        initial_portfolio_state=initial_portfolio_state,
        cost_model=cost_model,
        cash_asset_id=cash_asset_id,
        cash_min_weight=cash_floor,
        rolling_lookback_count=rolling_lookback_count,
        warnings=warnings,
    )


def _build_kospi_primary_rolling_configs(
    *,
    lookback_count: int,
) -> tuple[RollingLongMaAssetConfig, ...]:
    return tuple(
        RollingLongMaAssetConfig(
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            lookback_count=lookback_count,
            risk_on_weight=risk_on_weight,
            risk_off_weight=risk_off_weight,
            min_weight=min_weight,
            max_weight=max_weight,
        )
        for (
            asset_id,
            symbol,
            market,
            risk_on_weight,
            risk_off_weight,
            min_weight,
            max_weight,
        ) in _KOSPI_PRIMARY_ROLLING_SPECS
    )


def _build_period_specs(
    *,
    common_periods: tuple[str, ...],
    latest_timestamps: dict[str, datetime],
    fx_rates: dict[str, Decimal],
    warmup_count: int,
) -> tuple[BacktestPeriodSpec, ...]:
    specs: list[BacktestPeriodSpec] = []
    for current_index in range(warmup_count, len(common_periods)):
        decision_period = common_periods[current_index - 1]
        execution_period = common_periods[current_index]

        if decision_period not in latest_timestamps:
            raise ValueError(f"missing source timestamp for period: {decision_period}")
        if execution_period not in latest_timestamps:
            raise ValueError(f"missing source timestamp for period: {execution_period}")
        if execution_period not in fx_rates:
            raise ValueError(f"missing USDKRW rate for period: {execution_period}")

        decision_time = latest_timestamps[decision_period]
        intended_execution_time = latest_timestamps[execution_period]
        usdkrw_rate = fx_rates[execution_period]

        require_timezone_aware_datetime(decision_time, field_name="decision_time")
        require_timezone_aware_datetime(intended_execution_time, field_name="intended_execution_time")
        if decision_time <= datetime.min.replace(tzinfo=decision_time.tzinfo):
            raise ValueError("decision_time must be a positive timezone-aware timestamp.")
        if intended_execution_time <= datetime.min.replace(tzinfo=intended_execution_time.tzinfo):
            raise ValueError(
                "intended_execution_time must be a positive timezone-aware timestamp."
            )
        if usdkrw_rate <= ZERO:
            raise ValueError("usdkrw_rate must be greater than 0.")

        specs.append(
            BacktestPeriodSpec(
                decision_time=decision_time,
                intended_execution_time=intended_execution_time,
                usdkrw_rate=usdkrw_rate,
            )
        )
    return tuple(specs)


def _latest_source_timestamps_by_period(
    source_records: tuple[DateIdSourceRecord, ...],
) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for record in source_records:
        period_key = _period_key_from_record(record)
        current = latest.get(period_key)
        if current is None or record.source_timestamp > current:
            latest[period_key] = record.source_timestamp
    return latest


def _period_key_from_record(record: DateIdSourceRecord) -> str:
    payload_date = record.payload.get("date")
    if not isinstance(payload_date, str) or not payload_date.strip():
        raise ValueError("source record payload.date must be a non-empty string.")
    try:
        parsed_date = date.fromisoformat(payload_date.strip())
    except ValueError as exc:
        raise ValueError(f"invalid source record payload.date: {payload_date!r}") from exc
    return f"{parsed_date.year:04d}-{parsed_date.month:02d}"


def _collect_warnings(dataset_warnings: tuple[str, ...]) -> tuple[str, ...]:
    collected = [
        warning
        for warning in dataset_warnings
        if _FIRST_DAY_OF_MONTH_WARNING_SUBSTRING in warning
    ]
    collected.append(_SIGNAL_WARMUP_WARNING)
    return tuple(collected)


def _validate_non_negative_bps(value: Any, *, field_name: str) -> Decimal:
    parsed = _to_decimal_no_float(value, field_name=field_name)
    if parsed < ZERO:
        raise ValueError(f"{field_name} must be >= 0.")
    return parsed


def _to_decimal_no_float(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"{field_name} must not be a float.")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a Decimal.") from exc
