from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from domain.enums import AccountRole, AssetClass, Market
from emergency.models import (
    EmergencyTriggerSeverity,
    EmergencyTriggerStatus,
    EmergencyTriggerType,
    TriggerPayload,
    build_cooldown_key,
)
from config.settings import ExecutionMode

NOW = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)


def sample_stock_drop_payload(**overrides: object) -> TriggerPayload:
    base = {
        "trigger_id": "trigger-stock-drop-001",
        "trigger_type": EmergencyTriggerType.STOCK_DROP,
        "detected_at": NOW,
        "market": Market.KR,
        "symbol": "005930",
        "severity": EmergencyTriggerSeverity.HIGH,
        "status": EmergencyTriggerStatus.DETECTED,
        "threshold_percent": Decimal("-3"),
        "observed_percent": Decimal("-3.5"),
        "scope_symbols": ("005930", "000660"),
        "account_role": AccountRole.PAPER,
        "execution_mode": ExecutionMode.EMERGENCY_TRIGGER,
        "bypass_llm": False,
        "requires_llm_review": True,
        "requires_recovery_review": True,
        "below_invested_min": False,
        "below_min_reason": None,
        "cooldown_key": build_cooldown_key(
            trigger_type=EmergencyTriggerType.STOCK_DROP,
            market=Market.KR,
            symbol="005930",
        ),
        "metadata": {"sector": "semiconductor"},
    }
    base.update(overrides)
    return TriggerPayload(**base)


def sample_mdd_payload(**overrides: object) -> TriggerPayload:
    base = {
        "trigger_id": "trigger-mdd-level1-001",
        "trigger_type": EmergencyTriggerType.MDD_KILLSWITCH,
        "detected_at": NOW,
        "market": None,
        "symbol": None,
        "severity": EmergencyTriggerSeverity.HIGH,
        "status": EmergencyTriggerStatus.DETECTED,
        "threshold_percent": Decimal("-10"),
        "observed_percent": Decimal("-10.5"),
        "scope_symbols": (),
        "account_role": AccountRole.PAPER,
        "execution_mode": ExecutionMode.MDD_KILLSWITCH,
        "bypass_llm": True,
        "requires_llm_review": False,
        "requires_recovery_review": True,
        "below_invested_min": True,
        "below_min_reason": "MDD_KILLSWITCH",
        "cooldown_key": build_cooldown_key(
            trigger_type=EmergencyTriggerType.MDD_KILLSWITCH,
            market=None,
            symbol=None,
        ),
        "metadata": {
            "mdd_stage": "LEVEL_1",
            "target_cash_percent": "50",
            "historical_peak_nav": "10000000",
        },
    }
    base.update(overrides)
    return TriggerPayload(**base)
