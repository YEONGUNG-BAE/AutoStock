from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain.enums import AccountRole, Market
from emergency.detectors import (
    HeldStockSnapshot,
    IndexSnapshot,
    LossContributor,
    PortfolioSnapshot,
    ProfitRunSnapshot,
    detect_index_crash,
    detect_portfolio_loss,
    detect_profit_run,
    detect_stock_drop,
)
from emergency.models import (
    EmergencyTriggerStatus,
    EmergencyTriggerType,
    sort_triggers_by_priority,
)
from emergency_fixtures import sample_mdd_payload, sample_stock_drop_payload

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "emergency"


def test_stock_drop_fires_at_exactly_minus_3_percent() -> None:
    snapshot = HeldStockSnapshot(
        symbol="005930",
        market=Market.KR,
        sector="semiconductor",
        account_role=AccountRole.PAPER,
        quantity=Decimal("100"),
        previous_close=Decimal("100"),
        current_price=Decimal("97"),
        market_value=Decimal("9700"),
        same_sector_symbols=("000660",),
    )
    detected_at = datetime(2026, 5, 24, 14, 30, tzinfo=UTC)
    payload = detect_stock_drop(
        snapshot=snapshot,
        detected_at=detected_at,
        trigger_id="t1",
    )
    assert payload is not None
    assert payload.observed_percent == Decimal("-3")
    assert payload.scope_symbols == ("005930", "000660")


def test_stock_drop_does_not_fire_at_minus_2_99() -> None:
    snapshot = HeldStockSnapshot(
        symbol="005930",
        market=Market.KR,
        sector=None,
        account_role=AccountRole.PAPER,
        quantity=Decimal("100"),
        previous_close=Decimal("100"),
        current_price=Decimal("97.01"),
        market_value=Decimal("9701"),
    )
    assert detect_stock_drop(
        snapshot=snapshot,
        detected_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        trigger_id="t2",
    ) is None


def test_stock_drop_ignores_zero_quantity() -> None:
    snapshot = HeldStockSnapshot(
        symbol="005930",
        market=Market.KR,
        sector=None,
        account_role=AccountRole.PAPER,
        quantity=Decimal("0"),
        previous_close=Decimal("100"),
        current_price=Decimal("90"),
        market_value=Decimal("0"),
    )
    assert detect_stock_drop(
        snapshot=snapshot,
        detected_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        trigger_id="t3",
    ) is None


def test_index_crash_fires_at_exactly_minus_1_5_percent() -> None:
    snapshot = IndexSnapshot(
        market=Market.US,
        index_symbol="SPY",
        index_name="S&P 500",
        previous_close=Decimal("100"),
        current_value=Decimal("98.5"),
        affected_holdings=("AAPL", "MSFT"),
    )
    payload = detect_index_crash(
        snapshot=snapshot,
        detected_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        trigger_id="t4",
    )
    assert payload is not None
    assert payload.observed_percent == Decimal("-1.5")
    assert payload.scope_symbols == ("AAPL", "MSFT")


def test_portfolio_loss_fires_at_exactly_minus_2_percent() -> None:
    snapshot = PortfolioSnapshot(
        previous_total_nav=Decimal("100"),
        current_total_nav=Decimal("98"),
        loss_contributors=(
            LossContributor(symbol="A", loss_contribution=Decimal("-1")),
            LossContributor(symbol="B", loss_contribution=Decimal("-0.5")),
        ),
        account_role=AccountRole.PAPER,
    )
    payload = detect_portfolio_loss(
        snapshot=snapshot,
        detected_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        trigger_id="t5",
    )
    assert payload is not None
    assert payload.observed_percent == Decimal("-2")


