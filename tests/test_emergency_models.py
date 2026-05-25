from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config.settings import ExecutionMode
from decision.canonical_json import canonicalize_payload
from domain.enums import AccountRole, Market
from emergency.models import (
    EmergencyTriggerSeverity,
    EmergencyTriggerStatus,
    EmergencyTriggerType,
    TriggerPayload,
    build_cooldown_key,
    sort_triggers_by_priority,
)
from emergency_fixtures import sample_mdd_payload, sample_stock_drop_payload


def test_valid_stock_drop_payload() -> None:
    payload = sample_stock_drop_payload()
    assert payload.trigger_type == EmergencyTriggerType.STOCK_DROP
    assert payload.execution_mode == ExecutionMode.EMERGENCY_TRIGGER


def test_valid_index_crash_payload() -> None:
    payload = sample_stock_drop_payload(
        trigger_id="trigger-index-001",
        trigger_type=EmergencyTriggerType.INDEX_CRASH,
        symbol="KOSPI",
        cooldown_key=build_cooldown_key(
            trigger_type=EmergencyTriggerType.INDEX_CRASH,
            market=Market.KR,
            symbol=None,
        ),
    )
    assert payload.requires_llm_review is True


def test_valid_portfolio_loss_payload() -> None:
    payload = sample_stock_drop_payload(
        trigger_id="trigger-portfolio-001",
        trigger_type=EmergencyTriggerType.PORTFOLIO_LOSS,
        market=None,
        symbol=None,
        cooldown_key=build_cooldown_key(
            trigger_type=EmergencyTriggerType.PORTFOLIO_LOSS,
            market=None,
            symbol=None,
        ),
    )
    assert payload.execution_mode == ExecutionMode.EMERGENCY_TRIGGER


def test_valid_profit_run_stages() -> None:
    for threshold, requires_llm, status in [
        (Decimal("10"), False, EmergencyTriggerStatus.NOOP),
        (Decimal("15"), True, EmergencyTriggerStatus.DETECTED),
        (Decimal("20"), True, EmergencyTriggerStatus.DETECTED),
    ]:
        payload = sample_stock_drop_payload(
            trigger_id=f"trigger-profit-{threshold}",
            trigger_type=EmergencyTriggerType.PROFIT_RUN,
            threshold_percent=threshold,
            observed_percent=threshold,
            requires_llm_review=requires_llm,
            status=status,
            severity=EmergencyTriggerSeverity.LOW if threshold == 10 else EmergencyTriggerSeverity.MEDIUM,
        )
        assert payload.trigger_type == EmergencyTriggerType.PROFIT_RUN


def test_valid_mdd_payload() -> None:
    payload = sample_mdd_payload()
    assert payload.execution_mode == ExecutionMode.MDD_KILLSWITCH
    assert payload.bypass_llm is True
    assert payload.requires_llm_review is False


def test_blank_trigger_id_rejected() -> None:
    with pytest.raises(ValueError, match="trigger_id must not be blank"):
        sample_stock_drop_payload(trigger_id="  ")


def test_naive_detected_at_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        sample_stock_drop_payload(detected_at=datetime(2026, 5, 24, 14, 30))


def test_metadata_must_be_canonical_json_compatible() -> None:
    with pytest.raises(ValueError, match="float values are not allowed"):
        sample_stock_drop_payload(metadata={"bad": 1.5})


def test_canonical_serialization_deterministic() -> None:
    payload = sample_stock_drop_payload()
    first = payload.to_canonical_dict()
    second = payload.to_canonical_dict()
    assert first == second
    assert payload.payload_hash() == payload.payload_hash()


def test_mdd_requires_mdd_execution_mode() -> None:
    with pytest.raises(ValueError, match="MDD_KILLSWITCH requires execution_mode"):
        sample_mdd_payload(execution_mode=ExecutionMode.EMERGENCY_TRIGGER)


def test_mdd_requires_bypass_llm() -> None:
    with pytest.raises(ValueError, match="bypass_llm=True"):
        sample_mdd_payload(bypass_llm=False)


def test_non_mdd_requires_emergency_trigger_mode() -> None:
    with pytest.raises(ValueError, match="STOCK_DROP requires execution_mode"):
        sample_stock_drop_payload(execution_mode=ExecutionMode.NORMAL)


def test_stock_drop_requires_llm_review() -> None:
    with pytest.raises(ValueError, match="requires_llm_review=True"):
        sample_stock_drop_payload(requires_llm_review=False)


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TriggerPayload(
            trigger_id="x",
            trigger_type=EmergencyTriggerType.STOCK_DROP,
            detected_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
            market=Market.KR,
            symbol="005930",
            severity=EmergencyTriggerSeverity.HIGH,
            status=EmergencyTriggerStatus.DETECTED,
            threshold_percent=Decimal("-3"),
            observed_percent=Decimal("-4"),
            scope_symbols=("005930",),
            account_role=AccountRole.PAPER,
            execution_mode=ExecutionMode.EMERGENCY_TRIGGER,
            bypass_llm=False,
            requires_llm_review=True,
            requires_recovery_review=True,
            below_invested_min=False,
            below_min_reason=None,
            cooldown_key="key",
            metadata={},
            extra_field="bad",
        )


def test_blank_scope_symbol_rejected() -> None:
    with pytest.raises(ValueError, match="scope_symbols must not be blank"):
        sample_stock_drop_payload(scope_symbols=("005930", "  "))


def test_priority_sorting() -> None:
    mdd = sample_mdd_payload(trigger_id="mdd")
    portfolio = sample_stock_drop_payload(
        trigger_id="portfolio",
        trigger_type=EmergencyTriggerType.PORTFOLIO_LOSS,
        market=None,
        symbol=None,
        cooldown_key=build_cooldown_key(
            trigger_type=EmergencyTriggerType.PORTFOLIO_LOSS,
            market=None,
            symbol=None,
        ),
    )
    index = sample_stock_drop_payload(
        trigger_id="index",
        trigger_type=EmergencyTriggerType.INDEX_CRASH,
        symbol="KOSPI",
        cooldown_key=build_cooldown_key(
            trigger_type=EmergencyTriggerType.INDEX_CRASH,
            market=Market.KR,
            symbol=None,
        ),
    )
    stock = sample_stock_drop_payload(trigger_id="stock")
    profit = sample_stock_drop_payload(
        trigger_id="profit",
        trigger_type=EmergencyTriggerType.PROFIT_RUN,
        requires_llm_review=True,
        status=EmergencyTriggerStatus.DETECTED,
        cooldown_key=build_cooldown_key(
            trigger_type=EmergencyTriggerType.PROFIT_RUN,
            market=Market.KR,
            symbol="005930",
        ),
    )

    sorted_payloads = sort_triggers_by_priority([profit, stock, index, portfolio, mdd])
    assert [p.trigger_id for p in sorted_payloads] == [
        "mdd",
        "portfolio",
        "index",
        "stock",
        "profit",
    ]


def test_priority_tie_break_deterministic() -> None:
    ts = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)
    a = sample_stock_drop_payload(trigger_id="aaa", symbol="005930", detected_at=ts)
    b = sample_stock_drop_payload(trigger_id="bbb", symbol="000660", detected_at=ts)
    result = sort_triggers_by_priority([b, a])
    assert result[0].symbol == "000660"
    assert result[1].symbol == "005930"
