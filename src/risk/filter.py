from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from analysis.models import AnalysisAction
from analysis.rules import is_within_allocator_tolerance, tolerance_band
from allocator.models import AssetBucket
from domain.money import Money
from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity

from risk.models import RiskFilterInput, RiskMode
from risk.rules import (
    RISK_FILTER_SCHEMA,
    RISK_FILTER_VALIDATOR_VERSION,
    SINGLE_POSITION_CAP_PERCENT,
    additional_buy_cost,
    asset_class_soft_band_violations,
    invested_lower_bound_percent,
    is_cash_target_in_band_value,
    mdd_level_from_percent,
    money_cap_from_nav_percent,
    percent_of_money,
    proposed_target_market_value,
    slippage_tolerance_percent,
    CASH_TARGET_MAX,
    CASH_TARGET_MIN,
    GOLD_TRADES_MONTHLY_MAX,
    GOLD_TRADES_QUARTERLY_MAX,
    INVESTED_MAX_PERCENT,
)

# --- issue codes ---
RISK_SINGLE_POSITION_CAP_EXCEEDED = "RISK_SINGLE_POSITION_CAP_EXCEEDED"
RISK_CASH_BAND_VIOLATION = "RISK_CASH_BAND_VIOLATION"
RISK_INVESTED_BAND_VIOLATION = "RISK_INVESTED_BAND_VIOLATION"
RISK_ALLOCATOR_TOLERANCE_VIOLATION = "RISK_ALLOCATOR_TOLERANCE_VIOLATION"
RISK_ASSET_CLASS_SOFT_BAND_WARNING = "RISK_ASSET_CLASS_SOFT_BAND_WARNING"
RISK_MDD_KILLSWITCH_ACTIVE = "RISK_MDD_KILLSWITCH_ACTIVE"
RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED = "RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED"
RISK_GOLD_TRADE_FREQUENCY_EXCEEDED = "RISK_GOLD_TRADE_FREQUENCY_EXCEEDED"
RISK_INSUFFICIENT_CONTEXT = "RISK_INSUFFICIENT_CONTEXT"
RISK_NO_ACTION = "RISK_NO_ACTION"
RISK_UNSUPPORTED_ACTION = "RISK_UNSUPPORTED_ACTION"
RISK_ORDER_GENERATION_FAILED = "RISK_ORDER_GENERATION_FAILED"

_ERROR_ISSUE_ORDER = (
    RISK_UNSUPPORTED_ACTION,
    RISK_INSUFFICIENT_CONTEXT,
    RISK_MDD_KILLSWITCH_ACTIVE,
    RISK_SINGLE_POSITION_CAP_EXCEEDED,
    RISK_CASH_BAND_VIOLATION,
    RISK_INVESTED_BAND_VIOLATION,
    RISK_ALLOCATOR_TOLERANCE_VIOLATION,
    RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED,
    RISK_GOLD_TRADE_FREQUENCY_EXCEEDED,
)

_WARNING_ISSUE_ORDER = (
    RISK_ASSET_CLASS_SOFT_BAND_WARNING,
    RISK_NO_ACTION,
)


class RiskFilter:
    """validated decision + portfolio context에 Python hard filter를 적용한다."""

    def evaluate(self, risk_input: RiskFilterInput) -> ValidationResult:
        """RiskFilterInput에 대해 hard filter + soft band warning을 평가한다."""
        issues: list[ValidationIssue] = []
        action = risk_input.analysis_decision.fund_manager.action
        context = risk_input.context

        issues.extend(_check_action_supported(action))
        if any(issue.code == RISK_UNSUPPORTED_ACTION for issue in issues):
            return _build_result(issues)

        if action == AnalysisAction.HOLD:
            issues.append(
                ValidationIssue(
                    code=RISK_NO_ACTION,
                    message="AnalysisAction.HOLD requires no order intent.",
                    severity=ValidationSeverity.INFO,
                    path="analysis_decision.fund_manager.action",
                )
            )

        issues.extend(_check_mdd_killswitch(context, action))
        issues.extend(_check_mdd_percent_threshold(context))
        issues.extend(_check_single_position_cap(risk_input, action))
        issues.extend(_check_cash_band(risk_input, action))
        issues.extend(_check_invested_band(risk_input, action))
        issues.extend(_check_allocator_tolerance(risk_input))
        issues.extend(_check_directional_slippage(risk_input, action))
        issues.extend(_check_gold_trade_frequency(risk_input, action))
        issues.extend(_check_asset_class_soft_band(context))

        return _build_result(issues)


