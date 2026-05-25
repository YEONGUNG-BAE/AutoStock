from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, field_validator

from domain._datetime import require_timezone_aware_datetime
from domain.enums import Market
from emergency.models import (
    MDD_LEVEL_1_TO_2_COOLDOWN,
    EmergencyTriggerType,
    MddStage,
    mdd_reason_code,
)


class CooldownKey(BaseModel):
    """General emergency trigger cooldown key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_type: EmergencyTriggerType
    market: Market | None
    symbol: str | None


class CooldownDecision(BaseModel):
    """Cooldown 평가 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suppressed: bool
    elapsed_seconds: float | None
    cooldown_minutes: int
    debug_event_code: str | None = None


def should_suppress_by_cooldown(
    *,
    trigger_type: EmergencyTriggerType,
    market: Market | None,
    symbol: str | None,
    now: datetime,
    last_triggered_at: datetime | None,
    cooldown_minutes: int,
) -> CooldownDecision:
    """일반 emergency trigger cooldown을 평가한다."""
    now = require_timezone_aware_datetime(now, field_name="now")

    if cooldown_minutes < 0:
        raise ValueError("cooldown_minutes must be >= 0.")

    if last_triggered_at is None:
        return CooldownDecision(
            suppressed=False,
            elapsed_seconds=None,
            cooldown_minutes=cooldown_minutes,
        )

    last = require_timezone_aware_datetime(last_triggered_at, field_name="last_triggered_at")
    elapsed = now - last
    elapsed_seconds = elapsed.total_seconds()
    cooldown_delta = timedelta(minutes=cooldown_minutes)

    if elapsed < cooldown_delta:
        return CooldownDecision(
            suppressed=True,
            elapsed_seconds=elapsed_seconds,
            cooldown_minutes=cooldown_minutes,
            debug_event_code="EMERGENCY_TRIGGER_RATE_LIMITED",
        )

    return CooldownDecision(
        suppressed=False,
        elapsed_seconds=elapsed_seconds,
        cooldown_minutes=cooldown_minutes,
    )


class MddCooldownEvent(BaseModel):
    """MDD stage trigger 이력 (explicit state input)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: MddStage
    triggered_at: datetime

    @field_validator("triggered_at", mode="before")
    @classmethod
    def validate_triggered_at(cls, value) -> datetime:
        return require_timezone_aware_datetime(value, field_name="triggered_at")


class MddCooldownDecision(BaseModel):
    """MDD cooldown 평가 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    suppressed: bool
    reason: str | None = None
    debug_event_code: str | None = None


def _same_calendar_day(left: datetime, right: datetime) -> bool:
    """두 timezone-aware datetime이 같은 calendar day인지 확인한다."""
    return left.date() == right.date()


def should_suppress_mdd_stage(
    *,
    stage: MddStage,
    now: datetime,
    prior_events: tuple[MddCooldownEvent, ...],
) -> MddCooldownDecision:
    """MDD stage cooldown 규칙을 평가한다.

    - 동일 stage는 하루 1회만 trigger (Level 1/2/3 모두 적용)
    - 다른 stage는 같은 날 trigger 가능
    - Level 1 → Level 2는 4시간 interval cooldown
    - Level 3는 lower-stage interval cooldown만 무시 (same-day duplicate는 적용)
    """
    now = require_timezone_aware_datetime(now, field_name="now")

    # 1. Same-stage same-day duplicate applies to all stages, including LEVEL_3.
    for event in prior_events:
        if event.stage != stage:
            continue
        if _same_calendar_day(event.triggered_at, now):
            return MddCooldownDecision(
                suppressed=True,
                reason=f"same stage {stage.value} already triggered today",
                debug_event_code="MDD_COOLDOWN_ACTIVE",
            )

    # 2. LEVEL_3 ignores interval cooldown only.
    if stage == MddStage.LEVEL_3:
        return MddCooldownDecision(suppressed=False)

    # 3. LEVEL_2 keeps Level 1 -> Level 2 4-hour cooldown.
    if stage == MddStage.LEVEL_2:
        level_1_events = [e for e in prior_events if e.stage == MddStage.LEVEL_1]
        if level_1_events:
            latest_level_1 = max(level_1_events, key=lambda e: e.triggered_at)
            elapsed = now - latest_level_1.triggered_at
            if elapsed < MDD_LEVEL_1_TO_2_COOLDOWN:
                return MddCooldownDecision(
                    suppressed=True,
                    reason="Level 2 within 4 hours of Level 1",
                    debug_event_code="MDD_COOLDOWN_ACTIVE",
                )

    return MddCooldownDecision(suppressed=False)


def debug_event_code_for_mdd_stage(stage: MddStage) -> str:
    """MDD stage trigger debug event code."""
    return mdd_reason_code(stage)


def map_cooldown_to_debug_event(
    decision: CooldownDecision,
    *,
    trigger_type: EmergencyTriggerType,
) -> str | None:
    """CooldownDecision에서 Debug event code를 반환한다."""
    if not decision.suppressed:
        return None
    if trigger_type == EmergencyTriggerType.MDD_KILLSWITCH:
        return decision.debug_event_code or "MDD_COOLDOWN_ACTIVE"
    return decision.debug_event_code or "EMERGENCY_TRIGGER_RATE_LIMITED"
