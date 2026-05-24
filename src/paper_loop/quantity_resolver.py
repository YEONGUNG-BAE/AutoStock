from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from domain.enums import OrderSide, OrderType
from domain.market import MarketPrice
from domain.order import OrderIntent
from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity
from risk.models import RiskFilterContext
from risk.rules import RISK_FILTER_SCHEMA, RISK_FILTER_VALIDATOR_VERSION

from paper_loop.models import (
    PAPER_LOOP_INVALID_TARGET_WEIGHT_INTENT,
    PAPER_LOOP_NO_EXECUTABLE_QUANTITY,
    PAPER_LOOP_QUANTITY_CONTEXT_MISSING,
    PAPER_LOOP_QUANTITY_CURRENCY_MISMATCH,
    PAPER_LOOP_QUANTITY_RESOLVED,
    PAPER_LOOP_UNSUPPORTED_ORDER_TYPE,
    QuantityResolutionResult,
    QuantityResolutionStatus,
)


class QuantityResolver:
    """target_weight_percent 기반 OrderIntent를 quantity 기반으로 변환한다."""

    def resolve(
        self,
        *,
        intent: OrderIntent,
        context: RiskFilterContext,
        market_price: MarketPrice,
        current_position_quantity: Decimal | None = None,
    ) -> QuantityResolutionResult:
        """Phase 10 target_weight_percent intent → PaperBroker 실행 가능 quantity intent."""
        base_result = ValidationResult(
            passed=True,
            issues=(),
            schema_name=RISK_FILTER_SCHEMA,
            validator_version=RISK_FILTER_VALIDATOR_VERSION,
        )

        if intent.order_type != OrderType.MARKET:
            return _failed(
                base_result,
                code=PAPER_LOOP_UNSUPPORTED_ORDER_TYPE,
                message=f"Phase 11 MVP supports MARKET orders only, got {intent.order_type.value}.",
                path="order_intent.order_type",
            )

        if intent.quantity is not None:
            return _failed(
                base_result,
                code=PAPER_LOOP_INVALID_TARGET_WEIGHT_INTENT,
                message="Input OrderIntent must not include quantity; target_weight_percent is required.",
                path="order_intent.quantity",
            )

        if intent.target_weight_percent is None:
            return _failed(
                base_result,
                code=PAPER_LOOP_INVALID_TARGET_WEIGHT_INTENT,
                message="Input OrderIntent requires target_weight_percent.",
                path="order_intent.target_weight_percent",
            )

        if context.total_nav.currency != market_price.currency:
            return _failed(
                base_result,
                code=PAPER_LOOP_QUANTITY_CURRENCY_MISMATCH,
                message=(
                    "Quantity conversion requires total_nav.currency to match market_price.currency: "
                    f"nav={context.total_nav.currency.value}, "
                    f"quote={market_price.currency.value}."
                ),
                path="context.total_nav.currency",
            )

        current_value = _resolve_current_value(
            context=context,
            market_price=market_price,
            current_position_quantity=current_position_quantity,
        )
        if current_value is None:
            return _failed(
                base_result,
                code=PAPER_LOOP_QUANTITY_CONTEXT_MISSING,
                message=(
                    "Quantity conversion requires current_symbol_market_value "
                    "or current_position_quantity."
                ),
                path="context.current_symbol_market_value",
            )

        target_value = (
            context.total_nav.amount * intent.target_weight_percent / Decimal("100")
        )

        if intent.side == OrderSide.BUY:
            value_delta = max(Decimal("0"), target_value - current_value)
        elif intent.side == OrderSide.SELL:
            value_delta = max(Decimal("0"), current_value - target_value)
        else:
            return _failed(
                base_result,
                code=PAPER_LOOP_INVALID_TARGET_WEIGHT_INTENT,
                message=f"Unsupported order side for quantity resolution: {intent.side.value}.",
                path="order_intent.side",
            )

        raw_quantity = value_delta / market_price.price
        quantity = _floor_toward_zero(raw_quantity)

        if quantity <= Decimal("0"):
            noop_result = _append_issue(
                base_result,
                ValidationIssue(
                    code=PAPER_LOOP_NO_EXECUTABLE_QUANTITY,
                    message="Computed quantity is zero; no executable order.",
                    severity=ValidationSeverity.INFO,
                    path="order_intent.quantity",
                ),
            )
            return QuantityResolutionResult(
                status=QuantityResolutionStatus.NOOP,
                order_intent=None,
                validation_result=noop_result,
            )

        executable = OrderIntent(
            order_id=intent.order_id,
            correlation_id=intent.correlation_id,
            symbol=intent.symbol,
            market=intent.market,
            asset_class=intent.asset_class,
            account_role=intent.account_role,
            side=intent.side,
            order_type=intent.order_type,
            execution_mode=intent.execution_mode,
            time_in_force=intent.time_in_force,
            quantity=quantity,
            target_weight_percent=None,
            limit_price=None,
            reason_code=intent.reason_code,
            source_decision_id=intent.source_decision_id,
            created_at=intent.created_at,
        )

        resolved_result = _append_issue(
            base_result,
            ValidationIssue(
                code=PAPER_LOOP_QUANTITY_RESOLVED,
                message=f"Resolved executable quantity={quantity}.",
                severity=ValidationSeverity.INFO,
                path="order_intent.quantity",
            ),
        )
        return QuantityResolutionResult(
            status=QuantityResolutionStatus.RESOLVED,
            order_intent=executable,
            validation_result=resolved_result,
        )


def _resolve_current_value(
    *,
    context: RiskFilterContext,
    market_price: MarketPrice,
    current_position_quantity: Decimal | None,
) -> Decimal | None:
    if current_position_quantity is not None:
        return current_position_quantity * market_price.price
    if context.current_symbol_market_value is None:
        return None
    return context.current_symbol_market_value.amount


def _floor_toward_zero(value: Decimal) -> Decimal:
    """Phase 11 MVP: 정수 주식만 지원, 0 방향 floor."""
    if value >= Decimal("0"):
        return value.to_integral_value(rounding=ROUND_DOWN)
    return -((-value).to_integral_value(rounding=ROUND_DOWN))


def _append_issue(
    base: ValidationResult,
    issue: ValidationIssue,
) -> ValidationResult:
    merged = (*base.issues, issue)
    has_error = any(i.severity == ValidationSeverity.ERROR for i in merged)
    return ValidationResult(
        passed=not has_error,
        issues=merged,
        schema_name=base.schema_name,
        validator_version=base.validator_version,
    )


def _failed(
    base: ValidationResult,
    *,
    code: str,
    message: str,
    path: str | None,
) -> QuantityResolutionResult:
    failed_result = _append_issue(
        base,
        ValidationIssue(
            code=code,
            message=message,
            severity=ValidationSeverity.ERROR,
            path=path,
        ),
    )
    return QuantityResolutionResult(
        status=QuantityResolutionStatus.FAILED,
        order_intent=None,
        validation_result=failed_result,
    )
