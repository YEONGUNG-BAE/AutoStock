"""Sibling local monthly real-data evaluation dry-run for Phase 2d-3.

This module orchestrates local monthly CSV dataset assembly, KOSPI-primary run
config building, walk-forward NAV, benchmark-relative metrics, and markdown report
bundle rendering entirely in memory. It does not fetch or download data, write
report files, create artifacts, or produce investment conclusions.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from backtest_engine.benchmark_adapter import (
    BacktestBenchmarkRelativeResult,
    compute_walk_forward_benchmark_relative_metrics,
)
from backtest_engine.execution_prices import (
    select_execution_prices_for_single_step_decision,
)
from backtest_engine.local_dataset import (
    LocalMonthlyBenchmarkSpec,
    LocalMonthlyDatasetAssemblyResult,
    LocalMonthlyInstrumentSpec,
    assemble_local_monthly_dataset,
    default_local_monthly_benchmark_spec,
    default_local_monthly_instrument_specs_for_kospi_primary,
)
from backtest_engine.local_run_config import (
    LOCAL_RULES_ALLOCATOR_VERSION_V1,
    LOCAL_RULES_ALLOCATOR_VERSION_V2,
    LocalMonthlyRunConfig,
    build_kospi_primary_monthly_run_config,
)
from backtest_engine.observation_spacing import ObservationSpacingReport
from backtest_engine.period_step import BacktestSinglePeriodStepResult
from backtest_engine.rebalance import (
    BacktestHolding,
    BacktestPortfolioState,
    BacktestRebalanceResult,
    apply_single_rebalance_accounting,
)
from backtest_engine.report_bundle import (
    BacktestEvaluationReportBundle,
    render_backtest_evaluation_report_bundle,
)
from backtest_engine.single_step import BacktestSingleStepDecision
from backtest_engine.snapshot_builder import SnapshotAssetConfig
from backtest_engine.step_contract import (
    RULES_ALLOCATOR_V1,
    BacktestAssetFeature,
    BacktestFeatureSnapshot,
    BacktestTargetWeight,
    BacktestTargetWeights,
)
from backtest_engine.walk_forward import (
    WALK_FORWARD_POLICY_V1,
    BacktestNavPoint,
    BacktestWalkForwardResult,
    run_explicit_schedule_rules_walk_forward_nav,
)
from paper_review.models import BenchmarkReturnPoint

LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1 = (
    "sibling_local_monthly_kospi_primary_evaluation_dry_run.v1"
)
LOCAL_BENCHMARK_CALENDAR_ALIGNMENT_POLICY_V1 = (
    "local_monthly_benchmark_points_aligned_to_strategy_nav_calendar.v1"
)
LOCAL_BENCHMARK_METRIC_FREQUENCY_POLICY_V1 = (
    "local_monthly_benchmark_metrics_periods_per_year_12.v1"
)
LOCAL_STATIC_NEUTRAL_BASELINE_POLICY_V1 = (
    "local_monthly_static_neutral_baseline_us60_kr20_gold15_cash5.v1"
)
LOCAL_NAV_SANITY_POLICY_V1 = "local_monthly_walk_forward_nav_sanity.v1"
LOCAL_NAV_SANITY_POLICY_V2 = "local_monthly_walk_forward_nav_sanity.v2"
LOCAL_NAV_SANITY_DIAGNOSTIC_POLICY_V1 = (
    "local_monthly_walk_forward_nav_sanity_diagnostic.v1"
)
LOCAL_NAV_PERIOD_RETURN_DIAGNOSTIC_POLICY_V1 = (
    "local_monthly_walk_forward_nav_period_return_diagnostic.v1"
)
LOCAL_NAV_VALUATION_COMPONENT_DIAGNOSTIC_POLICY_V1 = (
    "local_monthly_walk_forward_nav_valuation_component_diagnostic.v1"
)

LOCAL_NAV_ACCOUNTING_ABS_TOLERANCE_KRW = Decimal("1E-6")
LOCAL_NAV_ACCOUNTING_REL_TOLERANCE = Decimal("1E-18")

_ZERO = Decimal("0")
_ONE = Decimal("1")
_FX_MARKETS = frozenset({"US", "GOLD"})
_SUPPORTED_MARKETS = frozenset({"KR", "US", "GOLD"})
# Evidence-quality guard: no single trade may exceed 2x pre-trade NAV notional.
_MAX_TRADE_GROSS_NOTIONAL_NAV_MULTIPLE = Decimal("2.00")

_NAV_SANITY_PASSED_WARNING = (
    "local monthly walk-forward NAV passed deterministic sanity checks"
)
_NAV_ACCOUNTING_DRIFT_WARNING = (
    "local NAV accounting identity had immaterial Decimal drift within tolerance"
)

_RESEARCH_ONLY_WARNING = (
    "real-data dry-run result is research evidence only; "
    "it is not an investment conclusion"
)
_KOSPI_PROXY_WARNING = (
    "KOSPI primary is a KR proxy, not implementable ETF evidence"
)
_BENCHMARK_CALENDAR_ALIGNMENT_WARNING = (
    "local benchmark points are calendar-aligned to strategy NAV timestamps "
    "before metric adaptation"
)
_BENCHMARK_METRIC_FREQUENCY_WARNING = (
    "local monthly benchmark metrics use periods_per_year=12 for annualized "
    "information ratio"
)
_TRACKING_ERROR_LEGACY_FIELD_WARNING = (
    "tracking_error_daily_percent is a legacy field name; local monthly value "
    "is per aligned observation"
)
_STATIC_NEUTRAL_NON_TACTICAL_WARNING = (
    "local static neutral baseline is non-tactical and not optimized to current evidence"
)
_STATIC_NEUTRAL_FIXED_WEIGHTS_WARNING = (
    "local static neutral baseline uses fixed weights: US 0.60, KR 0.20, "
    "GOLD 0.15, CASH 0.05"
)
_STATIC_NEUTRAL_RESEARCH_ONLY_WARNING = (
    "local static neutral baseline result is research evidence only; "
    "it is not an investment conclusion"
)
LOCAL_RULES_ALLOCATOR_V2_STATIC_NORMAL_STATE_POLICY = (
    "local_monthly_rules_allocator_v2_static_normal_state.v1"
)


def resolve_local_rules_allocator_v2_state_policy(
    rules_allocator_version: str,
) -> str | None:
    """Map a local rules allocator version to its V2 state policy attribution."""
    if rules_allocator_version == LOCAL_RULES_ALLOCATOR_VERSION_V1:
        return None
    if rules_allocator_version == LOCAL_RULES_ALLOCATOR_VERSION_V2:
        return LOCAL_RULES_ALLOCATOR_V2_STATIC_NORMAL_STATE_POLICY
    raise ValueError(
        "rules_allocator_version must be a supported local rules allocator version."
    )


_RULES_ALLOCATOR_V2_STATIC_NORMAL_WARNING = (
    "local rules allocator v2 uses static normal-state integration; "
    "relative recovery state machine is not implemented yet"
)

LOCAL_STATIC_NEUTRAL_BASELINE_WEIGHTS_V1: tuple[tuple[str, Decimal], ...] = (
    ("asset_us", Decimal("0.60")),
    ("asset_kr", Decimal("0.20")),
    ("asset_gold", Decimal("0.15")),
    ("cash", Decimal("0.05")),
)
_STATIC_NEUTRAL_CASH_WEIGHT = Decimal("0.05")


class LocalNavSanityStepDiagnostic(BaseModel):
    """Sanitized per-step NAV accounting diagnostic for local evidence quality."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_nav_sanity_diagnostic_policy: Literal[
        "local_monthly_walk_forward_nav_sanity_diagnostic.v1"
    ]
    step_index: int
    period_index: int
    decision_time: datetime
    intended_execution_time: datetime
    nav_as_of: datetime
    asset_count: int
    trade_count: int
    holding_count: int
    pre_trade_nav_krw: Decimal
    post_trade_portfolio_value_krw: Decimal
    cash_krw_after: Decimal
    recomputed_post_trade_value_krw: Decimal
    accounting_delta_krw: Decimal
    accounting_delta_ratio: Decimal
    max_trade_notional_to_pre_nav_ratio: Decimal | None
    nonzero_holding_asset_ids: tuple[str, ...]
    traded_asset_ids: tuple[str, ...]
    markets_seen: tuple[str, ...]
    warnings: tuple[str, ...]


