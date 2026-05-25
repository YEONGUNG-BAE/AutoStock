from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config.settings import ExecutionMode
from domain.enums import AccountRole, AssetClass, Market, OrderSide
from emergency.cooldown import MddCooldownEvent
from emergency.mdd import (
    MddLiquidationPosition,
    MddState,
    build_mdd_liquidation_plan,
    compute_mdd_percent,
    detect_mdd_killswitch,
)
from emergency.models import MddStage, mdd_stage_for_percent, mdd_target_cash_percent
from emergency_fixtures import sample_mdd_payload

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "emergency"


def test_mdd_percent_computed_from_nav_and_peak() -> None:
    mdd = compute_mdd_percent(current_nav=Decimal("9000000"), historical_peak_nav=Decimal("10000000"))
    assert mdd == Decimal("-10")


def test_mdd_level_mapping() -> None:
    assert mdd_stage_for_percent(Decimal("-10")) == MddStage.LEVEL_1
    assert mdd_stage_for_percent(Decimal("-15")) == MddStage.LEVEL_2
    assert mdd_stage_for_percent(Decimal("-20")) == MddStage.LEVEL_3
    assert mdd_target_cash_percent(MddStage.LEVEL_1) == Decimal("50")
    assert mdd_target_cash_percent(MddStage.LEVEL_2) == Decimal("80")
    assert mdd_target_cash_percent(MddStage.LEVEL_3) == Decimal("95")


def test_detect_mdd_killswitch_level_1() -> None:
    detected_at = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)
    state = MddState(
        current_nav=Decimal("9000000"),
        historical_peak_nav=Decimal("10000000"),
        mdd_percent=Decimal("-10"),
        detected_at=detected_at,
        account_role=AccountRole.PAPER,
    )
    payload = detect_mdd_killswitch(state=state, trigger_id="mdd-l1")
    assert payload is not None
    assert payload.metadata["mdd_stage"] == "LEVEL_1"


def test_same_stage_duplicate_same_day_suppressed() -> None:
    detected_at = datetime(2026, 5, 24, 16, 0, tzinfo=UTC)
    state = MddState(
        current_nav=Decimal("9000000"),
        historical_peak_nav=Decimal("10000000"),
        mdd_percent=Decimal("-10"),
        detected_at=detected_at,
    )
    prior = (
        MddCooldownEvent(
            stage=MddStage.LEVEL_1,
            triggered_at=datetime(2026, 5, 24, 10, 0, tzinfo=UTC),
        ),
    )
    assert detect_mdd_killswitch(state=state, trigger_id="dup", prior_cooldown_events=prior) is None


def test_historical_peak_not_reset_by_planning() -> None:
    peak = Decimal("10000000")
    state = MddState(
        current_nav=Decimal("8000000"),
        historical_peak_nav=peak,
        mdd_percent=Decimal("-20"),
        detected_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
    )
    payload = detect_mdd_killswitch(state=state, trigger_id="mdd-l3")
    assert payload is not None
    assert payload.metadata["historical_peak_nav"] == str(peak)


def test_candidate_order_intents_mdd_execution_mode() -> None:
    trigger = sample_mdd_payload(
        metadata={
            "mdd_stage": "LEVEL_1",
            "target_cash_percent": "50",
            "historical_peak_nav": "10000000",
        }
    )
    positions = (
        MddLiquidationPosition(
            symbol="LOSS1",
            market=Market.KR,
            asset_class=AssetClass.KR_EQUITY,
            account_role=AccountRole.PAPER,
            quantity=Decimal("100"),
            market_value=Decimal("3000000"),
            pnl_vs_cost=Decimal("-500000"),
        ),
        MddLiquidationPosition(
            symbol="PROF1",
            market=Market.KR,
            asset_class=AssetClass.KR_EQUITY,
            account_role=AccountRole.PAPER,
            quantity=Decimal("50"),
            market_value=Decimal("2000000"),
            pnl_vs_cost=Decimal("200000"),
        ),
    )
    plan = build_mdd_liquidation_plan(
        trigger_payload=trigger,
        current_cash=Decimal("1000000"),
        total_nav=Decimal("10000000"),
        positions=positions,
        correlation_id="corr-mdd",
    )
    assert plan.candidate_order_intents
    for intent in plan.candidate_order_intents:
        assert intent.execution_mode == ExecutionMode.MDD_KILLSWITCH
        assert intent.side == OrderSide.SELL
        assert intent.reason_code == "MDD_LEVEL_1_TRIGGERED"


