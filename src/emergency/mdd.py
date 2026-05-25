from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from config.settings import ExecutionMode
from domain._datetime import require_timezone_aware_datetime
from domain._decimal import to_decimal
from domain._strings import normalize_required_string
from domain.enums import AccountRole, AssetClass, Market, OrderSide, OrderType
from domain.order import OrderIntent
from emergency.cooldown import MddCooldownEvent, should_suppress_mdd_stage
from emergency.models import (
    MDD_LEVEL_1_THRESHOLD_PERCENT,
    MDD_LEVEL_2_THRESHOLD_PERCENT,
    MDD_LEVEL_3_THRESHOLD_PERCENT,
    EmergencyTriggerSeverity,
    EmergencyTriggerStatus,
    EmergencyTriggerType,
    MddStage,
    TriggerPayload,
    build_cooldown_key,
    mdd_reason_code,
    mdd_stage_for_percent,
    mdd_target_cash_percent,
)

_MDD_STAGE_THRESHOLD: dict[MddStage, Decimal] = {
    MddStage.LEVEL_1: MDD_LEVEL_1_THRESHOLD_PERCENT,
    MddStage.LEVEL_2: MDD_LEVEL_2_THRESHOLD_PERCENT,
    MddStage.LEVEL_3: MDD_LEVEL_3_THRESHOLD_PERCENT,
}


def compute_mdd_percent(*, current_nav: Decimal, historical_peak_nav: Decimal) -> Decimal:
    """현재 NAV와 historical peak NAV에서 MDD 퍼센트를 계산한다."""
    if historical_peak_nav <= Decimal("0"):
        raise ValueError("historical_peak_nav must be greater than 0.")
    if current_nav <= Decimal("0"):
        raise ValueError("current_nav must be greater than 0.")
    return (current_nav - historical_peak_nav) / historical_peak_nav * Decimal("100")


class MddState(BaseModel):
    """MDD killswitch detector/planner 입력 상태."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_nav: Decimal
    historical_peak_nav: Decimal
    mdd_percent: Decimal
    last_triggered_stages: tuple[MddStage, ...] = ()
    detected_at: datetime
    account_role: AccountRole | None = None

    @field_validator("current_nav", "historical_peak_nav", "mdd_percent", mode="before")
    @classmethod
    def validate_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="decimal")

    @field_validator("detected_at", mode="before")
    @classmethod
    def validate_detected_at(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="detected_at")

    @model_validator(mode="after")
    def validate_mdd_consistency(self) -> Any:
        expected = compute_mdd_percent(
            current_nav=self.current_nav,
            historical_peak_nav=self.historical_peak_nav,
        )
        if self.mdd_percent != expected:
            raise ValueError(
                f"mdd_percent {self.mdd_percent} does not match computed {expected}."
            )
        return self


class MddLiquidationTarget(BaseModel):
    """MDD stage별 청산 목표."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: MddStage
    target_cash_percent: Decimal
    halt_required: bool

    @field_validator("target_cash_percent", mode="before")
    @classmethod
    def validate_target(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="target_cash_percent")


class MddLiquidationPosition(BaseModel):
    """MDD 청산 후보 포지션."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    market: Market
    asset_class: AssetClass
    account_role: AccountRole
    quantity: Decimal
    market_value: Decimal
    pnl_vs_cost: Decimal
    is_suspended: bool = False
    is_unfillable: bool = False

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="symbol")

    @field_validator("quantity", "market_value", "pnl_vs_cost", mode="before")
    @classmethod
    def validate_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="decimal")

    @property
    def is_excluded(self) -> bool:
        return self.is_suspended or self.is_unfillable or self.quantity <= Decimal("0")


class MddLiquidationPlan(BaseModel):
    """MDD 청산 계획 (Phase 15: candidate OrderIntent만 생성, broker 미호출)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_payload: TriggerPayload
    target_cash_percent: Decimal
    current_cash_percent: Decimal
    cash_to_raise: Decimal
    candidate_order_intents: tuple[OrderIntent, ...]
    excluded_symbols: tuple[str, ...]
    requires_recovery_review: bool
    below_invested_min: bool
    below_min_reason: str | None
    halt_required: bool = False

    @field_validator(
        "target_cash_percent",
        "current_cash_percent",
        "cash_to_raise",
        mode="before",
    )
    @classmethod
    def validate_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="decimal")


