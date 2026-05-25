from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from config.settings import ExecutionMode
from domain._decimal import to_decimal
from domain._strings import normalize_required_string
from domain.enums import AccountRole, Market
from emergency.models import (
    INDEX_CRASH_THRESHOLD_PERCENT,
    PORTFOLIO_LOSS_THRESHOLD_PERCENT,
    PROFIT_RUN_STAGE_1_PERCENT,
    PROFIT_RUN_STAGE_2_PERCENT,
    PROFIT_RUN_STAGE_3_PERCENT,
    STOCK_DROP_THRESHOLD_PERCENT,
    EmergencyTriggerSeverity,
    EmergencyTriggerStatus,
    EmergencyTriggerType,
    TriggerPayload,
    build_cooldown_key,
)


def _intraday_return_percent(*, previous_close: Decimal, current_value: Decimal) -> Decimal:
    """당일 수익률(%)을 계산한다."""
    if previous_close <= Decimal("0"):
        raise ValueError("previous_close must be greater than 0.")
    return (current_value - previous_close) / previous_close * Decimal("100")


class HeldStockSnapshot(BaseModel):
    """STOCK_DROP detector 입력."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    market: Market
    sector: str | None
    account_role: AccountRole
    quantity: Decimal
    previous_close: Decimal
    current_price: Decimal
    market_value: Decimal
    same_sector_symbols: tuple[str, ...] = ()

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="symbol")

    @field_validator("quantity", "previous_close", "current_price", "market_value", mode="before")
    @classmethod
    def validate_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="decimal")


class IndexSnapshot(BaseModel):
    """INDEX_CRASH detector 입력."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: Market
    index_symbol: str
    index_name: str
    previous_close: Decimal
    current_value: Decimal
    affected_holdings: tuple[str, ...]

    @field_validator("index_symbol", "index_name", mode="before")
    @classmethod
    def validate_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("previous_close", "current_value", mode="before")
    @classmethod
    def validate_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="decimal")

    @field_validator("affected_holdings", mode="before")
    @classmethod
    def validate_holdings(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(normalize_required_string(item, field_name="symbol") for item in value)


class LossContributor(BaseModel):
    """PORTFOLIO_LOSS detector 손실 기여 종목."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    loss_contribution: Decimal

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="symbol")

    @field_validator("loss_contribution", mode="before")
    @classmethod
    def validate_loss(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="loss_contribution")


class PortfolioSnapshot(BaseModel):
    """PORTFOLIO_LOSS detector 입력."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    previous_total_nav: Decimal
    current_total_nav: Decimal
    loss_contributors: tuple[LossContributor, ...]
    account_role: AccountRole | None = None

    @field_validator("previous_total_nav", "current_total_nav", mode="before")
    @classmethod
    def validate_nav(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="nav")


class ProfitRunSnapshot(BaseModel):
    """PROFIT_RUN detector 입력."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    market: Market
    account_role: AccountRole
    current_market_weight_percent: Decimal

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="symbol")

    @field_validator("current_market_weight_percent", mode="before")
    @classmethod
    def validate_weight(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="current_market_weight_percent")


def _profit_run_stage(weight: Decimal) -> tuple[Decimal, EmergencyTriggerSeverity, bool, EmergencyTriggerStatus] | None:
    """현재 시장 비중에서 PROFIT_RUN stage 정보를 반환한다."""
    if weight >= PROFIT_RUN_STAGE_3_PERCENT:
        return (
            PROFIT_RUN_STAGE_3_PERCENT,
            EmergencyTriggerSeverity.HIGH,
            True,
            EmergencyTriggerStatus.DETECTED,
        )
    if weight >= PROFIT_RUN_STAGE_2_PERCENT:
        return (
            PROFIT_RUN_STAGE_2_PERCENT,
            EmergencyTriggerSeverity.MEDIUM,
            True,
            EmergencyTriggerStatus.DETECTED,
        )
    if weight >= PROFIT_RUN_STAGE_1_PERCENT:
        return (
            PROFIT_RUN_STAGE_1_PERCENT,
            EmergencyTriggerSeverity.LOW,
            False,
            EmergencyTriggerStatus.NOOP,
        )
    return None


def detect_stock_drop(
    *,
    snapshot: HeldStockSnapshot,
    detected_at: datetime,
    trigger_id: str,
) -> TriggerPayload | None:
    """보유 종목 당일 -3% 이하 하락 시 STOCK_DROP trigger를 반환한다."""
    if snapshot.quantity <= Decimal("0"):
        return None

    observed = _intraday_return_percent(
        previous_close=snapshot.previous_close,
        current_value=snapshot.current_price,
    )
    if observed > STOCK_DROP_THRESHOLD_PERCENT:
        return None

    scope = tuple(dict.fromkeys((snapshot.symbol, *snapshot.same_sector_symbols)))
    cooldown_key = build_cooldown_key(
        trigger_type=EmergencyTriggerType.STOCK_DROP,
        market=snapshot.market,
        symbol=snapshot.symbol,
    )

    return TriggerPayload(
        trigger_id=trigger_id,
        trigger_type=EmergencyTriggerType.STOCK_DROP,
        detected_at=detected_at,
        market=snapshot.market,
        symbol=snapshot.symbol,
        severity=EmergencyTriggerSeverity.HIGH,
        status=EmergencyTriggerStatus.DETECTED,
        threshold_percent=STOCK_DROP_THRESHOLD_PERCENT,
        observed_percent=observed,
        scope_symbols=scope,
        account_role=snapshot.account_role,
        execution_mode=ExecutionMode.EMERGENCY_TRIGGER,
        bypass_llm=False,
        requires_llm_review=True,
        requires_recovery_review=True,
        below_invested_min=False,
        below_min_reason=None,
        cooldown_key=cooldown_key,
        metadata={"sector": snapshot.sector},
    )


def detect_index_crash(
    *,
    snapshot: IndexSnapshot,
    detected_at: datetime,
    trigger_id: str,
    account_role: AccountRole | None = None,
) -> TriggerPayload | None:
    """시장 지수 당일 -1.5% 이하 하락 시 INDEX_CRASH trigger를 반환한다."""
    observed = _intraday_return_percent(
        previous_close=snapshot.previous_close,
        current_value=snapshot.current_value,
    )
    if observed > INDEX_CRASH_THRESHOLD_PERCENT:
        return None

    cooldown_key = build_cooldown_key(
        trigger_type=EmergencyTriggerType.INDEX_CRASH,
        market=snapshot.market,
        symbol=None,
    )

    return TriggerPayload(
        trigger_id=trigger_id,
        trigger_type=EmergencyTriggerType.INDEX_CRASH,
        detected_at=detected_at,
        market=snapshot.market,
        symbol=snapshot.index_symbol,
        severity=EmergencyTriggerSeverity.HIGH,
        status=EmergencyTriggerStatus.DETECTED,
        threshold_percent=INDEX_CRASH_THRESHOLD_PERCENT,
        observed_percent=observed,
        scope_symbols=snapshot.affected_holdings,
        account_role=account_role,
        execution_mode=ExecutionMode.EMERGENCY_TRIGGER,
        bypass_llm=False,
        requires_llm_review=True,
        requires_recovery_review=True,
        below_invested_min=False,
        below_min_reason=None,
        cooldown_key=cooldown_key,
        metadata={"index_name": snapshot.index_name},
    )


def _select_top_loss_contributors(
    contributors: tuple[LossContributor, ...],
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    """손실 기여도 Top N 종목을 symbol 오름차순 tie-break로 선택한다."""
    negatives = [c for c in contributors if c.loss_contribution < Decimal("0")]
    ranked = sorted(
        negatives,
        key=lambda c: (c.loss_contribution, c.symbol),
    )
    return tuple(c.symbol for c in ranked[:limit])


def detect_portfolio_loss(
    *,
    snapshot: PortfolioSnapshot,
    detected_at: datetime,
    trigger_id: str,
) -> TriggerPayload | None:
    """포트폴리오 당일 -2% 이하 하락 시 PORTFOLIO_LOSS trigger를 반환한다."""
    if snapshot.previous_total_nav <= Decimal("0"):
        raise ValueError("previous_total_nav must be greater than 0.")

    observed = (
        (snapshot.current_total_nav - snapshot.previous_total_nav)
        / snapshot.previous_total_nav
        * Decimal("100")
    )
    if observed > PORTFOLIO_LOSS_THRESHOLD_PERCENT:
        return None

    scope = _select_top_loss_contributors(snapshot.loss_contributors)
    cooldown_key = build_cooldown_key(
        trigger_type=EmergencyTriggerType.PORTFOLIO_LOSS,
        market=None,
        symbol=None,
    )

    return TriggerPayload(
        trigger_id=trigger_id,
        trigger_type=EmergencyTriggerType.PORTFOLIO_LOSS,
        detected_at=detected_at,
        market=None,
        symbol=None,
        severity=EmergencyTriggerSeverity.HIGH,
        status=EmergencyTriggerStatus.DETECTED,
        threshold_percent=PORTFOLIO_LOSS_THRESHOLD_PERCENT,
        observed_percent=observed,
        scope_symbols=scope,
        account_role=snapshot.account_role,
        execution_mode=ExecutionMode.EMERGENCY_TRIGGER,
        bypass_llm=False,
        requires_llm_review=True,
        requires_recovery_review=True,
        below_invested_min=False,
        below_min_reason=None,
        cooldown_key=cooldown_key,
        metadata={},
    )


def detect_profit_run(
    *,
    snapshot: ProfitRunSnapshot,
    detected_at: datetime,
    trigger_id: str,
) -> TriggerPayload | None:
    """단일 종목 현재 시장 비중 10/15/20% 도달 시 PROFIT_RUN trigger를 반환한다."""
    stage_info = _profit_run_stage(snapshot.current_market_weight_percent)
    if stage_info is None:
        return None

    threshold, severity, requires_llm, status = stage_info
    cooldown_key = build_cooldown_key(
        trigger_type=EmergencyTriggerType.PROFIT_RUN,
        market=snapshot.market,
        symbol=snapshot.symbol,
    )

    return TriggerPayload(
        trigger_id=trigger_id,
        trigger_type=EmergencyTriggerType.PROFIT_RUN,
        detected_at=detected_at,
        market=snapshot.market,
        symbol=snapshot.symbol,
        severity=severity,
        status=status,
        threshold_percent=threshold,
        observed_percent=snapshot.current_market_weight_percent,
        scope_symbols=(snapshot.symbol,),
        account_role=snapshot.account_role,
        execution_mode=ExecutionMode.EMERGENCY_TRIGGER,
        bypass_llm=False,
        requires_llm_review=requires_llm,
        requires_recovery_review=requires_llm,
        below_invested_min=False,
        below_min_reason=None,
        cooldown_key=cooldown_key,
        metadata={"profit_run_stage_percent": str(threshold)},
    )
