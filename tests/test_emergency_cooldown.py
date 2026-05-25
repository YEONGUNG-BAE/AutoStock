from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain.enums import Market
from emergency.cooldown import (
    MddCooldownEvent,
    should_suppress_by_cooldown,
    should_suppress_mdd_stage,
)
from emergency.models import EmergencyTriggerType, MddStage


def test_no_prior_event_allowed() -> None:
    now = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)
    decision = should_suppress_by_cooldown(
        trigger_type=EmergencyTriggerType.STOCK_DROP,
        market=Market.KR,
        symbol="005930",
        now=now,
        last_triggered_at=None,
        cooldown_minutes=60,
    )
    assert decision.suppressed is False


def test_elapsed_below_cooldown_suppressed() -> None:
    now = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)
    last = now - timedelta(minutes=30)
    decision = should_suppress_by_cooldown(
        trigger_type=EmergencyTriggerType.STOCK_DROP,
        market=Market.KR,
        symbol="005930",
        now=now,
        last_triggered_at=last,
        cooldown_minutes=60,
    )
    assert decision.suppressed is True
    assert decision.debug_event_code == "EMERGENCY_TRIGGER_RATE_LIMITED"


def test_elapsed_equal_cooldown_allowed() -> None:
    now = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)
    last = now - timedelta(minutes=60)
    decision = should_suppress_by_cooldown(
        trigger_type=EmergencyTriggerType.INDEX_CRASH,
        market=Market.US,
        symbol=None,
        now=now,
        last_triggered_at=last,
        cooldown_minutes=60,
    )
    assert decision.suppressed is False


def test_cooldown_key_includes_trigger_market_symbol() -> None:
    from emergency.models import build_cooldown_key

    key = build_cooldown_key(
        trigger_type=EmergencyTriggerType.STOCK_DROP,
        market=Market.KR,
        symbol="005930",
    )
    assert key == "STOCK_DROP:KR:005930"


def test_cooldown_decisions_deterministic() -> None:
    now = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)
    last = now - timedelta(minutes=45)
    first = should_suppress_by_cooldown(
        trigger_type=EmergencyTriggerType.PORTFOLIO_LOSS,
        market=None,
        symbol=None,
        now=now,
        last_triggered_at=last,
        cooldown_minutes=60,
    )
    second = should_suppress_by_cooldown(
        trigger_type=EmergencyTriggerType.PORTFOLIO_LOSS,
        market=None,
        symbol=None,
        now=now,
        last_triggered_at=last,
        cooldown_minutes=60,
    )
    assert first.suppressed == second.suppressed


def test_mdd_same_stage_same_day_suppressed() -> None:
    now = datetime(2026, 5, 24, 16, 0, tzinfo=UTC)
    prior = (
        MddCooldownEvent(
            stage=MddStage.LEVEL_1,
            triggered_at=datetime(2026, 5, 24, 10, 0, tzinfo=UTC),
        ),
    )
    decision = should_suppress_mdd_stage(stage=MddStage.LEVEL_1, now=now, prior_events=prior)
    assert decision.suppressed is True
    assert decision.debug_event_code == "MDD_COOLDOWN_ACTIVE"


def test_mdd_different_stages_same_day_allowed() -> None:
    now = datetime(2026, 5, 24, 16, 0, tzinfo=UTC)
    prior = (
        MddCooldownEvent(
            stage=MddStage.LEVEL_1,
            triggered_at=datetime(2026, 5, 24, 10, 0, tzinfo=UTC),
        ),
    )
    decision = should_suppress_mdd_stage(stage=MddStage.LEVEL_2, now=now, prior_events=prior)
    assert decision.suppressed is False


def test_mdd_level_2_within_4_hours_of_level_1_suppressed() -> None:
    level_1_at = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    now = level_1_at + timedelta(hours=2)
    prior = (MddCooldownEvent(stage=MddStage.LEVEL_1, triggered_at=level_1_at),)
    decision = should_suppress_mdd_stage(stage=MddStage.LEVEL_2, now=now, prior_events=prior)
    assert decision.suppressed is True


def test_mdd_level_3_same_stage_same_day_suppressed() -> None:
    now = datetime(2026, 5, 24, 18, 0, tzinfo=UTC)
    prior = (
        MddCooldownEvent(
            stage=MddStage.LEVEL_3,
            triggered_at=datetime(2026, 5, 24, 10, 0, tzinfo=UTC),
        ),
    )
    decision = should_suppress_mdd_stage(stage=MddStage.LEVEL_3, now=now, prior_events=prior)
    assert decision.suppressed is True
    assert decision.debug_event_code == "MDD_COOLDOWN_ACTIVE"
    assert decision.reason is not None
    assert "LEVEL_3" in decision.reason


def test_mdd_level_3_ignores_interval_cooldown_from_lower_stages() -> None:
    now = datetime(2026, 5, 24, 11, 0, tzinfo=UTC)
    prior = (
        MddCooldownEvent(
            stage=MddStage.LEVEL_1,
            triggered_at=datetime(2026, 5, 24, 10, 0, tzinfo=UTC),
        ),
    )
    decision = should_suppress_mdd_stage(stage=MddStage.LEVEL_3, now=now, prior_events=prior)
    assert decision.suppressed is False


def test_negative_cooldown_minutes_rejected() -> None:
    with pytest.raises(ValueError, match="cooldown_minutes must be >= 0"):
        should_suppress_by_cooldown(
            trigger_type=EmergencyTriggerType.STOCK_DROP,
            market=Market.KR,
            symbol="005930",
            now=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
            last_triggered_at=None,
            cooldown_minutes=-1,
        )
