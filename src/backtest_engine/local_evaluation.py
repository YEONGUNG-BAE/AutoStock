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
from backtest_engine.local_dataset import (
    LocalMonthlyBenchmarkSpec,
    LocalMonthlyDatasetAssemblyResult,
    LocalMonthlyInstrumentSpec,
    assemble_local_monthly_dataset,
    default_local_monthly_benchmark_spec,
    default_local_monthly_instrument_specs_for_kospi_primary,
)
from backtest_engine.local_run_config import (
    LocalMonthlyRunConfig,
    build_kospi_primary_monthly_run_config,
)
from backtest_engine.period_step import BacktestSinglePeriodStepResult
from backtest_engine.rebalance import BacktestRebalanceResult
from backtest_engine.report_bundle import (
    BacktestEvaluationReportBundle,
    render_backtest_evaluation_report_bundle,
)
from backtest_engine.walk_forward import (
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
LOCAL_NAV_SANITY_POLICY_V1 = "local_monthly_walk_forward_nav_sanity.v1"
LOCAL_NAV_SANITY_DIAGNOSTIC_POLICY_V1 = (
    "local_monthly_walk_forward_nav_sanity_diagnostic.v1"
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_FX_MARKETS = frozenset({"US", "GOLD"})
_SUPPORTED_MARKETS = frozenset({"KR", "US", "GOLD"})
# Evidence-quality guard: no single trade may exceed 2x pre-trade NAV notional.
_MAX_TRADE_GROSS_NOTIONAL_NAV_MULTIPLE = Decimal("2.00")

_NAV_SANITY_PASSED_WARNING = (
    "local monthly walk-forward NAV passed deterministic sanity checks"
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
        if recomputed_post_trade != rebalance.post_trade_portfolio_value_krw:
            raise ValueError(
                f"steps[{index}] post-trade holdings value plus cash must equal "
                "post_trade_portfolio_value_krw; run sanitized NAV sanity diagnostic "
                "for this step."
            )

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
                f"nav_points[{index}] period return exceeds max_abs_period_return."
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

    return (_NAV_SANITY_PASSED_WARNING,)


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
    max_trade_notional_to_pre_nav_ratio: Decimal | None,
    markets_seen: tuple[str, ...],
) -> tuple[str, ...]:
    warnings: list[str] = []
    if accounting_delta_krw != _ZERO:
        warnings.append("accounting_delta_nonzero")
        warnings.append("post_trade_value_excludes_or_double_counts_holdings")
        warnings.append("cash_and_holdings_not_equal_nav")
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
    )

    walk_forward_result = run_explicit_schedule_rules_walk_forward_nav(
        dataset.source_records,
        period_specs=run_config.period_specs,
        rolling_asset_configs=run_config.rolling_asset_configs,
        initial_portfolio_state=run_config.initial_portfolio_state,
        cost_model=run_config.cost_model,
        cash_asset_id=run_config.cash_asset_id,
        cash_min_weight=run_config.cash_min_weight,
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
    )

    report_bundle = render_backtest_evaluation_report_bundle(
        benchmark_relative_result=benchmark_relative_result,
    )

    warnings = _collect_warnings(
        dataset_warnings=dataset.warnings,
        run_config_warnings=run_config.warnings,
        nav_sanity_warnings=nav_sanity_warnings,
    )

    return LocalMonthlyEvaluationDryRunResult(
        local_monthly_evaluation_dry_run_policy=(
            LOCAL_MONTHLY_EVALUATION_DRY_RUN_POLICY_V1
        ),
        dataset=dataset,
        run_config=run_config,
        walk_forward_result=walk_forward_result,
        benchmark_relative_result=benchmark_relative_result,
        report_bundle=report_bundle,
        warnings=warnings,
    )


def _collect_warnings(
    *,
    dataset_warnings: tuple[str, ...],
    run_config_warnings: tuple[str, ...],
    nav_sanity_warnings: tuple[str, ...],
) -> tuple[str, ...]:
    combined = list(dataset_warnings)
    combined.extend(run_config_warnings)
    combined.extend(nav_sanity_warnings)
    combined.append(_RESEARCH_ONLY_WARNING)
    combined.append(_KOSPI_PROXY_WARNING)
    combined.append(_BENCHMARK_CALENDAR_ALIGNMENT_WARNING)
    return tuple(combined)