def detect_mdd_killswitch(
    *,
    state: MddState,
    trigger_id: str,
    prior_cooldown_events: tuple[MddCooldownEvent, ...] = (),
) -> TriggerPayload | None:
    """MDD stage 도달 시 MDD_KILLSWITCH trigger를 반환한다. cooldown suppress 시 None."""
    stage = mdd_stage_for_percent(state.mdd_percent)
    if stage is None:
        return None

    cooldown = should_suppress_mdd_stage(
        stage=stage,
        now=state.detected_at,
        prior_events=prior_cooldown_events,
    )
    if cooldown.suppressed:
        return None

    target_cash = mdd_target_cash_percent(stage)
    cooldown_key = build_cooldown_key(
        trigger_type=EmergencyTriggerType.MDD_KILLSWITCH,
        market=None,
        symbol=None,
    )

    severity = {
        MddStage.LEVEL_1: EmergencyTriggerSeverity.HIGH,
        MddStage.LEVEL_2: EmergencyTriggerSeverity.HIGH,
        MddStage.LEVEL_3: EmergencyTriggerSeverity.CRITICAL,
    }[stage]

    return TriggerPayload(
        trigger_id=trigger_id,
        trigger_type=EmergencyTriggerType.MDD_KILLSWITCH,
        detected_at=state.detected_at,
        market=None,
        symbol=None,
        severity=severity,
        status=EmergencyTriggerStatus.DETECTED,
        threshold_percent=_MDD_STAGE_THRESHOLD[stage],
        observed_percent=state.mdd_percent,
        scope_symbols=(),
        account_role=state.account_role,
        execution_mode=ExecutionMode.MDD_KILLSWITCH,
        bypass_llm=True,
        requires_llm_review=False,
        requires_recovery_review=stage != MddStage.LEVEL_3,
        below_invested_min=True,
        below_min_reason="MDD_KILLSWITCH",
        cooldown_key=cooldown_key,
        metadata={
            "mdd_stage": stage.value,
            "target_cash_percent": str(target_cash),
            "historical_peak_nav": str(state.historical_peak_nav),
        },
    )


def build_mdd_liquidation_plan(
    *,
    trigger_payload: TriggerPayload,
    current_cash: Decimal,
    total_nav: Decimal,
    positions: tuple[MddLiquidationPosition, ...],
    correlation_id: str,
) -> MddLiquidationPlan:
    """MDD 청산 계획과 candidate SELL OrderIntent를 deterministic하게 생성한다."""
    if trigger_payload.trigger_type != EmergencyTriggerType.MDD_KILLSWITCH:
        raise ValueError("build_mdd_liquidation_plan requires MDD_KILLSWITCH trigger.")

    stage_value = trigger_payload.metadata.get("mdd_stage")
    if not stage_value:
        raise ValueError("trigger_payload.metadata must include mdd_stage.")
    stage = MddStage(stage_value)

    target_cash_percent = mdd_target_cash_percent(stage)
    if total_nav <= Decimal("0"):
        raise ValueError("total_nav must be greater than 0.")

    current_cash_percent = current_cash / total_nav * Decimal("100")
    target_cash_amount = total_nav * target_cash_percent / Decimal("100")
    cash_to_raise = max(Decimal("0"), target_cash_amount - current_cash)

    excluded = tuple(p.symbol for p in positions if p.is_excluded)
    fillable = [p for p in positions if not p.is_excluded]

    # 손실 포지션 우선 (pnl_vs_cost 오름차순 = 손실 큰 순), tie-break symbol ascending
    losses = sorted(
        [p for p in fillable if p.pnl_vs_cost < Decimal("0")],
        key=lambda p: (p.pnl_vs_cost, p.symbol),
    )
    profits = sorted(
        [p for p in fillable if p.pnl_vs_cost >= Decimal("0")],
        key=lambda p: p.symbol,
    )

    candidate_intents: list[OrderIntent] = []
    raised = Decimal("0")
    order_index = 0

    def _append_sell(position: MddLiquidationPosition, quantity: Decimal) -> None:
        nonlocal order_index, raised
        if quantity <= Decimal("0"):
            return
        order_index += 1
        reason = mdd_reason_code(stage)
        candidate_intents.append(
            OrderIntent(
                order_id=f"mdd-{stage.value.lower()}-{position.symbol}-{order_index}",
                correlation_id=correlation_id,
                symbol=position.symbol,
                market=position.market,
                asset_class=position.asset_class,
                account_role=position.account_role,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                execution_mode=ExecutionMode.MDD_KILLSWITCH,
                quantity=quantity,
                reason_code=reason,
                source_decision_id=trigger_payload.trigger_id,
                created_at=trigger_payload.detected_at,
            )
        )
        unit_value = position.market_value / position.quantity if position.quantity > 0 else Decimal("0")
        raised += quantity * unit_value

    remaining_to_raise = cash_to_raise
    # 1~3% residual mismatch 허용
    tolerance = total_nav * Decimal("0.03")

    for position in losses:
        if remaining_to_raise <= Decimal("0"):
            break
        sell_qty = position.quantity
        _append_sell(position, sell_qty)
        remaining_to_raise = cash_to_raise - raised

    if remaining_to_raise > tolerance and profits:
        total_profit_value = sum(p.market_value for p in profits)
        if total_profit_value > Decimal("0"):
            for position in profits:
                if remaining_to_raise <= tolerance:
                    break
                proportion = position.market_value / total_profit_value
                sell_value = min(remaining_to_raise * proportion, position.market_value)
                if position.quantity <= Decimal("0"):
                    continue
                unit_value = position.market_value / position.quantity
                if unit_value <= Decimal("0"):
                    continue
                sell_qty = min(position.quantity, (sell_value / unit_value).quantize(Decimal("0.0001")))
                _append_sell(position, sell_qty)
                remaining_to_raise = cash_to_raise - raised

    halt_required = stage == MddStage.LEVEL_3

    return MddLiquidationPlan(
        trigger_payload=trigger_payload,
        target_cash_percent=target_cash_percent,
        current_cash_percent=current_cash_percent,
        cash_to_raise=cash_to_raise,
        candidate_order_intents=tuple(candidate_intents),
        excluded_symbols=excluded,
        requires_recovery_review=stage != MddStage.LEVEL_3,
        below_invested_min=True,
        below_min_reason="MDD_KILLSWITCH",
        halt_required=halt_required,
    )
