from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from config.settings import ExecutionMode
from decision.canonical_json import canonicalize_payload, payload_sha256
from domain._datetime import require_timezone_aware_datetime
from domain._decimal import to_decimal
from domain._strings import normalize_required_string
from domain.enums import AccountRole, Market

# --- 기본 임계값 (Phase 15 module constants) ---

STOCK_DROP_THRESHOLD_PERCENT = Decimal("-3")
INDEX_CRASH_THRESHOLD_PERCENT = Decimal("-1.5")
PORTFOLIO_LOSS_THRESHOLD_PERCENT = Decimal("-2")
PROFIT_RUN_STAGE_1_PERCENT = Decimal("10")
PROFIT_RUN_STAGE_2_PERCENT = Decimal("15")
PROFIT_RUN_STAGE_3_PERCENT = Decimal("20")
MDD_LEVEL_1_THRESHOLD_PERCENT = Decimal("-10")
MDD_LEVEL_2_THRESHOLD_PERCENT = Decimal("-15")
MDD_LEVEL_3_THRESHOLD_PERCENT = Decimal("-20")
MDD_LEVEL_1_TARGET_CASH_PERCENT = Decimal("50")
MDD_LEVEL_2_TARGET_CASH_PERCENT = Decimal("80")
MDD_LEVEL_3_TARGET_CASH_PERCENT = Decimal("95")
MDD_LEVEL_1_TO_2_COOLDOWN = timedelta(hours=4)

# 일반 emergency trigger 기본 cooldown (분)
DEFAULT_EMERGENCY_COOLDOWN_MINUTES = 60


class EmergencyTriggerType(StrEnum):
    STOCK_DROP = "STOCK_DROP"
    INDEX_CRASH = "INDEX_CRASH"
    PORTFOLIO_LOSS = "PORTFOLIO_LOSS"
    PROFIT_RUN = "PROFIT_RUN"
    MDD_KILLSWITCH = "MDD_KILLSWITCH"


class EmergencyTriggerSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EmergencyTriggerStatus(StrEnum):
    DETECTED = "DETECTED"
    SUPPRESSED_BY_COOLDOWN = "SUPPRESSED_BY_COOLDOWN"
    PLANNED = "PLANNED"
    NOOP = "NOOP"


class MddStage(StrEnum):
    LEVEL_1 = "LEVEL_1"  # -10%, target cash 50%
    LEVEL_2 = "LEVEL_2"  # -15%, target cash 80%
    LEVEL_3 = "LEVEL_3"  # -20%, target cash 95%


# 우선순위: 숫자가 작을수록 높은 우선순위
_TRIGGER_PRIORITY_RANK: dict[EmergencyTriggerType, int] = {
    EmergencyTriggerType.MDD_KILLSWITCH: 0,
    EmergencyTriggerType.PORTFOLIO_LOSS: 1,
    EmergencyTriggerType.INDEX_CRASH: 2,
    EmergencyTriggerType.STOCK_DROP: 3,
    EmergencyTriggerType.PROFIT_RUN: 4,
}


def trigger_priority_rank(trigger_type: EmergencyTriggerType) -> int:
    """트리거 타입의 우선순위 rank를 반환한다. 낮을수록 높은 우선순위."""
    return _TRIGGER_PRIORITY_RANK[trigger_type]


def sort_triggers_by_priority(
    payloads: tuple[TriggerPayload, ...] | list[TriggerPayload],
) -> tuple[TriggerPayload, ...]:
    """우선순위 규칙에 따라 TriggerPayload를 deterministic하게 정렬한다."""

    def sort_key(payload: TriggerPayload) -> tuple[Any, ...]:
        market_key = payload.market.value if payload.market is not None else ""
        symbol_key = payload.symbol or ""
        return (
            trigger_priority_rank(payload.trigger_type),
            market_key,
            symbol_key,
            payload.detected_at.isoformat(),
            payload.trigger_id,
        )

    return tuple(sorted(payloads, key=sort_key))


def build_cooldown_key(
    *,
    trigger_type: EmergencyTriggerType,
    market: Market | None,
    symbol: str | None,
) -> str:
    """per-trigger / per-market / per-symbol cooldown key를 생성한다."""
    market_part = market.value if market is not None else "*"
    symbol_part = symbol if symbol is not None else "*"
    return f"{trigger_type.value}:{market_part}:{symbol_part}"


def mdd_stage_for_percent(mdd_percent: Decimal) -> MddStage | None:
    """MDD 퍼센트에서 해당 stage를 반환한다. 임계값 미달 시 None."""
    if mdd_percent <= MDD_LEVEL_3_THRESHOLD_PERCENT:
        return MddStage.LEVEL_3
    if mdd_percent <= MDD_LEVEL_2_THRESHOLD_PERCENT:
        return MddStage.LEVEL_2
    if mdd_percent <= MDD_LEVEL_1_THRESHOLD_PERCENT:
        return MddStage.LEVEL_1
    return None


def mdd_target_cash_percent(stage: MddStage) -> Decimal:
    """MDD stage별 목표 현금 비중을 반환한다."""
    mapping = {
        MddStage.LEVEL_1: MDD_LEVEL_1_TARGET_CASH_PERCENT,
        MddStage.LEVEL_2: MDD_LEVEL_2_TARGET_CASH_PERCENT,
        MddStage.LEVEL_3: MDD_LEVEL_3_TARGET_CASH_PERCENT,
    }
    return mapping[stage]