def has_blocking_errors(result: ValidationResult) -> bool:
    """ERROR severity issue가 있으면 True."""
    return any(issue.severity == ValidationSeverity.ERROR for issue in result.issues)


def _check_action_supported(action: AnalysisAction) -> list[ValidationIssue]:
    if action in {AnalysisAction.BUY, AnalysisAction.SELL, AnalysisAction.HOLD}:
        return []
    return [
        ValidationIssue(
            code=RISK_UNSUPPORTED_ACTION,
            message=f"Unsupported AnalysisAction: {action.value!r}.",
            severity=ValidationSeverity.ERROR,
            path="analysis_decision.fund_manager.action",
        )
    ]


def _check_mdd_killswitch(context, action: AnalysisAction) -> list[ValidationIssue]:
    if context.mode != RiskMode.MDD_KILLSWITCH:
        return []
    issues: list[ValidationIssue] = [
        ValidationIssue(
            code=RISK_MDD_KILLSWITCH_ACTIVE,
            message="MDD_KILLSWITCH mode is active; normal BUY orders are restricted.",
            severity=ValidationSeverity.ERROR if action == AnalysisAction.BUY else ValidationSeverity.WARNING,
            path="context.mode",
        )
    ]
    return issues


def _check_mdd_percent_threshold(context) -> list[ValidationIssue]:
    level = mdd_level_from_percent(context.mdd_percent)
    if level is None:
        return []
    assert context.mdd_percent is not None
    if level == 3:
        severity = ValidationSeverity.ERROR
        message = (
            f"Account MDD drawdown {context.mdd_percent.value}% reached Level 3 threshold "
            f"(target cash 95%)."
        )
    elif level == 2:
        severity = ValidationSeverity.WARNING
        message = (
            f"Account MDD drawdown {context.mdd_percent.value}% reached Level 2 threshold "
            f"(target cash 80%)."
        )
    else:
        severity = ValidationSeverity.WARNING
        message = (
            f"Account MDD drawdown {context.mdd_percent.value}% reached Level 1 threshold "
            f"(target cash 50%)."
        )
    return [
        ValidationIssue(
            code=RISK_MDD_KILLSWITCH_ACTIVE,
            message=message,
            severity=severity,
            path="context.mdd_percent",
        )
    ]


def _check_single_position_cap(risk_input: RiskFilterInput, action: AnalysisAction) -> list[ValidationIssue]:
    if action != AnalysisAction.BUY:
        return []

    context = risk_input.context
    fund_manager = risk_input.analysis_decision.fund_manager
    if (
        context.current_symbol_market_value is None
        or context.current_symbol_cumulative_buy_cost is None
    ):
        return [
            ValidationIssue(
                code=RISK_INSUFFICIENT_CONTEXT,
                message=(
                    "Single position cap check requires "
                    "current_symbol_market_value and current_symbol_cumulative_buy_cost."
                ),
                severity=ValidationSeverity.ERROR,
                path="context.current_symbol_market_value",
            )
        ]

    additional = additional_buy_cost(
        context.total_nav,
        fund_manager.target_weight_percent,
        context.current_symbol_market_value,
    )
    cumulative_after = Money(
        amount=context.current_symbol_cumulative_buy_cost.amount + additional.amount,
        currency=context.total_nav.currency,
    )
    cap = money_cap_from_nav_percent(context.total_nav, SINGLE_POSITION_CAP_PERCENT)
    if cumulative_after.amount > cap.amount:
        return [
            ValidationIssue(
                code=RISK_SINGLE_POSITION_CAP_EXCEEDED,
                message=(
                    "Single position cumulative buy cost would exceed 5% NAV cap: "
                    f"cumulative_after={cumulative_after.amount}, cap={cap.amount}."
                ),
                severity=ValidationSeverity.ERROR,
                path="context.current_symbol_cumulative_buy_cost",
            )
        ]
    return []