class LocalNavValuationComponentAssetDiagnostic(BaseModel):
    """Sanitized per-asset NAV valuation component diagnostic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    market: str
    had_previous_holding: bool
    has_current_holding: bool
    was_traded_current_step: bool
    previous_value_krw: Decimal
    current_value_krw: Decimal
    value_delta_krw: Decimal
    value_ratio: Decimal | None
    contribution_to_nav_delta_ratio: Decimal | None
    execution_price_ratio: Decimal | None
    usdkrw_rate_ratio: Decimal | None
    holding_quantity_ratio: Decimal | None
    warnings: tuple[str, ...]


class LocalNavValuationComponentDiagnostic(BaseModel):
    """Sanitized NAV valuation component diagnostic for one nav point pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_nav_valuation_component_diagnostic_policy: Literal[
        "local_monthly_walk_forward_nav_valuation_component_diagnostic.v1"
    ]
    nav_point_index: int
    previous_step_index: int
    current_step_index: int
    previous_as_of: datetime
    current_as_of: datetime
    previous_portfolio_value_krw: Decimal
    current_portfolio_value_krw: Decimal
    nav_delta_krw: Decimal
    period_return: Decimal
    asset_count: int
    current_trade_count: int
    previous_cash_krw: Decimal
    current_cash_krw: Decimal
    cash_delta_krw: Decimal
    cash_delta_to_nav_delta_ratio: Decimal | None
    component_sum_delta_krw: Decimal
    component_sum_delta_minus_nav_delta_krw: Decimal
    largest_positive_component_asset_id: str | None
    largest_positive_component_delta_to_nav_delta_ratio: Decimal | None
    asset_diagnostics: tuple[LocalNavValuationComponentAssetDiagnostic, ...]
    warnings: tuple[str, ...]


class LocalNavPeriodReturnDiagnostic(BaseModel):
    """Sanitized NAV period-return diagnostic for local evidence quality."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_nav_period_return_diagnostic_policy: Literal[
        "local_monthly_walk_forward_nav_period_return_diagnostic.v1"
    ]
    nav_point_index: int
    previous_nav_point_index: int
    previous_step_index: int
    current_step_index: int
    previous_as_of: datetime
    current_as_of: datetime
    previous_portfolio_value_krw: Decimal
    current_portfolio_value_krw: Decimal
    period_return: Decimal
    abs_period_return: Decimal
    max_abs_period_return: Decimal
    previous_cash_krw: Decimal
    current_cash_krw: Decimal
    previous_cash_weight: Decimal
    current_cash_weight: Decimal
    previous_holding_asset_ids: tuple[str, ...]
    current_holding_asset_ids: tuple[str, ...]
    current_traded_asset_ids: tuple[str, ...]
    current_markets_seen: tuple[str, ...]
    current_trade_count: int
    current_holding_count: int
    current_max_trade_notional_to_pre_nav_ratio: Decimal | None
    warnings: tuple[str, ...]


class LocalStaticNeutralBaselineResult(BaseModel):
    """Frozen local static neutral baseline result for sanitized evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_static_neutral_baseline_policy: Literal[
        "local_monthly_static_neutral_baseline_us60_kr20_gold15_cash5.v1"
    ]
    walk_forward_result: BacktestWalkForwardResult
    benchmark_relative_result: BacktestBenchmarkRelativeResult
    warnings: tuple[str, ...]