def mdd_reason_code(stage: MddStage) -> str:
    """MDD stage별 reason_code / debug event code 문자열."""
    mapping = {
        MddStage.LEVEL_1: "MDD_LEVEL_1_TRIGGERED",
        MddStage.LEVEL_2: "MDD_LEVEL_2_TRIGGERED",
        MddStage.LEVEL_3: "MDD_LEVEL_3_TRIGGERED",
    }
    return mapping[stage]


class TriggerPayload(BaseModel):
    """Emergency trigger 감지 결과 payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_id: str
    trigger_type: EmergencyTriggerType
    detected_at: datetime
    market: Market | None
    symbol: str | None
    severity: EmergencyTriggerSeverity
    status: EmergencyTriggerStatus
    threshold_percent: Decimal
    observed_percent: Decimal
    scope_symbols: tuple[str, ...]
    account_role: AccountRole | None
    execution_mode: ExecutionMode
    bypass_llm: bool
    requires_llm_review: bool
    requires_recovery_review: bool
    below_invested_min: bool
    below_min_reason: str | None
    cooldown_key: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("trigger_id", "cooldown_key", mode="before")
    @classmethod
    def validate_required_ids(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_optional_symbol(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name="symbol")

    @field_validator("scope_symbols", mode="before")
    @classmethod
    def validate_scope_symbols(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("scope_symbols must be a list or tuple.")
        normalized: list[str] = []
        for item in value:
            normalized.append(normalize_required_string(item, field_name="scope_symbols"))
        return tuple(normalized)

    @field_validator("threshold_percent", "observed_percent", mode="before")
    @classmethod
    def validate_percent_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="percent")

    @field_validator("detected_at", mode="before")
    @classmethod
    def validate_detected_at(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return require_timezone_aware_datetime(value, field_name="detected_at")

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value

    @model_validator(mode="after")
    def validate_trigger_payload(self) -> Self:
        expected_metadata = canonicalize_payload(self.metadata)
        if self.metadata != expected_metadata:
            raise ValueError("metadata must be in canonical JSON-compatible form.")

        if self.trigger_type == EmergencyTriggerType.MDD_KILLSWITCH:
            if self.execution_mode != ExecutionMode.MDD_KILLSWITCH:
                raise ValueError("MDD_KILLSWITCH requires execution_mode=MDD_KILLSWITCH.")
            if not self.bypass_llm:
                raise ValueError("MDD_KILLSWITCH requires bypass_llm=True.")
            if self.requires_llm_review:
                raise ValueError("MDD_KILLSWITCH requires requires_llm_review=False.")
        else:
            if self.execution_mode != ExecutionMode.EMERGENCY_TRIGGER:
                raise ValueError(
                    f"{self.trigger_type.value} requires execution_mode=EMERGENCY_TRIGGER."
                )
            if self.bypass_llm:
                raise ValueError(
                    f"{self.trigger_type.value} requires bypass_llm=False."
                )
            if self.trigger_type in {
                EmergencyTriggerType.STOCK_DROP,
                EmergencyTriggerType.INDEX_CRASH,
                EmergencyTriggerType.PORTFOLIO_LOSS,
            }:
                if not self.requires_llm_review:
                    raise ValueError(
                        f"{self.trigger_type.value} requires requires_llm_review=True."
                    )

        if self.below_invested_min and not self.below_min_reason:
            raise ValueError("below_invested_min=True requires below_min_reason.")

        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic canonical dict 표현을 반환한다."""
        payload: dict[str, Any] = {
            "trigger_id": self.trigger_id,
            "trigger_type": self.trigger_type.value,
            "detected_at": self.detected_at.isoformat(),
            "market": self.market.value if self.market is not None else None,
            "symbol": self.symbol,
            "severity": self.severity.value,
            "status": self.status.value,
            "threshold_percent": str(self.threshold_percent),
            "observed_percent": str(self.observed_percent),
            "scope_symbols": list(self.scope_symbols),
            "account_role": self.account_role.value if self.account_role is not None else None,
            "execution_mode": self.execution_mode.value,
            "bypass_llm": self.bypass_llm,
            "requires_llm_review": self.requires_llm_review,
            "requires_recovery_review": self.requires_recovery_review,
            "below_invested_min": self.below_invested_min,
            "below_min_reason": self.below_min_reason,
            "cooldown_key": self.cooldown_key,
            "metadata": dict(self.metadata),
        }
        return canonicalize_payload(payload)

    def payload_hash(self) -> str:
        """canonical JSON 기준 sha256 hex digest."""
        return payload_sha256(self.to_canonical_dict())


class EmergencyTriggerEvent(BaseModel):
    """Operational trigger/audit event wrapper. Postmortem error_tags와 분리된다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    payload: TriggerPayload
    created_at: datetime
    suppressed_reason: str | None = None
    related_debug_event_code: str | None = None
    related_daily_summary_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id", mode="before")
    @classmethod
    def validate_event_id(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="event_id")

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return require_timezone_aware_datetime(value, field_name="created_at")

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        expected = canonicalize_payload(self.metadata)
        if self.metadata != expected:
            raise ValueError("metadata must be in canonical JSON-compatible form.")
        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic canonical dict 표현을 반환한다."""
        payload: dict[str, Any] = {
            "event_id": self.event_id,
            "payload": self.payload.to_canonical_dict(),
            "created_at": self.created_at.isoformat(),
            "suppressed_reason": self.suppressed_reason,
            "related_debug_event_code": self.related_debug_event_code,
            "related_daily_summary_id": self.related_daily_summary_id,
            "metadata": dict(self.metadata),
        }
        return canonicalize_payload(payload)