def test_portfolio_loss_top_3_contributors() -> None:
    snapshot = PortfolioSnapshot(
        previous_total_nav=Decimal("10000000"),
        current_total_nav=Decimal("9800000"),
        loss_contributors=(
            LossContributor(symbol="005930", loss_contribution=Decimal("-80000")),
            LossContributor(symbol="000660", loss_contribution=Decimal("-60000")),
            LossContributor(symbol="035420", loss_contribution=Decimal("-40000")),
            LossContributor(symbol="035720", loss_contribution=Decimal("-20000")),
        ),
    )
    payload = detect_portfolio_loss(
        snapshot=snapshot,
        detected_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        trigger_id="t6",
    )
    assert payload is not None
    assert payload.scope_symbols == ("005930", "000660", "035420")


def test_portfolio_loss_tie_break_symbol_ascending() -> None:
    snapshot = PortfolioSnapshot(
        previous_total_nav=Decimal("100"),
        current_total_nav=Decimal("97"),
        loss_contributors=(
            LossContributor(symbol="ZZZ", loss_contribution=Decimal("-1")),
            LossContributor(symbol="AAA", loss_contribution=Decimal("-1")),
        ),
    )
    payload = detect_portfolio_loss(
        snapshot=snapshot,
        detected_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        trigger_id="t7",
    )
    assert payload is not None
    assert payload.scope_symbols[0] == "AAA"


def test_profit_run_stages() -> None:
    for weight, requires_llm, status in [
        (Decimal("10"), False, EmergencyTriggerStatus.NOOP),
        (Decimal("15"), True, EmergencyTriggerStatus.DETECTED),
        (Decimal("20"), True, EmergencyTriggerStatus.DETECTED),
    ]:
        payload = detect_profit_run(
            snapshot=ProfitRunSnapshot(
                symbol="AAPL",
                market=Market.US,
                account_role=AccountRole.PAPER,
                current_market_weight_percent=weight,
            ),
            detected_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
            trigger_id=f"profit-{weight}",
        )
        assert payload is not None
        assert payload.requires_llm_review is requires_llm
        assert payload.status == status


def test_profit_run_does_not_use_buy_cost() -> None:
    """current_market_weight_percent만 사용; buy-cost cap과 무관."""
    payload = detect_profit_run(
        snapshot=ProfitRunSnapshot(
            symbol="AAPL",
            market=Market.US,
            account_role=AccountRole.PAPER,
            current_market_weight_percent=Decimal("12"),
        ),
        detected_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        trigger_id="profit-weight-only",
    )
    assert payload is not None
    assert payload.observed_percent == Decimal("12")


def test_trigger_priority_ordering() -> None:
    payloads = [
        sample_stock_drop_payload(
            trigger_id="profit",
            trigger_type=EmergencyTriggerType.PROFIT_RUN,
            requires_llm_review=True,
            status=EmergencyTriggerStatus.DETECTED,
        ),
        sample_stock_drop_payload(trigger_id="stock"),
        sample_mdd_payload(trigger_id="mdd"),
    ]
    sorted_ids = [p.trigger_id for p in sort_triggers_by_priority(payloads)]
    assert sorted_ids == ["mdd", "stock", "profit"]


def test_golden_stock_drop_fixture() -> None:
    data = json.loads((FIXTURES / "stock_drop.json").read_text(encoding="utf-8"))
    snapshot = HeldStockSnapshot(
        symbol=data["symbol"],
        market=Market(data["market"]),
        sector=data["sector"],
        account_role=AccountRole(data["account_role"]),
        quantity=Decimal(data["quantity"]),
        previous_close=Decimal(data["previous_close"]),
        current_price=Decimal(data["current_price"]),
        market_value=Decimal(data["market_value"]),
        same_sector_symbols=tuple(data["same_sector_symbols"]),
    )
    payload = detect_stock_drop(
        snapshot=snapshot,
        detected_at=datetime.fromisoformat(data["detected_at"]),
        trigger_id=data["trigger_id"],
    )
    assert payload is not None
    assert payload.observed_percent == Decimal(data["expected_observed_percent"])