class LocalMonthlyEvaluationDryRunResult(BaseModel):
    """Immutable in-memory local monthly real-data evaluation dry-run result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_monthly_evaluation_dry_run_policy: Literal[
        "sibling_local_monthly_kospi_primary_evaluation_dry_run.v1"
    ]
    dataset: LocalMonthlyDatasetAssemblyResult
    run_config: LocalMonthlyRunConfig
    walk_forward_result: BacktestWalkForwardResult
    benchmark_relative_result: BacktestBenchmarkRelativeResult
    static_neutral_baseline_result: LocalStaticNeutralBaselineResult
    report_bundle: BacktestEvaluationReportBundle
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.run_config.dataset != self.dataset:
            raise ValueError("run_config.dataset must equal dataset.")
        if (
            self.benchmark_relative_result.walk_forward_result
            != self.walk_forward_result
        ):
            raise ValueError(
                "benchmark_relative_result.walk_forward_result must equal "
                "walk_forward_result."
            )
        if (
            self.report_bundle.benchmark_relative_result
            != self.benchmark_relative_result
        ):
            raise ValueError(
                "report_bundle.benchmark_relative_result must equal "
                "benchmark_relative_result."
            )
        if (
            self.static_neutral_baseline_result.benchmark_relative_result.walk_forward_result
            != self.static_neutral_baseline_result.walk_forward_result
        ):
            raise ValueError(
                "static baseline benchmark_relative_result.walk_forward_result "
                "must equal static baseline walk_forward_result."
            )
        if len(self.walk_forward_result.steps) != len(self.run_config.period_specs):
            raise ValueError(
                "walk_forward_result.steps length must equal "
                "run_config.period_specs length."
            )
        if len(self.walk_forward_result.nav_points) != len(
            self.run_config.period_specs
        ):
            raise ValueError(
                "walk_forward_result.nav_points length must equal "
                "run_config.period_specs length."
            )
        if (
            self.walk_forward_result.initial_portfolio_state
            != self.run_config.initial_portfolio_state
        ):
            raise ValueError(
                "walk_forward_result.initial_portfolio_state must equal "
                "run_config.initial_portfolio_state."
            )
        if len(self.static_neutral_baseline_result.walk_forward_result.steps) != len(
            self.run_config.period_specs
        ):
            raise ValueError(
                "static baseline steps length must equal run_config.period_specs length."
            )
        if len(
            self.static_neutral_baseline_result.walk_forward_result.nav_points
        ) != len(self.run_config.period_specs):
            raise ValueError(
                "static baseline nav_points length must equal "
                "run_config.period_specs length."
            )
        if (
            self.static_neutral_baseline_result.walk_forward_result.initial_portfolio_state
            != self.run_config.initial_portfolio_state
        ):
            raise ValueError(
                "static baseline initial_portfolio_state must equal "
                "run_config.initial_portfolio_state."
            )
        return self


def align_local_monthly_benchmark_points_to_nav_calendar(
    *,
    run_config: LocalMonthlyRunConfig,
    walk_forward_result: BacktestWalkForwardResult,
    benchmark_points: Iterable[BenchmarkReturnPoint],
) -> tuple[BenchmarkReturnPoint, ...]:
    """Align local monthly benchmark points onto strategy NAV calendar dates."""
    materialized_benchmark_points = tuple(benchmark_points)
    nav_points = walk_forward_result.nav_points
    common_periods = run_config.dataset.common_periods
    rolling_lookback_count = run_config.rolling_lookback_count

    if len(nav_points) != len(run_config.period_specs):
        raise ValueError(
            "walk_forward_result.nav_points length must equal "
            "run_config.period_specs length."
        )
    if len(nav_points) < 2:
        raise ValueError("at least 2 walk-forward NAV points are required.")
    if not materialized_benchmark_points:
        raise ValueError("benchmark_points must not be empty.")

    fx_points = run_config.dataset.fx_points
    if len(materialized_benchmark_points) != len(fx_points):
        raise ValueError(
            "benchmark_points length must equal dataset.fx_points length."
        )

    seen_periods: set[str] = set()
    period_to_benchmark: dict[str, BenchmarkReturnPoint] = {}
    for fx_point, benchmark_point in zip(fx_points, materialized_benchmark_points, strict=True):
        if fx_point.period_key in seen_periods:
            raise ValueError(f"duplicate benchmark period key: {fx_point.period_key}")
        seen_periods.add(fx_point.period_key)
        period_to_benchmark[fx_point.period_key] = benchmark_point

    if len(period_to_benchmark) != len(fx_points):
        raise ValueError("duplicate benchmark period keys detected.")

    aligned_points: list[BenchmarkReturnPoint] = []
    for index, nav_point in enumerate(nav_points):
        execution_period = common_periods[rolling_lookback_count + index]
        benchmark_point = period_to_benchmark.get(execution_period)
        if benchmark_point is None:
            raise ValueError(
                f"missing benchmark point for execution period: {execution_period}"
            )
        aligned_points.append(
            BenchmarkReturnPoint(
                as_of=nav_point.as_of,
                total_return_index_value=benchmark_point.total_return_index_value,
            )
        )

    if len(aligned_points) != len(nav_points):
        raise ValueError(
            "aligned benchmark point count must equal walk-forward NAV point count."
        )

    aligned_dates = tuple(point.as_of.date() for point in aligned_points)
    nav_dates = tuple(point.as_of.date() for point in nav_points)
    if aligned_dates != nav_dates:
        raise ValueError(
            "aligned benchmark as_of.date() sequence must equal NAV as_of.date() sequence."
        )

    for previous, current in zip(aligned_dates, aligned_dates[1:], strict=False):
        if previous >= current:
            raise ValueError(
                "aligned benchmark calendar dates must be strictly increasing."
            )

    return tuple(aligned_points)


def _is_material_nav_accounting_delta(
    *,
    accounting_delta_krw: Decimal,
    post_trade_portfolio_value_krw: Decimal,
    abs_tolerance_krw: Decimal = LOCAL_NAV_ACCOUNTING_ABS_TOLERANCE_KRW,
    rel_tolerance: Decimal = LOCAL_NAV_ACCOUNTING_REL_TOLERANCE,
) -> bool:
    if not abs_tolerance_krw.is_finite() or abs_tolerance_krw < _ZERO:
        raise ValueError("abs_tolerance_krw must be a non-negative finite Decimal.")
    if not rel_tolerance.is_finite() or rel_tolerance < _ZERO:
        raise ValueError("rel_tolerance must be a non-negative finite Decimal.")
    if not post_trade_portfolio_value_krw.is_finite():
        raise ValueError(
            "post_trade_portfolio_value_krw must be a finite Decimal."
        )

    abs_delta = abs(accounting_delta_krw)

    if abs_delta <= abs_tolerance_krw:
        return False

    if post_trade_portfolio_value_krw > _ZERO:
        rel_delta = abs_delta / post_trade_portfolio_value_krw
        if rel_delta <= rel_tolerance:
            return False

    return True


def validate_local_monthly_walk_forward_nav_sanity(
    *,
    run_config: LocalMonthlyRunConfig,
    walk_forward_result: BacktestWalkForwardResult,
    max_abs_period_return: Decimal = Decimal("1.00"),
    max_terminal_return: Decimal = Decimal("20.00"),
) -> tuple[str, ...]:
    """Validate walk-forward NAV and position accounting for local evidence quality.

    These checks are evidence-quality guards for local dry-run export. They are
    not strategy objectives and not investment rules. Hard violations raise
    ``ValueError`` to block misleading evidence before benchmark metrics or export.
    """
    nav_points = walk_forward_result.nav_points
    period_specs = run_config.period_specs
    steps = walk_forward_result.steps

    if len(nav_points) != len(period_specs):
        raise ValueError(
            "walk_forward_result.nav_points length must equal "
            "run_config.period_specs length for NAV sanity."
        )
    if len(steps) != len(period_specs):
        raise ValueError(
            "walk_forward_result.steps length must equal "
            "run_config.period_specs length for NAV sanity."
        )

    nav_sanity_warnings: list[str] = []
    saw_immaterial_accounting_drift = False

    for index, nav_point in enumerate(nav_points):
        if nav_point.portfolio_value_krw <= _ZERO:
            raise ValueError(
                f"nav_points[{index}].portfolio_value_krw must be positive."
            )
        if nav_point.cash_krw < _ZERO:
            raise ValueError(f"nav_points[{index}].cash_krw must be >= 0.")
        if nav_point.cash_krw > nav_point.portfolio_value_krw:
            raise ValueError(
                f"nav_points[{index}].cash_krw must be <= portfolio_value_krw."
            )

    for index, step in enumerate(steps):
        rebalance = step.rebalance_result
        period_spec = period_specs[index]
        usdkrw_rate = period_spec.usdkrw_rate

        if rebalance.post_trade_portfolio_value_krw <= _ZERO:
            raise ValueError(
                f"steps[{index}].rebalance_result.post_trade_portfolio_value_krw "
                "must be positive."
            )
        if rebalance.cash_krw_after < _ZERO:
            raise ValueError(
                f"steps[{index}].rebalance_result.cash_krw_after must be >= 0."
            )

        holding_ids = tuple(holding.asset_id for holding in rebalance.post_trade_holdings)
        if len(holding_ids) != len(set(holding_ids)):
            raise ValueError(
                f"steps[{index}].rebalance_result.post_trade_holdings "
                "must have unique asset ids."
            )

        holdings_value_krw = _compute_post_trade_holdings_value_krw_for_sanity(
            step,
            usdkrw_rate=usdkrw_rate,
            step_index=index,
        )

        recomputed_post_trade = rebalance.cash_krw_after + holdings_value_krw
        accounting_delta_krw = (
            recomputed_post_trade - rebalance.post_trade_portfolio_value_krw
        )
        if _is_material_nav_accounting_delta(
            accounting_delta_krw=accounting_delta_krw,
            post_trade_portfolio_value_krw=rebalance.post_trade_portfolio_value_krw,
        ):
            raise ValueError(
                f"steps[{index}] post-trade holdings value plus cash must equal "
                "post_trade_portfolio_value_krw; run sanitized NAV sanity diagnostic "
                "for this step."
            )
        if accounting_delta_krw != _ZERO:
            saw_immaterial_accounting_drift = True

        pre_trade_nav = rebalance.pre_trade_portfolio_value_krw
        max_trade_notional = pre_trade_nav * _MAX_TRADE_GROSS_NOTIONAL_NAV_MULTIPLE
        for trade_index, trade in enumerate(rebalance.trades):
            if not trade.quantity.is_finite() or trade.quantity <= _ZERO:
                raise ValueError(
                    f"steps[{index}].trades[{trade_index}].quantity must be "
                    "finite and positive."
                )
            if not trade.gross_notional_krw.is_finite():
                raise ValueError(
                    f"steps[{index}].trades[{trade_index}].gross_notional_krw "
                    "must be finite."
                )
            if trade.gross_notional_krw > max_trade_notional:
                raise ValueError(
                    f"steps[{index}].trades[{trade_index}].gross_notional_krw "
                    "exceeds deterministic pre-trade NAV multiple."
                )
            if trade.market not in _SUPPORTED_MARKETS:
                raise ValueError(
                    f"steps[{index}].trades[{trade_index}] has unsupported market: "
                    f"{trade.market!r}."
                )

    for index in range(1, len(nav_points)):
        previous_nav = nav_points[index - 1].portfolio_value_krw
        current_nav = nav_points[index].portfolio_value_krw
        if previous_nav <= _ZERO:
            raise ValueError(
                f"nav_points[{index - 1}].portfolio_value_krw must be positive "
                "for period return sanity."
            )
        period_return = (current_nav / previous_nav) - _ONE
        if not period_return.is_finite():
            raise ValueError(f"nav_points[{index}] period return must be finite.")
        if abs(period_return) > max_abs_period_return:
            raise ValueError(
                f"nav_points[{index}] period return exceeds max_abs_period_return; "
                "run sanitized NAV period return diagnostic for this nav point."
            )

    initial_nav = steps[0].rebalance_result.pre_trade_portfolio_value_krw
    if initial_nav <= _ZERO:
        raise ValueError("initial pre-trade portfolio value must be positive.")
    terminal_nav = nav_points[-1].portfolio_value_krw
    terminal_return = (terminal_nav / initial_nav) - _ONE
    if not terminal_return.is_finite():
        raise ValueError("terminal strategy return must be finite.")
    if terminal_return > max_terminal_return:
        raise ValueError("terminal strategy return exceeds max_terminal_return.")

    nav_sanity_warnings.append(_NAV_SANITY_PASSED_WARNING)
    if saw_immaterial_accounting_drift:
        nav_sanity_warnings.append(_NAV_ACCOUNTING_DRIFT_WARNING)
    return tuple(nav_sanity_warnings)


def build_local_nav_sanity_step_diagnostic(
    *,
    run_config: LocalMonthlyRunConfig,
    walk_forward_result: BacktestWalkForwardResult,
    step_index: int,
) -> LocalNavSanityStepDiagnostic:
    """Build a sanitized NAV accounting diagnostic for one walk-forward step."""
    period_specs = run_config.period_specs
    steps = walk_forward_result.steps
    nav_points = walk_forward_result.nav_points

    if step_index < 0 or step_index >= len(period_specs):
        raise ValueError(
            f"step_index must be in [0, {len(period_specs)}); got {step_index}."
        )
    if len(steps) != len(period_specs):
        raise ValueError(
            "walk_forward_result.steps length must equal "
            "run_config.period_specs length for NAV sanity diagnostic."
        )
    if len(nav_points) != len(period_specs):
        raise ValueError(
            "walk_forward_result.nav_points length must equal "
            "run_config.period_specs length for NAV sanity diagnostic."
        )

    period_spec = period_specs[step_index]
    step = steps[step_index]
    nav_point = nav_points[step_index]
    rebalance = step.rebalance_result
    usdkrw_rate = period_spec.usdkrw_rate

    recomputed_holdings_value_krw = _compute_post_trade_holdings_value_krw_for_sanity(
        step,
        usdkrw_rate=usdkrw_rate,
        step_index=step_index,
        raise_on_unsupported_market=False,
    )
    recomputed_post_trade_value_krw = (
        rebalance.cash_krw_after + recomputed_holdings_value_krw
    )
    accounting_delta_krw = (
        recomputed_post_trade_value_krw - rebalance.post_trade_portfolio_value_krw
    )
    if rebalance.post_trade_portfolio_value_krw == _ZERO:
        accounting_delta_ratio = _ZERO
    else:
        accounting_delta_ratio = (
            accounting_delta_krw / rebalance.post_trade_portfolio_value_krw
        )

    pre_trade_nav_krw = rebalance.pre_trade_portfolio_value_krw
    if rebalance.trades:
        max_trade_notional_to_pre_nav_ratio = max(
            trade.gross_notional_krw / pre_trade_nav_krw
            for trade in rebalance.trades
            if pre_trade_nav_krw > _ZERO
        )
    else:
        max_trade_notional_to_pre_nav_ratio = None

    nonzero_holding_asset_ids = tuple(
        sorted(
            holding.asset_id
            for holding in rebalance.post_trade_holdings
            if holding.quantity > _ZERO
        )
    )
    traded_asset_ids = tuple(
        sorted({trade.asset_id for trade in rebalance.trades})
    )
    markets_seen = tuple(
        sorted(
            {
                price_record.market
                for price_record in step.execution_prices.prices
            }
            | {trade.market for trade in rebalance.trades}
        )
    )

    warnings = _collect_nav_sanity_step_diagnostic_warnings(
        rebalance=rebalance,
        accounting_delta_krw=accounting_delta_krw,
        post_trade_portfolio_value_krw=rebalance.post_trade_portfolio_value_krw,
        max_trade_notional_to_pre_nav_ratio=max_trade_notional_to_pre_nav_ratio,
        markets_seen=markets_seen,
    )

    return LocalNavSanityStepDiagnostic(
        local_nav_sanity_diagnostic_policy=LOCAL_NAV_SANITY_DIAGNOSTIC_POLICY_V1,
        step_index=step_index,
        period_index=run_config.rolling_lookback_count + step_index,
        decision_time=period_spec.decision_time,
        intended_execution_time=period_spec.intended_execution_time,
        nav_as_of=nav_point.as_of,
        asset_count=len(step.execution_prices.prices),
        trade_count=len(rebalance.trades),
        holding_count=len(rebalance.post_trade_holdings),
        pre_trade_nav_krw=pre_trade_nav_krw,
        post_trade_portfolio_value_krw=rebalance.post_trade_portfolio_value_krw,
        cash_krw_after=rebalance.cash_krw_after,
        recomputed_post_trade_value_krw=recomputed_post_trade_value_krw,
        accounting_delta_krw=accounting_delta_krw,
        accounting_delta_ratio=accounting_delta_ratio,
        max_trade_notional_to_pre_nav_ratio=max_trade_notional_to_pre_nav_ratio,
        nonzero_holding_asset_ids=nonzero_holding_asset_ids,
        traded_asset_ids=traded_asset_ids,
        markets_seen=markets_seen,
        warnings=warnings,
    )


def build_local_nav_period_return_diagnostic(
    *,
    run_config: LocalMonthlyRunConfig,
    walk_forward_result: BacktestWalkForwardResult,
    nav_point_index: int,
    max_abs_period_return: Decimal = Decimal("1.00"),
) -> LocalNavPeriodReturnDiagnostic:
    """Build a sanitized NAV period-return diagnostic for one nav point pair."""
    nav_points = walk_forward_result.nav_points
    steps = walk_forward_result.steps
    period_specs = run_config.period_specs

    if nav_point_index < 1:
        raise ValueError(
            f"nav_point_index must be >= 1; got {nav_point_index}."
        )
    if nav_point_index >= len(nav_points):
        raise ValueError(
            f"nav_point_index must be in [1, {len(nav_points)}); got {nav_point_index}."
        )
    if len(nav_points) != len(period_specs):
        raise ValueError(
            "walk_forward_result.nav_points length must equal "
            "run_config.period_specs length for NAV period return diagnostic."
        )
    if len(steps) != len(period_specs):
        raise ValueError(
            "walk_forward_result.steps length must equal "
            "run_config.period_specs length for NAV period return diagnostic."
        )
    if not max_abs_period_return.is_finite() or max_abs_period_return < _ZERO:
        raise ValueError(
            "max_abs_period_return must be a non-negative finite Decimal."
        )

    previous_nav_point_index = nav_point_index - 1
    previous_step_index = nav_point_index - 1
    current_step_index = nav_point_index

    previous_nav_point = nav_points[previous_nav_point_index]
    current_nav_point = nav_points[nav_point_index]
    previous_step = steps[previous_step_index]
    current_step = steps[current_step_index]

    previous_portfolio_value_krw = previous_nav_point.portfolio_value_krw
    current_portfolio_value_krw = current_nav_point.portfolio_value_krw
    period_return = (current_portfolio_value_krw / previous_portfolio_value_krw) - _ONE
    abs_period_return = abs(period_return)

    previous_cash_krw = previous_nav_point.cash_krw
    current_cash_krw = current_nav_point.cash_krw
    previous_cash_weight = previous_cash_krw / previous_portfolio_value_krw
    current_cash_weight = current_cash_krw / current_portfolio_value_krw

    previous_holding_asset_ids = tuple(
        sorted(
            holding.asset_id
            for holding in previous_step.rebalance_result.post_trade_holdings
            if holding.quantity > _ZERO
        )
    )
    current_holding_asset_ids = tuple(
        sorted(
            holding.asset_id
            for holding in current_step.rebalance_result.post_trade_holdings
            if holding.quantity > _ZERO
        )
    )
    current_traded_asset_ids = tuple(
        sorted({trade.asset_id for trade in current_step.rebalance_result.trades})
    )
    current_markets_seen = tuple(
        sorted(
            {
                price_record.market
                for price_record in current_step.execution_prices.prices
            }
            | {trade.market for trade in current_step.rebalance_result.trades}
        )
    )

    current_rebalance = current_step.rebalance_result
    current_trade_count = len(current_rebalance.trades)
    current_holding_count = len(current_rebalance.post_trade_holdings)

    pre_trade_nav_krw = current_rebalance.pre_trade_portfolio_value_krw
    if current_rebalance.trades and pre_trade_nav_krw > _ZERO:
        current_max_trade_notional_to_pre_nav_ratio = max(
            trade.gross_notional_krw / pre_trade_nav_krw
            for trade in current_rebalance.trades
        )
    else:
        current_max_trade_notional_to_pre_nav_ratio = None

    warnings = _collect_nav_period_return_diagnostic_warnings(
        period_return=period_return,
        abs_period_return=abs_period_return,
        max_abs_period_return=max_abs_period_return,
        previous_cash_weight=previous_cash_weight,
        current_cash_weight=current_cash_weight,
        current_max_trade_notional_to_pre_nav_ratio=(
            current_max_trade_notional_to_pre_nav_ratio
        ),
        current_trade_count=current_trade_count,
    )

    return LocalNavPeriodReturnDiagnostic(
        local_nav_period_return_diagnostic_policy=(
            LOCAL_NAV_PERIOD_RETURN_DIAGNOSTIC_POLICY_V1
        ),
        nav_point_index=nav_point_index,
        previous_nav_point_index=previous_nav_point_index,
        previous_step_index=previous_step_index,
        current_step_index=current_step_index,
        previous_as_of=previous_nav_point.as_of,
        current_as_of=current_nav_point.as_of,
        previous_portfolio_value_krw=previous_portfolio_value_krw,
        current_portfolio_value_krw=current_portfolio_value_krw,
        period_return=period_return,
        abs_period_return=abs_period_return,
        max_abs_period_return=max_abs_period_return,
        previous_cash_krw=previous_cash_krw,
        current_cash_krw=current_cash_krw,
        previous_cash_weight=previous_cash_weight,
        current_cash_weight=current_cash_weight,
        previous_holding_asset_ids=previous_holding_asset_ids,
        current_holding_asset_ids=current_holding_asset_ids,
        current_traded_asset_ids=current_traded_asset_ids,
        current_markets_seen=current_markets_seen,
        current_trade_count=current_trade_count,
        current_holding_count=current_holding_count,
        current_max_trade_notional_to_pre_nav_ratio=(
            current_max_trade_notional_to_pre_nav_ratio
        ),
        warnings=warnings,
    )


def build_local_nav_valuation_component_diagnostic(
    *,
    run_config: LocalMonthlyRunConfig,
    walk_forward_result: BacktestWalkForwardResult,
    nav_point_index: int,
) -> LocalNavValuationComponentDiagnostic:
    """Build a sanitized NAV valuation component diagnostic for one nav point pair."""
    nav_points = walk_forward_result.nav_points
    steps = walk_forward_result.steps
    period_specs = run_config.period_specs

    if nav_point_index < 1:
        raise ValueError(
            f"nav_point_index must be >= 1; got {nav_point_index}."
        )
    if nav_point_index >= len(nav_points):
        raise ValueError(
            f"nav_point_index must be in [1, {len(nav_points)}); got {nav_point_index}."
        )
    if len(nav_points) != len(period_specs):
        raise ValueError(
            "walk_forward_result.nav_points length must equal "
            "run_config.period_specs length for NAV valuation component diagnostic."
        )
    if len(steps) != len(period_specs):
        raise ValueError(
            "walk_forward_result.steps length must equal "
            "run_config.period_specs length for NAV valuation component diagnostic."
        )

    previous_step_index = nav_point_index - 1
    current_step_index = nav_point_index

    previous_nav_point = nav_points[previous_step_index]
    current_nav_point = nav_points[current_step_index]
    previous_step = steps[previous_step_index]
    current_step = steps[current_step_index]
    previous_period_spec = period_specs[previous_step_index]
    current_period_spec = period_specs[current_step_index]

    previous_portfolio_value_krw = previous_nav_point.portfolio_value_krw
    current_portfolio_value_krw = current_nav_point.portfolio_value_krw
    nav_delta_krw = current_portfolio_value_krw - previous_portfolio_value_krw
    period_return = (current_portfolio_value_krw / previous_portfolio_value_krw) - _ONE

    previous_cash_krw = previous_nav_point.cash_krw
    current_cash_krw = current_nav_point.cash_krw
    cash_delta_krw = current_cash_krw - previous_cash_krw
    if nav_delta_krw == _ZERO:
        cash_delta_to_nav_delta_ratio = None
    else:
        cash_delta_to_nav_delta_ratio = cash_delta_krw / nav_delta_krw

    current_traded_asset_ids = {
        trade.asset_id for trade in current_step.rebalance_result.trades
    }
    current_trade_count = len(current_step.rebalance_result.trades)

    asset_ids = _collect_valuation_component_asset_ids(
        previous_step=previous_step,
        current_step=current_step,
        current_traded_asset_ids=current_traded_asset_ids,
    )

    asset_diagnostics: list[LocalNavValuationComponentAssetDiagnostic] = []
    for asset_id in sorted(asset_ids):
        asset_diagnostics.append(
            _build_valuation_component_asset_diagnostic(
                asset_id=asset_id,
                previous_step=previous_step,
                current_step=current_step,
                previous_usdkrw_rate=previous_period_spec.usdkrw_rate,
                current_usdkrw_rate=current_period_spec.usdkrw_rate,
                nav_delta_krw=nav_delta_krw,
                current_traded_asset_ids=current_traded_asset_ids,
            )
        )

    asset_diagnostics_tuple = tuple(asset_diagnostics)
    asset_value_delta_sum = sum(
        (asset.value_delta_krw for asset in asset_diagnostics_tuple),
        start=_ZERO,
    )
    component_sum_delta_krw = cash_delta_krw + asset_value_delta_sum
    component_sum_delta_minus_nav_delta_krw = (
        component_sum_delta_krw - nav_delta_krw
    )

    largest_positive_component_asset_id: str | None = None
    largest_positive_component_delta: Decimal | None = None
    for asset_diag in asset_diagnostics_tuple:
        if asset_diag.value_delta_krw <= _ZERO:
            continue
        if (
            largest_positive_component_delta is None
            or asset_diag.value_delta_krw > largest_positive_component_delta
        ):
            largest_positive_component_delta = asset_diag.value_delta_krw
            largest_positive_component_asset_id = asset_diag.asset_id

    if (
        largest_positive_component_asset_id is None
        or nav_delta_krw == _ZERO
    ):
        largest_positive_component_delta_to_nav_delta_ratio = None
    else:
        largest_positive_component_delta_to_nav_delta_ratio = (
            largest_positive_component_delta / nav_delta_krw
        )

    warnings = _collect_nav_valuation_component_diagnostic_warnings(
        period_return=period_return,
        nav_delta_krw=nav_delta_krw,
        cash_delta_krw=cash_delta_krw,
        component_sum_delta_krw=component_sum_delta_krw,
        component_sum_delta_minus_nav_delta_krw=component_sum_delta_minus_nav_delta_krw,
        current_portfolio_value_krw=current_portfolio_value_krw,
        asset_diagnostics=asset_diagnostics_tuple,
    )

    return LocalNavValuationComponentDiagnostic(
        local_nav_valuation_component_diagnostic_policy=(
            LOCAL_NAV_VALUATION_COMPONENT_DIAGNOSTIC_POLICY_V1
        ),
        nav_point_index=nav_point_index,
        previous_step_index=previous_step_index,
        current_step_index=current_step_index,
        previous_as_of=previous_nav_point.as_of,
        current_as_of=current_nav_point.as_of,
        previous_portfolio_value_krw=previous_portfolio_value_krw,
        current_portfolio_value_krw=current_portfolio_value_krw,
        nav_delta_krw=nav_delta_krw,
        period_return=period_return,
        asset_count=len(asset_diagnostics_tuple),
        current_trade_count=current_trade_count,
        previous_cash_krw=previous_cash_krw,
        current_cash_krw=current_cash_krw,
        cash_delta_krw=cash_delta_krw,
        cash_delta_to_nav_delta_ratio=cash_delta_to_nav_delta_ratio,
        component_sum_delta_krw=component_sum_delta_krw,
        component_sum_delta_minus_nav_delta_krw=(
            component_sum_delta_minus_nav_delta_krw
        ),
        largest_positive_component_asset_id=largest_positive_component_asset_id,
        largest_positive_component_delta_to_nav_delta_ratio=(
            largest_positive_component_delta_to_nav_delta_ratio
        ),
        asset_diagnostics=asset_diagnostics_tuple,
        warnings=warnings,
    )


def _collect_valuation_component_asset_ids(
    *,
    previous_step: BacktestSinglePeriodStepResult,
    current_step: BacktestSinglePeriodStepResult,
    current_traded_asset_ids: set[str],
) -> set[str]:
    asset_ids: set[str] = set(current_traded_asset_ids)
    for price_record in previous_step.execution_prices.prices:
        asset_ids.add(price_record.asset_id)
    for price_record in current_step.execution_prices.prices:
        asset_ids.add(price_record.asset_id)
    for holding in previous_step.rebalance_result.post_trade_holdings:
        asset_ids.add(holding.asset_id)
    for holding in current_step.rebalance_result.post_trade_holdings:
        asset_ids.add(holding.asset_id)
    return asset_ids


def _holding_quantity_for_asset(
    holdings: tuple[BacktestHolding, ...],
    asset_id: str,
) -> Decimal:
    for holding in holdings:
        if holding.asset_id == asset_id:
            return holding.quantity
    return _ZERO


def _execution_price_and_market_for_asset(
    step: BacktestSinglePeriodStepResult,
    asset_id: str,
) -> tuple[Decimal | None, str | None]:
    for price_record in step.execution_prices.prices:
        if price_record.asset_id == asset_id:
            return price_record.execution_price, price_record.market
    for trade in step.rebalance_result.trades:
        if trade.asset_id == asset_id:
            return None, trade.market
    return None, None


def _build_valuation_component_asset_diagnostic(
    *,
    asset_id: str,
    previous_step: BacktestSinglePeriodStepResult,
    current_step: BacktestSinglePeriodStepResult,
    previous_usdkrw_rate: Decimal,
    current_usdkrw_rate: Decimal,
    nav_delta_krw: Decimal,
    current_traded_asset_ids: set[str],
) -> LocalNavValuationComponentAssetDiagnostic:
    previous_quantity = _holding_quantity_for_asset(
        previous_step.rebalance_result.post_trade_holdings,
        asset_id,
    )
    current_quantity = _holding_quantity_for_asset(
        current_step.rebalance_result.post_trade_holdings,
        asset_id,
    )
    previous_execution_price, previous_market = _execution_price_and_market_for_asset(
        previous_step,
        asset_id,
    )
    current_execution_price, current_market = _execution_price_and_market_for_asset(
        current_step,
        asset_id,
    )

    market = current_market or previous_market
    if market is None:
        market = "UNKNOWN"

    if previous_execution_price is not None:
        previous_value_krw = _holding_value_krw_for_sanity(
            previous_quantity,
            previous_execution_price,
            market=market,
            usdkrw_rate=previous_usdkrw_rate,
        )
    else:
        previous_value_krw = _ZERO

    if current_execution_price is not None:
        current_value_krw = _holding_value_krw_for_sanity(
            current_quantity,
            current_execution_price,
            market=market,
            usdkrw_rate=current_usdkrw_rate,
        )
    else:
        current_value_krw = _ZERO

    value_delta_krw = current_value_krw - previous_value_krw

    if previous_value_krw == _ZERO:
        value_ratio = None
    else:
        value_ratio = current_value_krw / previous_value_krw

    if nav_delta_krw == _ZERO:
        contribution_to_nav_delta_ratio = None
    else:
        contribution_to_nav_delta_ratio = value_delta_krw / nav_delta_krw

    if (
        previous_execution_price is None
        or current_execution_price is None
        or previous_execution_price == _ZERO
    ):
        execution_price_ratio = None
    else:
        execution_price_ratio = current_execution_price / previous_execution_price

    if market in _FX_MARKETS:
        if previous_usdkrw_rate == _ZERO:
            usdkrw_rate_ratio = None
        else:
            usdkrw_rate_ratio = current_usdkrw_rate / previous_usdkrw_rate
    else:
        usdkrw_rate_ratio = None

    if previous_quantity == _ZERO:
        holding_quantity_ratio = None
    else:
        holding_quantity_ratio = current_quantity / previous_quantity

    was_traded_current_step = asset_id in current_traded_asset_ids
    asset_warnings = _collect_nav_valuation_component_asset_warnings(
        market=market,
        previous_value_krw=previous_value_krw,
        current_value_krw=current_value_krw,
        value_ratio=value_ratio,
        execution_price_ratio=execution_price_ratio,
        holding_quantity_ratio=holding_quantity_ratio,
        was_traded_current_step=was_traded_current_step,
    )

    return LocalNavValuationComponentAssetDiagnostic(
        asset_id=asset_id,
        market=market,
        had_previous_holding=previous_quantity > _ZERO,
        has_current_holding=current_quantity > _ZERO,
        was_traded_current_step=was_traded_current_step,
        previous_value_krw=previous_value_krw,
        current_value_krw=current_value_krw,
        value_delta_krw=value_delta_krw,
        value_ratio=value_ratio,
        contribution_to_nav_delta_ratio=contribution_to_nav_delta_ratio,
        execution_price_ratio=execution_price_ratio,
        usdkrw_rate_ratio=usdkrw_rate_ratio,
        holding_quantity_ratio=holding_quantity_ratio,
        warnings=asset_warnings,
    )


def _collect_nav_valuation_component_asset_warnings(
    *,
    market: str,
    previous_value_krw: Decimal,
    current_value_krw: Decimal,
    value_ratio: Decimal | None,
    execution_price_ratio: Decimal | None,
    holding_quantity_ratio: Decimal | None,
    was_traded_current_step: bool,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if value_ratio is not None and value_ratio > Decimal("10"):
        warnings.append("large_value_ratio")
    if execution_price_ratio is not None and execution_price_ratio > Decimal("10"):
        warnings.append("large_price_ratio")
    if holding_quantity_ratio is not None and holding_quantity_ratio > Decimal("10"):
        warnings.append("large_quantity_ratio")
    if previous_value_krw == _ZERO and current_value_krw > _ZERO:
        warnings.append("new_holding")
    if previous_value_krw > _ZERO and current_value_krw == _ZERO:
        warnings.append("closed_holding")
    if was_traded_current_step:
        warnings.append("traded_current_step")
    if market in _FX_MARKETS:
        warnings.append("fx_market_uses_usdkrw_ratio")
    return tuple(warnings)


def _collect_nav_valuation_component_diagnostic_warnings(
    *,
    period_return: Decimal,
    nav_delta_krw: Decimal,
    cash_delta_krw: Decimal,
    component_sum_delta_krw: Decimal,
    component_sum_delta_minus_nav_delta_krw: Decimal,
    current_portfolio_value_krw: Decimal,
    asset_diagnostics: tuple[LocalNavValuationComponentAssetDiagnostic, ...],
) -> tuple[str, ...]:
    warnings: list[str] = []
    if period_return > Decimal("1.00"):
        warnings.append("positive_nav_spike")
    if _is_material_nav_accounting_delta(
        accounting_delta_krw=component_sum_delta_minus_nav_delta_krw,
        post_trade_portfolio_value_krw=current_portfolio_value_krw,
    ):
        warnings.append("component_sum_delta_does_not_match_nav_delta")
    if nav_delta_krw > _ZERO:
        for asset_diag in asset_diagnostics:
            if (
                asset_diag.value_delta_krw > _ZERO
                and asset_diag.contribution_to_nav_delta_ratio is not None
                and asset_diag.contribution_to_nav_delta_ratio > Decimal("0.80")
            ):
                warnings.append("single_asset_dominates_nav_delta")
                break
        if abs(cash_delta_krw) / abs(nav_delta_krw) > Decimal("0.50"):
            warnings.append("cash_delta_material")
    return tuple(warnings)


def _collect_nav_period_return_diagnostic_warnings(
    *,
    period_return: Decimal,
    abs_period_return: Decimal,
    max_abs_period_return: Decimal,
    previous_cash_weight: Decimal,
    current_cash_weight: Decimal,
    current_max_trade_notional_to_pre_nav_ratio: Decimal | None,
    current_trade_count: int,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if abs_period_return > max_abs_period_return:
        warnings.append("period_return_exceeds_max_abs_period_return")
    if period_return > max_abs_period_return:
        warnings.append("positive_nav_spike")
    if period_return < -max_abs_period_return:
        warnings.append("negative_nav_crash")
    if abs(current_cash_weight - previous_cash_weight) > Decimal("0.50"):
        warnings.append("cash_weight_changed_materially")
    if (
        current_max_trade_notional_to_pre_nav_ratio is not None
        and current_max_trade_notional_to_pre_nav_ratio > Decimal("1.00")
    ):
        warnings.append("large_trade_notional_to_pre_nav_ratio")
    if current_trade_count == 0:
        warnings.append("no_current_trades")
    return tuple(warnings)


def _compute_post_trade_holdings_value_krw_for_sanity(
    step: BacktestSinglePeriodStepResult,
    *,
    usdkrw_rate: Decimal,
    step_index: int,
    raise_on_unsupported_market: bool = True,
) -> Decimal:
    rebalance = step.rebalance_result
    holdings_value_krw = _ZERO
    for price_record in step.execution_prices.prices:
        if price_record.market not in _SUPPORTED_MARKETS:
            if raise_on_unsupported_market:
                raise ValueError(
                    f"steps[{step_index}] has unsupported market for valuation: "
                    f"{price_record.market!r}."
                )
            continue

        quantity = _ZERO
        for holding in rebalance.post_trade_holdings:
            if holding.asset_id == price_record.asset_id:
                quantity = holding.quantity
                break

        holding_value = _holding_value_krw_for_sanity(
            quantity,
            price_record.execution_price,
            market=price_record.market,
            usdkrw_rate=usdkrw_rate,
        )
        holdings_value_krw += holding_value

        if raise_on_unsupported_market:
            if price_record.market in _FX_MARKETS:
                expected = quantity * price_record.execution_price * usdkrw_rate
                if holding_value != expected:
                    raise ValueError(
                        f"steps[{step_index}] USD/GOLD holding value must use period "
                        "USDKRW rate."
                    )
            elif price_record.market == "KR":
                expected = quantity * price_record.execution_price
                if holding_value != expected:
                    raise ValueError(
                        f"steps[{step_index}] KR holding value must not use USDKRW."
                    )

    return holdings_value_krw


def _collect_nav_sanity_step_diagnostic_warnings(
    *,
    rebalance: BacktestRebalanceResult,
    accounting_delta_krw: Decimal,
    post_trade_portfolio_value_krw: Decimal,
    max_trade_notional_to_pre_nav_ratio: Decimal | None,
    markets_seen: tuple[str, ...],
) -> tuple[str, ...]:
    warnings: list[str] = []
    if accounting_delta_krw != _ZERO:
        warnings.append("accounting_delta_nonzero")
        if _is_material_nav_accounting_delta(
            accounting_delta_krw=accounting_delta_krw,
            post_trade_portfolio_value_krw=post_trade_portfolio_value_krw,
        ):
            warnings.append("accounting_delta_material")
            warnings.append("post_trade_value_excludes_or_double_counts_holdings")
            warnings.append("cash_and_holdings_not_equal_nav")
        else:
            warnings.append("accounting_delta_immaterial_decimal_drift")
    if rebalance.cash_krw_after < _ZERO:
        warnings.append("negative_cash")
    if rebalance.cash_krw_after > rebalance.post_trade_portfolio_value_krw:
        warnings.append("cash_above_nav")
    if (
        max_trade_notional_to_pre_nav_ratio is not None
        and max_trade_notional_to_pre_nav_ratio
        > _MAX_TRADE_GROSS_NOTIONAL_NAV_MULTIPLE
    ):
        warnings.append("trade_notional_exceeds_nav_multiple")
    if any(market not in _SUPPORTED_MARKETS for market in markets_seen):
        warnings.append("unsupported_market")
    return tuple(warnings)


def _holding_value_krw_for_sanity(
    quantity: Decimal,
    execution_price: Decimal,
    *,
    market: str,
    usdkrw_rate: Decimal,
) -> Decimal:
    if quantity == _ZERO:
        return _ZERO
    if market == "KR":
        return quantity * execution_price
    if market in _FX_MARKETS:
        return quantity * execution_price * usdkrw_rate
    raise ValueError(f"unsupported market for valuation: {market!r}.")


def run_local_static_neutral_baseline(
    *,
    dataset: LocalMonthlyDatasetAssemblyResult,
    run_config: LocalMonthlyRunConfig,
    aligned_benchmark_points: tuple[BenchmarkReturnPoint, ...],
) -> LocalStaticNeutralBaselineResult:
    """Run deterministic fixed-weight local monthly baseline evidence."""
    if run_config.dataset != dataset:
        raise ValueError("run_config.dataset must equal dataset.")
    if len(aligned_benchmark_points) != len(run_config.period_specs):
        raise ValueError(
            "aligned_benchmark_points length must equal run_config.period_specs length."
        )
    if run_config.cash_min_weight != _STATIC_NEUTRAL_CASH_WEIGHT:
        raise ValueError(
            "static neutral cash weight must equal run_config.cash_min_weight."
        )

    non_cash_configs = _build_static_neutral_snapshot_asset_configs(
        run_config=run_config,
    )
    current_portfolio_state = run_config.initial_portfolio_state
    steps: list[BacktestSinglePeriodStepResult] = []
    nav_points: list[BacktestNavPoint] = []

    for index, period in enumerate(run_config.period_specs):
        decision = _build_static_neutral_single_step_decision(
            run_config=run_config,
            period_index=index,
            snapshot_asset_configs=non_cash_configs,
        )
        execution_prices = select_execution_prices_for_single_step_decision(
            dataset.source_records,
            decision=decision,
        )
        rebalance_result = apply_single_rebalance_accounting(
            decision=decision,
            execution_prices=execution_prices,
            portfolio_state=current_portfolio_state,
            cost_model=run_config.cost_model,
            usdkrw_rate=period.usdkrw_rate,
        )
        next_portfolio_state = BacktestPortfolioState(
            as_of=period.intended_execution_time,
            cash_krw=rebalance_result.cash_krw_after,
            holdings=rebalance_result.post_trade_holdings,
        )
        step = BacktestSinglePeriodStepResult(
            decision_time=period.decision_time,
            intended_execution_time=period.intended_execution_time,
            period_step_policy="single_period_rules_rebalance_step.v1",
            decision=decision,
            execution_prices=execution_prices,
            rebalance_result=rebalance_result,
            next_portfolio_state=next_portfolio_state,
        )
        steps.append(step)
        nav_points.append(
            BacktestNavPoint(
                as_of=period.intended_execution_time,
                portfolio_value_krw=rebalance_result.post_trade_portfolio_value_krw,
                cash_krw=next_portfolio_state.cash_krw,
                total_cost_krw=rebalance_result.total_cost_krw,
            )
        )
        current_portfolio_state = next_portfolio_state

    static_walk_forward_result = BacktestWalkForwardResult(
        walk_forward_policy=WALK_FORWARD_POLICY_V1,
        initial_portfolio_state=run_config.initial_portfolio_state,
        period_specs=run_config.period_specs,
        steps=tuple(steps),
        nav_points=tuple(nav_points),
        final_portfolio_state=current_portfolio_state,
        total_fee_krw=sum(
            (step.rebalance_result.total_fee_krw for step in steps),
            _ZERO,
        ),
        total_tax_krw=sum(
            (step.rebalance_result.total_tax_krw for step in steps),
            _ZERO,
        ),
        total_fx_spread_krw=sum(
            (step.rebalance_result.total_fx_spread_krw for step in steps),
            _ZERO,
        ),
        total_cost_krw=sum(
            (step.rebalance_result.total_cost_krw for step in steps),
            _ZERO,
        ),
    )

    nav_sanity_warnings = validate_local_monthly_walk_forward_nav_sanity(
        run_config=run_config,
        walk_forward_result=static_walk_forward_result,
    )
    static_benchmark_relative_result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=static_walk_forward_result,
        benchmark_points=aligned_benchmark_points,
        periods_per_year=Decimal("12"),
    )

    return LocalStaticNeutralBaselineResult(
        local_static_neutral_baseline_policy=LOCAL_STATIC_NEUTRAL_BASELINE_POLICY_V1,
        walk_forward_result=static_walk_forward_result,
        benchmark_relative_result=static_benchmark_relative_result,
        warnings=(
            *nav_sanity_warnings,
            _STATIC_NEUTRAL_NON_TACTICAL_WARNING,
            _STATIC_NEUTRAL_FIXED_WEIGHTS_WARNING,
            _STATIC_NEUTRAL_RESEARCH_ONLY_WARNING,
        ),
    )


def _build_static_neutral_snapshot_asset_configs(
    *,
    run_config: LocalMonthlyRunConfig,
) -> tuple[SnapshotAssetConfig, ...]:
    asset_configs = tuple(
        SnapshotAssetConfig(
            asset_id=config.asset_id,
            symbol=config.symbol,
            market=config.market,
            long_ma=Decimal("1"),
            risk_on_weight=_static_weight_for_asset(config.asset_id),
            risk_off_weight=_static_weight_for_asset(config.asset_id),
            min_weight=_static_weight_for_asset(config.asset_id),
            max_weight=_static_weight_for_asset(config.asset_id),
        )
        for config in run_config.rolling_asset_configs
    )
    asset_ids = tuple(config.asset_id for config in asset_configs)
    if asset_ids != ("asset_us", "asset_kr", "asset_gold"):
        raise ValueError("static neutral baseline requires US, KR, and GOLD assets.")
    return asset_configs


def _build_static_neutral_single_step_decision(
    *,
    run_config: LocalMonthlyRunConfig,
    period_index: int,
    snapshot_asset_configs: tuple[SnapshotAssetConfig, ...],
) -> BacktestSingleStepDecision:
    period = run_config.period_specs[period_index]
    common_periods = run_config.dataset.common_periods
    current_period_position = run_config.rolling_lookback_count + period_index
    spacing_period_keys = tuple(
        common_periods[current_period_position - 1 : current_period_position + 1]
    )
    feature_snapshot = BacktestFeatureSnapshot(
        decision_time=period.decision_time,
        assets=tuple(
            BacktestAssetFeature(
                asset_id=config.asset_id,
                as_of=period.decision_time,
                current_price=Decimal("1"),
                long_ma=Decimal("1"),
                risk_on_weight=_static_weight_for_asset(config.asset_id),
                risk_off_weight=_static_weight_for_asset(config.asset_id),
                min_weight=_static_weight_for_asset(config.asset_id),
                max_weight=_static_weight_for_asset(config.asset_id),
            )
            for config in snapshot_asset_configs
        ),
        cash_asset_id=run_config.cash_asset_id,
        cash_min_weight=run_config.cash_min_weight,
    )
    target_weights = BacktestTargetWeights(
        decision_time=period.decision_time,
        allocator_version=RULES_ALLOCATOR_V1,
        weights=tuple(
            BacktestTargetWeight(
                asset_id=asset_id,
                weight=weight,
            )
            for asset_id, weight in _static_weights_for_cash_asset(
                run_config.cash_asset_id
            )
        ),
    )
    return BacktestSingleStepDecision(
        decision_time=period.decision_time,
        intended_execution_time=period.intended_execution_time,
        allocator_version=RULES_ALLOCATOR_V1,
        observation_spacing_reports=tuple(
            ObservationSpacingReport(
                asset_id=config.asset_id,
                symbol=config.symbol,
                market=config.market,
                frequency="monthly",
                lookback_count=2,
                period_keys=spacing_period_keys,
            )
            for config in snapshot_asset_configs
        ),
        snapshot_asset_configs=snapshot_asset_configs,
        feature_snapshot=feature_snapshot,
        target_weights=target_weights,
    )


def _static_weights_for_cash_asset(
    cash_asset_id: str,
) -> tuple[tuple[str, Decimal], ...]:
    if cash_asset_id == "cash":
        return LOCAL_STATIC_NEUTRAL_BASELINE_WEIGHTS_V1
    return tuple(
        (cash_asset_id if asset_id == "cash" else asset_id, weight)
        for asset_id, weight in LOCAL_STATIC_NEUTRAL_BASELINE_WEIGHTS_V1
    )


def _static_weight_for_asset(asset_id: str) -> Decimal:
    weights = dict(LOCAL_STATIC_NEUTRAL_BASELINE_WEIGHTS_V1)
    if asset_id not in weights:
        raise ValueError(f"unsupported static neutral asset id: {asset_id!r}.")
    return weights[asset_id]


def run_local_monthly_evaluation_dry_run(
    *,
    repo_root: Path,
    data_root: Path | None = None,
    instrument_specs: Iterable[LocalMonthlyInstrumentSpec] | None = None,
    benchmark_spec: LocalMonthlyBenchmarkSpec | None = None,
    initial_cash_krw: Decimal = Decimal("100000000"),
    cash_asset_id: str = "cash",
    cash_min_weight: Decimal = Decimal("0.05"),
    rolling_lookback_count: int = 3,
    fee_bps: Decimal = Decimal("10"),
    kr_sell_tax_bps: Decimal = Decimal("23"),
    fx_spread_bps: Decimal = Decimal("15"),
    rules_allocator_version: str = LOCAL_RULES_ALLOCATOR_VERSION_V1,
) -> LocalMonthlyEvaluationDryRunResult:
    """Run an in-memory local monthly real-data evaluation dry-run."""
    resolved_repo_root = repo_root.resolve()

    materialized_instrument_specs = (
        default_local_monthly_instrument_specs_for_kospi_primary()
        if instrument_specs is None
        else tuple(instrument_specs)
    )
    materialized_benchmark_spec = (
        default_local_monthly_benchmark_spec()
        if benchmark_spec is None
        else benchmark_spec
    )

    dataset = assemble_local_monthly_dataset(
        repo_root=resolved_repo_root,
        data_root=data_root,
        instrument_specs=materialized_instrument_specs,
        benchmark_spec=materialized_benchmark_spec,
    )

    run_config = build_kospi_primary_monthly_run_config(
        dataset=dataset,
        initial_cash_krw=initial_cash_krw,
        cash_asset_id=cash_asset_id,
        cash_min_weight=cash_min_weight,
        rolling_lookback_count=rolling_lookback_count,
        fee_bps=fee_bps,
        kr_sell_tax_bps=kr_sell_tax_bps,
        fx_spread_bps=fx_spread_bps,
        rules_allocator_version=rules_allocator_version,
    )

    walk_forward_result = run_explicit_schedule_rules_walk_forward_nav(
        dataset.source_records,
        period_specs=run_config.period_specs,
        rolling_asset_configs=run_config.rolling_asset_configs,
        initial_portfolio_state=run_config.initial_portfolio_state,
        cost_model=run_config.cost_model,
        cash_asset_id=run_config.cash_asset_id,
        cash_min_weight=run_config.cash_min_weight,
        rules_allocator_version=run_config.rules_allocator_version,
    )

    nav_sanity_warnings = validate_local_monthly_walk_forward_nav_sanity(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
    )

    aligned_benchmark_points = align_local_monthly_benchmark_points_to_nav_calendar(
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_points=dataset.benchmark_points,
    )

    benchmark_relative_result = compute_walk_forward_benchmark_relative_metrics(
        walk_forward_result=walk_forward_result,
        benchmark_points=aligned_benchmark_points,
        periods_per_year=Decimal("12"),
    )

    static_neutral_baseline_result = run_local_static_neutral_baseline(
        dataset=dataset,
        run_config=run_config,
        aligned_benchmark_points=aligned_benchmark_points,
    )

    report_bundle = render_backtest_evaluation_report_bundle(
        benchmark_relative_result=benchmark_relative_result,
    )

    warnings = _collect_warnings(
        dataset_warnings=dataset.warnings,
        run_config_warnings=run_config.warnings,
        nav_sanity_warnings=nav_sanity_warnings,
        static_neutral_baseline_warnings=static_neutral_baseline_result.warnings,
        rules_allocator_version=run_config.rules_allocator_version,
    )

    return LocalMonthlyEvaluationDryRunResult(
        local_monthly_evaluation_dry_run_policy=(
            LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1
        ),
        dataset=dataset,
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_relative_result=benchmark_relative_result,
        static_neutral_baseline_result=static_neutral_baseline_result,
        report_bundle=report_bundle,
        warnings=warnings,
    )


def _collect_warnings(
    *,
    dataset_warnings: tuple[str, ...],
    run_config_warnings: tuple[str, ...],
    nav_sanity_warnings: tuple[str, ...],
    static_neutral_baseline_warnings: tuple[str, ...],
    rules_allocator_version: str = LOCAL_RULES_ALLOCATOR_VERSION_V1,
) -> tuple[str, ...]:
    combined = list(dataset_warnings)
    combined.extend(run_config_warnings)
    combined.extend(nav_sanity_warnings)
    combined.extend(static_neutral_baseline_warnings)
    if rules_allocator_version == LOCAL_RULES_ALLOCATOR_VERSION_V2:
        combined.append(_RULES_ALLOCATOR_V2_STATIC_NORMAL_WARNING)
    combined.append(_RESEARCH_ONLY_WARNING)
    combined.append(_KOSPI_PROXY_WARNING)
    combined.append(_BENCHMARK_CALENDAR_ALIGNMENT_WARNING)
    combined.append(_BENCHMARK_METRIC_FREQUENCY_WARNING)
    combined.append(_TRACKING_ERROR_LEGACY_FIELD_WARNING)
    return tuple(combined)