def _check_cash_band(risk_input: RiskFilterInput, action: AnalysisAction) -> list[ValidationIssue]:
    context = risk_input.context
    allocator = risk_input.allocator_decision
    issues: list[ValidationIssue] = []

    cash_target = allocator.cash_policy.cash_target_percent.value
    if not is_cash_target_in_band_value(cash_target):
        issues.append(
            ValidationIssue(
                code=RISK_CASH_BAND_VIOLATION,
                message=(
                    f"Allocator cash_target_percent {cash_target} is outside "
                    f"{CASH_TARGET_MIN}~{CASH_TARGET_MAX} band."
                ),
                severity=ValidationSeverity.ERROR,
                path="allocator_decision.cash_policy.cash_target_percent",
            )
        )

    current_cash_percent = percent_of_money(context.cash, context.total_nav)

    if action == AnalysisAction.BUY:
        projected_cash_percent = _projected_cash_percent_after_buy(risk_input)
        if projected_cash_percent is not None and projected_cash_percent < CASH_TARGET_MIN:
            issues.append(
                ValidationIssue(
                    code=RISK_CASH_BAND_VIOLATION,
                    message=(
                        f"Estimated post-BUY cash percent {projected_cash_percent} "
                        f"would fall below minimum {CASH_TARGET_MIN}."
                    ),
                    severity=ValidationSeverity.ERROR,
                    path="context.cash",
                )
            )
    elif action == AnalysisAction.SELL:
        projected_cash_percent = _projected_cash_percent_after_sell(risk_input)
        if projected_cash_percent is not None and projected_cash_percent > CASH_TARGET_MAX:
            issues.append(
                ValidationIssue(
                    code=RISK_CASH_BAND_VIOLATION,
                    message=(
                        f"Estimated post-SELL cash percent {projected_cash_percent} "
                        f"would exceed maximum {CASH_TARGET_MAX}."
                    ),
                    severity=ValidationSeverity.WARNING,
                    path="context.cash",
                )
            )
    elif current_cash_percent < CASH_TARGET_MIN or current_cash_percent > CASH_TARGET_MAX:
        # HOLD 등 — 현재 cash band만 방어적 재검증
        if current_cash_percent < CASH_TARGET_MIN or current_cash_percent > CASH_TARGET_MAX:
            issues.append(
                ValidationIssue(
                    code=RISK_CASH_BAND_VIOLATION,
                    message=(
                        f"Current cash percent {current_cash_percent} is outside "
                        f"{CASH_TARGET_MIN}~{CASH_TARGET_MAX} band."
                    ),
                    severity=ValidationSeverity.ERROR,
                    path="context.cash",
                )
            )

    return issues


def _projected_cash_percent_after_buy(risk_input: RiskFilterInput) -> Decimal | None:
    context = risk_input.context
    if context.current_symbol_market_value is None:
        return None
    additional = additional_buy_cost(
        context.total_nav,
        risk_input.analysis_decision.fund_manager.target_weight_percent,
        context.current_symbol_market_value,
    )
    projected_cash = context.cash.amount - additional.amount
    projected = Money(amount=projected_cash, currency=context.total_nav.currency)
    return percent_of_money(projected, context.total_nav)


def _projected_cash_percent_after_sell(risk_input: RiskFilterInput) -> Decimal | None:
    context = risk_input.context
    if context.current_symbol_market_value is None:
        return None
    proposed = proposed_target_market_value(
        context.total_nav,
        risk_input.analysis_decision.fund_manager.target_weight_percent,
    )
    sell_proceeds = context.current_symbol_market_value.amount - proposed.amount
    if sell_proceeds <= 0:
        return percent_of_money(context.cash, context.total_nav)
    projected_cash = context.cash.amount + sell_proceeds
    projected = Money(amount=projected_cash, currency=context.total_nav.currency)
    return percent_of_money(projected, context.total_nav)


