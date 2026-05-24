from __future__ import annotations

from config.settings import ExecutionMode
from analysis.models import AnalysisAction
from domain.enums import AccountRole, AssetClass, Currency, Market, OrderSide, OrderType
from domain.order import OrderIntent
from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity
from risk.filter import (
    RISK_NO_ACTION,
    RISK_ORDER_GENERATION_FAILED,
    RISK_UNSUPPORTED_ACTION,
    RiskFilter,
    has_blocking_errors,
)
from risk.models import OrderGenerationStatus, RiskFilterInput, RiskMode

# RiskMode → ExecutionMode 매핑
_RISK_TO_EXECUTION_MODE: dict[RiskMode, ExecutionMode] = {
    RiskMode.NORMAL: ExecutionMode.NORMAL,
    RiskMode.REBALANCING: ExecutionMode.REBALANCING,
    RiskMode.EMERGENCY_TRIGGER: ExecutionMode.EMERGENCY_TRIGGER,
    RiskMode.MDD_KILLSWITCH: ExecutionMode.MDD_KILLSWITCH,
}

# market string → Market enum
_MARKET_STRING_MAP: dict[str, Market] = {
    "KR": Market.KR,
    "kr": Market.KR,
    "US": Market.US,
    "us": Market.US,
}

# Market → 기본 currency / asset_class / account_role
_MARKET_DEFAULTS: dict[Market, tuple[Currency, AssetClass, AccountRole]] = {
    Market.KR: (Currency.KRW, AssetClass.KR_EQUITY, AccountRole.PAPER),
    Market.US: (Currency.USD, AssetClass.US_EQUITY, AccountRole.PAPER),
}


class OrderGenerationResult:
    """OrderIntentGenerator 실행 결과."""

    __slots__ = ("status", "order_intent", "validation_result", "correlation_id")

    def __init__(
        self,
        *,
        status: OrderGenerationStatus,
        order_intent: OrderIntent | None,
        validation_result: ValidationResult,
        correlation_id: str | None = None,
    ) -> None:
        self.status = status
        self.order_intent = order_intent
        self.validation_result = validation_result
        self.correlation_id = correlation_id


class OrderIntentGenerator:
    """validated AnalysisDecision을 OrderIntent로 변환한다. PaperBroker는 호출하지 않는다."""

    def __init__(self, risk_filter: RiskFilter | None = None) -> None:
        self._risk_filter = risk_filter or RiskFilter()

    def generate(self, risk_input: RiskFilterInput) -> OrderGenerationResult:
        """RiskFilter 평가 후 OrderIntent를 deterministic하게 생성한다."""
        correlation_id = (
            risk_input.correlation_id or risk_input.analysis_decision.decision_id.value
        )
        risk_result = self._risk_filter.evaluate(risk_input)

        if has_blocking_errors(risk_result):
            return OrderGenerationResult(
                status=OrderGenerationStatus.BLOCKED,
                order_intent=None,
                validation_result=risk_result,
                correlation_id=correlation_id,
            )

        action = risk_input.analysis_decision.fund_manager.action
        if action == AnalysisAction.HOLD:
            return OrderGenerationResult(
                status=OrderGenerationStatus.NOOP,
                order_intent=None,
                validation_result=risk_result,
                correlation_id=correlation_id,
            )

        try:
            order_intent = _build_order_intent(risk_input, correlation_id=correlation_id)
        except _OrderGenerationError as exc:
            blocked_result = _merge_generation_failure(risk_result, exc.message, exc.path)
            return OrderGenerationResult(
                status=OrderGenerationStatus.BLOCKED,
                order_intent=None,
                validation_result=blocked_result,
                correlation_id=correlation_id,
            )

        return OrderGenerationResult(
            status=OrderGenerationStatus.GENERATED,
            order_intent=order_intent,
            validation_result=risk_result,
            correlation_id=correlation_id,
        )


class _OrderGenerationError(Exception):
    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = path


def _build_order_intent(risk_input: RiskFilterInput, *, correlation_id: str) -> OrderIntent:
    analysis = risk_input.analysis_decision
    context = risk_input.context
    action = analysis.fund_manager.action

    if action == AnalysisAction.BUY:
        side = OrderSide.BUY
    elif action == AnalysisAction.SELL:
        side = OrderSide.SELL
    else:
        raise _OrderGenerationError(
            f"Cannot generate OrderIntent for action {action.value!r}.",
            path="analysis_decision.fund_manager.action",
        )

    market = _parse_market(analysis.market)
    currency, asset_class, account_role = _resolve_asset_routing(
        market,
        asset_bucket=risk_input.context.asset_bucket,
    )
    execution_mode = _RISK_TO_EXECUTION_MODE[context.mode]
    order_id = f"order-{analysis.decision_id.value}"

    return OrderIntent(
        order_id=order_id,
        correlation_id=correlation_id,
        symbol=analysis.symbol,
        market=market,
        asset_class=asset_class,
        account_role=account_role,
        side=side,
        order_type=OrderType.MARKET,
        execution_mode=execution_mode,
        target_weight_percent=analysis.fund_manager.target_weight_percent.value,
        limit_price=None,
        reason_code=analysis.summary_one_liner,
        source_decision_id=analysis.decision_id.value,
        created_at=context.created_at,
    )


def _parse_market(market_str: str) -> Market:
    parsed = _MARKET_STRING_MAP.get(market_str)
    if parsed is None:
        raise _OrderGenerationError(
            f"Unsupported market string: {market_str!r}.",
            path="analysis_decision.market",
        )
    return parsed


def _resolve_asset_routing(
    market: Market,
    *,
    asset_bucket,
) -> tuple[Currency, AssetClass, AccountRole]:
    from allocator.models import AssetBucket

    if asset_bucket == AssetBucket.GOLD:
        return Currency.KRW, AssetClass.GOLD, AccountRole.PAPER
    return _MARKET_DEFAULTS[market]


def _merge_generation_failure(
    risk_result: ValidationResult,
    message: str,
    path: str | None,
) -> ValidationResult:
    from risk.rules import RISK_FILTER_SCHEMA, RISK_FILTER_VALIDATOR_VERSION

    extra = ValidationIssue(
        code=RISK_ORDER_GENERATION_FAILED,
        message=message,
        severity=ValidationSeverity.ERROR,
        path=path,
    )
    merged_issues = (*risk_result.issues, extra)
    return ValidationResult(
        passed=False,
        issues=merged_issues,
        schema_name=RISK_FILTER_SCHEMA,
        validator_version=RISK_FILTER_VALIDATOR_VERSION,
    )