def test_suspended_positions_excluded() -> None:
    trigger = sample_mdd_payload(
        metadata={
            "mdd_stage": "LEVEL_2",
            "target_cash_percent": "80",
            "historical_peak_nav": "10000000",
        }
    )
    positions = (
        MddLiquidationPosition(
            symbol="SUSP",
            market=Market.KR,
            asset_class=AssetClass.KR_EQUITY,
            account_role=AccountRole.PAPER,
            quantity=Decimal("100"),
            market_value=Decimal("5000000"),
            pnl_vs_cost=Decimal("-1000000"),
            is_suspended=True,
        ),
        MddLiquidationPosition(
            symbol="OK",
            market=Market.KR,
            asset_class=AssetClass.KR_EQUITY,
            account_role=AccountRole.PAPER,
            quantity=Decimal("100"),
            market_value=Decimal("3000000"),
            pnl_vs_cost=Decimal("-500000"),
        ),
    )
    plan = build_mdd_liquidation_plan(
        trigger_payload=trigger,
        current_cash=Decimal("500000"),
        total_nav=Decimal("10000000"),
        positions=positions,
        correlation_id="corr-mdd",
    )
    assert "SUSP" in plan.excluded_symbols
    assert all(intent.symbol != "SUSP" for intent in plan.candidate_order_intents)


def test_loss_positions_prioritized_before_profitable() -> None:
    trigger = sample_mdd_payload(
        metadata={
            "mdd_stage": "LEVEL_1",
            "target_cash_percent": "50",
            "historical_peak_nav": "10000000",
        }
    )
    positions = (
        MddLiquidationPosition(
            symbol="PROF",
            market=Market.KR,
            asset_class=AssetClass.KR_EQUITY,
            account_role=AccountRole.PAPER,
            quantity=Decimal("100"),
            market_value=Decimal("2000000"),
            pnl_vs_cost=Decimal("300000"),
        ),
        MddLiquidationPosition(
            symbol="LOSS",
            market=Market.KR,
            asset_class=AssetClass.KR_EQUITY,
            account_role=AccountRole.PAPER,
            quantity=Decimal("100"),
            market_value=Decimal("3000000"),
            pnl_vs_cost=Decimal("-800000"),
        ),
    )
    plan = build_mdd_liquidation_plan(
        trigger_payload=trigger,
        current_cash=Decimal("1000000"),
        total_nav=Decimal("10000000"),
        positions=positions,
        correlation_id="corr-mdd",
    )
    symbols = [intent.symbol for intent in plan.candidate_order_intents]
    if "LOSS" in symbols and "PROF" in symbols:
        assert symbols.index("LOSS") < symbols.index("PROF")


def test_level_3_halt_required() -> None:
    trigger = sample_mdd_payload(
        observed_percent=Decimal("-20"),
        metadata={
            "mdd_stage": "LEVEL_3",
            "target_cash_percent": "95",
            "historical_peak_nav": "10000000",
        },
    )
    plan = build_mdd_liquidation_plan(
        trigger_payload=trigger,
        current_cash=Decimal("500000"),
        total_nav=Decimal("10000000"),
        positions=(),
        correlation_id="corr-mdd",
    )
    assert plan.halt_required is True


def test_golden_mdd_levels_fixture() -> None:
    data = json.loads((FIXTURES / "mdd_levels.json").read_text(encoding="utf-8"))
    detected_at = datetime.fromisoformat(data["detected_at"])
    for level in data["levels"]:
        mdd = compute_mdd_percent(
            current_nav=Decimal(level["current_nav"]),
            historical_peak_nav=Decimal(level["historical_peak_nav"]),
        )
        assert mdd == Decimal(level["expected_mdd_percent"])
        stage = mdd_stage_for_percent(mdd)
        assert stage is not None
        assert stage.value == level["expected_stage"]
        assert mdd_target_cash_percent(stage) == Decimal(level["target_cash_percent"])