def _check_invested_band(risk_input: RiskFilterInput, action: AnalysisAction) -> list[ValidationIssue]:
    context = risk_input.context
    current_invested_percent = percent_of_money(context.invested_amount, context.total_nav)
    lower_bound = invested_lower_bound_percent(
        context.mode,
        context.paper_observation_min_invested_percent,
    )
    issues: list[ValidationIssue] = []

    if current_invested_percent > INVESTED_MAX_PERCENT:
        issues.append(
            ValidationIssue(
                code=RISK_INVESTED_BAND_VIOLATION,
                message=(
                    f"Invested percent {current_invested_percent} exceeds maximum "
                    f"{INVESTED_MAX_PERCENT}."
                ),
                severity=ValidationSeverity.ERROR,
                path="context.invested_amount",
            )
        )

    below_lower = current_invested_percent < lower_bound
    if below_lower:
        severity = (
            ValidationSeverity.WARNING
            if context.mode
            in {RiskMode.REBALANCING, RiskMode.EMERGENCY_TRIGGER, RiskMode.MDD_KILLSWITCH}
            else ValidationSeverity.ERROR
        )
        issues.append(
            ValidationIssue(
                code=RISK_INVESTED_BAND_VIOLATION,
                message=(
                    f"Invested percent {current_invested_percent} is below minimum "
                    f"{lower_bound} for mode {context.mode.value}."
                ),
                severity=severity,
                path="context.invested_amount",
            )
        )

    if action == AnalysisAction.SELL and context.mode == RiskMode.NORMAL:
        projected = _projected_invested_percent_after_sell(risk_input)
        if projected is not None and projected < lower_bound:
            issues.append(
                ValidationIssue(
                    code=RISK_INVESTED_BAND_VIOLATION,
                    message=(
                        f"Estimated post-SELL invested percent {projected} would fall below "
                        f"minimum {lower_bound} in NORMAL mode."
                    ),
                    severity=ValidationSeverity.ERROR,
                    path="context.invested_amount",
                )
            )

    return issues


def _projected_invested_percent_after_sell(risk_input: RiskFilterInput) -> Decimal | None:
    context = risk_input.context
    if context.current_symbol_market_value is None:
        return None
    proposed = proposed_target_market_value(
        context.total_nav,
        risk_input.analysis_decision.fund_manager.target_weight_percent,
    )
    reduction = context.current_symbol_market_value.amount - proposed.amount
    if reduction <= 0:
        return percent_of_money(context.invested_amount, context.total_nav)
    projected_invested = context.invested_amount.amount - reduction
    projected = Money(amount=projected_invested, currency=context.total_nav.currency)
    return percent_of_money(projected, context.total_nav)


def _check_allocator_tolerance(risk_input: RiskFilterInput) -> list[ValidationIssue]:
    context = risk_input.context
    if context.allocator_symbol_target_weight is None:
        return []

    target = risk_input.analysis_decision.fund_manager.target_weight_percent
    allocator_target = context.allocator_symbol_target_weight
    tolerance = context.allocator_tolerance_percent
    if is_within_allocator_tolerance(target, allocator_target, tolerance):
        return []

    lower, upper = tolerance_band(allocator_target, tolerance)
    return [
        ValidationIssue(
            code=RISK_ALLOCATOR_TOLERANCE_VIOLATION,
            message=(
                "fund_manager.target_weight_percent must be within "
                f"allocator_symbol_target_weight ± tolerance_percent: "
                f"target={target.value}, allocator_target={allocator_target.value}, "
                f"tolerance={tolerance.value}, allowed_band={lower}~{upper}"
            ),
            severity=ValidationSeverity.ERROR,
            path="analysis_decision.fund_manager.target_weight_percent",
        )
    ]


def _check_directional_slippage(risk_input: RiskFilterInput, action: AnalysisAction) -> list[ValidationIssue]:
    if action == AnalysisAction.HOLD:
        return []
    if risk_input.context.mode == RiskMode.MDD_KILLSWITCH:
        return []

    context = risk_input.context
    symbol = risk_input.analysis_decision.symbol
    if context.proposed_price is None or context.reference_prices is None:
        return []
    reference = context.reference_prices.get(symbol)
    if reference is None:
        return []

    proposed = context.proposed_price
    if proposed.currency != reference.currency:
        return [
            ValidationIssue(
                code=RISK_INSUFFICIENT_CONTEXT,
                message=(
                    "Directional slippage requires proposed_price currency to match "
                    f"reference quote currency: proposed={proposed.currency.value}, "
                    f"reference={reference.currency.value}."
                ),
                severity=ValidationSeverity.ERROR,
                path="context.proposed_price",
            )
        ]

    tolerance = slippage_tolerance_percent(reference.market)
    reference_amount = reference.price
    proposed_amount = proposed.amount
    tolerance_factor = tolerance / Decimal("100")

    if action == AnalysisAction.BUY:
        max_allowed = reference_amount * (Decimal("1") + tolerance_factor)
        if proposed_amount > max_allowed:
            return [
                ValidationIssue(
                    code=RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED,
                    message=(
                        f"BUY proposed_price {proposed_amount} exceeds reference "
                        f"{reference_amount} + {tolerance}% tolerance."
                    ),
                    severity=ValidationSeverity.ERROR,
                    path="context.proposed_price",
                )
            ]
    elif action == AnalysisAction.SELL:
        # SELL: 하방 이동 허용, reference 대비 rebound(상승)만 block
        max_allowed = reference_amount * (Decimal("1") + tolerance_factor)
        if proposed_amount > max_allowed:
            return [
                ValidationIssue(
                    code=RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED,
                    message=(
                        f"SELL proposed_price {proposed_amount} rebounded above reference "
                        f"{reference_amount} + {tolerance}% tolerance."
                    ),
                    severity=ValidationSeverity.ERROR,
                    path="context.proposed_price",
                )
            ]

    return []


def _check_gold_trade_frequency(risk_input: RiskFilterInput, action: AnalysisAction) -> list[ValidationIssue]:
    if action == AnalysisAction.HOLD:
        return []
    context = risk_input.context
    if context.asset_bucket != AssetBucket.GOLD:
        return []

    issues: list[ValidationIssue] = []
    if context.gold_trades_this_month >= GOLD_TRADES_MONTHLY_MAX:
        issues.append(
            ValidationIssue(
                code=RISK_GOLD_TRADE_FREQUENCY_EXCEEDED,
                message=(
                    f"Gold trades this month ({context.gold_trades_this_month}) "
                    f"reached monthly limit ({GOLD_TRADES_MONTHLY_MAX})."
                ),
                severity=ValidationSeverity.ERROR,
                path="context.gold_trades_this_month",
            )
        )
    if context.gold_trades_this_quarter >= GOLD_TRADES_QUARTERLY_MAX:
        issues.append(
            ValidationIssue(
                code=RISK_GOLD_TRADE_FREQUENCY_EXCEEDED,
                message=(
                    f"Gold trades this quarter ({context.gold_trades_this_quarter}) "
                    f"reached quarterly limit ({GOLD_TRADES_QUARTERLY_MAX})."
                ),
                severity=ValidationSeverity.ERROR,
                path="context.gold_trades_this_quarter",
            )
        )
    return issues


def _check_asset_class_soft_band(context) -> list[ValidationIssue]:
    if context.current_asset_weights is None:
        return []
    violations = asset_class_soft_band_violations(context.current_asset_weights)
    if not violations:
        return []
    return [
        ValidationIssue(
            code=RISK_ASSET_CLASS_SOFT_BAND_WARNING,
            message=(
                "Asset class weights outside 15~55 soft band: "
                + ", ".join(sorted(violations))
                + "."
            ),
            severity=ValidationSeverity.WARNING,
            path="context.current_asset_weights",
        )
    ]


def _sort_issues(issues: Iterable[ValidationIssue]) -> tuple[ValidationIssue, ...]:
    error_order = {code: index for index, code in enumerate(_ERROR_ISSUE_ORDER)}
    warning_order = {code: index for index, code in enumerate(_WARNING_ISSUE_ORDER)}

    def sort_key(issue: ValidationIssue) -> tuple[int, int, str, str, str]:
        if issue.severity == ValidationSeverity.ERROR:
            group = 0
            order_map = error_order
        elif issue.severity == ValidationSeverity.WARNING:
            group = 1
            order_map = warning_order
        else:
            group = 2
            order_map = warning_order
        return (
            group,
            order_map.get(issue.code, len(order_map)),
            issue.code,
            issue.path or "",
            issue.message,
        )

    return tuple(sorted(issues, key=sort_key))


def _build_result(issues: list[ValidationIssue]) -> ValidationResult:
    ordered = _sort_issues(issues)
    if not ordered:
        return ValidationResult(
            passed=True,
            issues=(),
            schema_name=RISK_FILTER_SCHEMA,
            validator_version=RISK_FILTER_VALIDATOR_VERSION,
        )
    has_error = any(issue.severity == ValidationSeverity.ERROR for issue in ordered)
    return ValidationResult(
        passed=not has_error,
        issues=ordered,
        schema_name=RISK_FILTER_SCHEMA,
        validator_version=RISK_FILTER_VALIDATOR_VERSION,
    )
