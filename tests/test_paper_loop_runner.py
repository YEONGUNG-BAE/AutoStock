from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import AnalysisAction
from broker import PaperBrokerAdapter
from decision import SQLiteDecisionStore
from domain import (
    AccountRole,
    CashSnapshot,
    Currency,
    DecisionId,
    Market,
    MarketPrice,
    Money,
    OrderStatus,
    Percent,
)
from ledger import SQLiteLedger
from paper_loop import (
    PaperLoopInput,
    PaperLoopRunner,
    PaperLoopStatus,
)
from risk import RiskMode


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
SYMBOL = "005930"
PRICE = Decimal("70000")
INITIAL_CASH = Decimal("100000000")


@pytest.fixture
def loop_env(tmp_path: Path):
    """Paper loop runner + ledger + decision store fixture."""
    ledger = SQLiteLedger(tmp_path / "ledger.db")
    decision_store = SQLiteDecisionStore(tmp_path / "decisions.db")
    broker = PaperBrokerAdapter(
        ledger,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=INITIAL_CASH,
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
    )
    runner = PaperLoopRunner(
        ledger=ledger,
        decision_store=decision_store,
        broker=broker,
    )
    yield runner, ledger, decision_store, broker
    ledger.close()
    decision_store.close()


def _market_price(**overrides: object) -> MarketPrice:
    base = {
        "symbol": SYMBOL,
        "market": Market.KR,
        "currency": Currency.KRW,
        "price": PRICE,
        "as_of": NOW,
    }
    base.update(overrides)
    return MarketPrice(**base)


def _loop_input(
    sample_risk_input_factory,
    *,
    run_suffix: str,
    action: AnalysisAction,
    target_weight: str,
    context_overrides: dict | None = None,
) -> PaperLoopInput:
    ctx = {
        "current_symbol_market_value": Money.from_str("2000000", Currency.KRW),
        "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
    }
    if context_overrides:
        ctx.update(context_overrides)

    risk_input = sample_risk_input_factory(
        action=action,
        target_weight_percent=Percent(target_weight),
        context_overrides=ctx,
        allocator_overrides={"decision_id": DecisionId(f"allocator-{run_suffix}")},
        analysis_overrides={"decision_id": DecisionId(f"analysis-{run_suffix}")},
    )
    return PaperLoopInput(
        run_id=DecisionId(f"paper-loop-{run_suffix}"),
        created_at=NOW,
        allocator_decision=risk_input.allocator_decision,
        analysis_decision=risk_input.analysis_decision,
        risk_context=risk_input.context,
        market_price=_market_price(),
        correlation_id=f"corr-{run_suffix}",
    )


def test_valid_buy_path_filled(loop_env, sample_risk_input_factory) -> None:
    runner, ledger, decision_store, broker = loop_env
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="buy-001",
        action=AnalysisAction.BUY,
        target_weight="5",
    )

    result = runner.run(loop_input)

    assert result.status == PaperLoopStatus.FILLED
    assert result.generated_order_intent is not None
    assert result.generated_order_intent.target_weight_percent == Decimal("5")
    assert result.executable_order_intent is not None
    assert result.executable_order_intent.quantity == Decimal("42")
    assert result.broker_order_result is not None
    assert result.broker_order_result.status == OrderStatus.FILLED
    assert result.fill is not None
    assert result.nav_snapshot is not None

    cash = broker.get_cash(Currency.KRW, AccountRole.PAPER)
    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    assert position is not None
    assert position.quantity == Decimal("42")
    assert cash.amount == INITIAL_CASH - (Decimal("42") * PRICE)

    nav_rows = ledger.list_nav_snapshots()
    assert len(nav_rows) == 1
    assert nav_rows[0].total_nav_krw == cash.amount + (position.quantity * PRICE)

    snapshots = decision_store.list_decision_snapshots()
    assert len(snapshots) == 3


def test_valid_sell_path_filled(loop_env, sample_risk_input_factory) -> None:
    runner, ledger, decision_store, broker = loop_env

    buy_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="sell-seed",
        action=AnalysisAction.BUY,
        target_weight="5",
    )
    runner.run(buy_input)

    sell_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="sell-001",
        action=AnalysisAction.SELL,
        target_weight="2",
        context_overrides={
            "current_symbol_market_value": Money.from_str("5000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("3000000", Currency.KRW),
        },
    )
    result = runner.run(sell_input)

    assert result.status == PaperLoopStatus.FILLED
    assert result.executable_order_intent is not None
    # 실제 보유 42주(2.94M) 기준: target 2M → reduction 0.94M / 70k = 13
    assert result.executable_order_intent.quantity == Decimal("13")
    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    assert position is not None
    assert position.quantity == Decimal("29")


def test_hold_path_noop(loop_env, sample_risk_input_factory) -> None:
    runner, ledger, decision_store, broker = loop_env
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="hold-001",
        action=AnalysisAction.HOLD,
        target_weight="5",
    )

    cash_before = broker.get_cash(Currency.KRW, AccountRole.PAPER)
    before_entries = ledger.list_cash_ledger_entries(currency=Currency.KRW)
    result = runner.run(loop_input)

    assert result.status == PaperLoopStatus.NOOP
    assert result.broker_order_result is None
    assert result.fill is None
    assert result.nav_snapshot is None
    assert broker.get_cash(Currency.KRW, AccountRole.PAPER).amount == cash_before.amount
    after_entries = ledger.list_cash_ledger_entries(currency=Currency.KRW)
    assert after_entries == before_entries
    assert len(ledger.list_nav_snapshots()) == 0


def test_risk_blocked_no_broker_call(loop_env, sample_risk_input_factory) -> None:
    runner, ledger, decision_store, broker = loop_env
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="blocked-001",
        action=AnalysisAction.BUY,
        target_weight="12",
        context_overrides={
            "allocator_symbol_target_weight": Percent("5"),
            "current_symbol_market_value": Money.from_str("4000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )

    result = runner.run(loop_input)

    assert result.status == PaperLoopStatus.RISK_BLOCKED
    assert result.broker_order_result is None
    assert ledger.get_order_intent("order-analysis-blocked-001") is None


def test_quantity_failed_no_broker_records(loop_env, sample_risk_input_factory) -> None:
    runner, ledger, decision_store, broker = loop_env
    from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity
    from paper_loop.models import (
        PAPER_LOOP_QUANTITY_CURRENCY_MISMATCH,
        QuantityResolutionResult,
        QuantityResolutionStatus,
    )
    from paper_loop.quantity_resolver import QuantityResolver

    class _FailingResolver(QuantityResolver):
        def resolve(self, **kwargs):
            return QuantityResolutionResult(
                status=QuantityResolutionStatus.FAILED,
                order_intent=None,
                validation_result=ValidationResult(
                    passed=False,
                    issues=(
                        ValidationIssue(
                            code=PAPER_LOOP_QUANTITY_CURRENCY_MISMATCH,
                            message="forced failure",
                            severity=ValidationSeverity.ERROR,
                        ),
                    ),
                ),
            )

    failing_runner = PaperLoopRunner(
        ledger=ledger,
        decision_store=decision_store,
        broker=broker,
        quantity_resolver=_FailingResolver(),
    )
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="qty-fail-001",
        action=AnalysisAction.BUY,
        target_weight="5",
    )
    result = failing_runner.run(loop_input)

    assert result.status == PaperLoopStatus.QUANTITY_FAILED
    assert result.broker_order_result is None
    assert ledger.get_order_intent("order-analysis-qty-fail-001") is None


@pytest.fixture
def low_cash_loop_env(tmp_path: Path):
    """현금 부족 broker rejected 테스트용 fixture."""
    ledger = SQLiteLedger(tmp_path / "ledger_low.db")
    decision_store = SQLiteDecisionStore(tmp_path / "decisions_low.db")
    broker = PaperBrokerAdapter(
        ledger,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=Decimal("1000000"),
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
    )
    runner = PaperLoopRunner(
        ledger=ledger,
        decision_store=decision_store,
        broker=broker,
    )
    yield runner, ledger, decision_store, broker
    ledger.close()
    decision_store.close()


def test_broker_rejected_no_fill_side_effect(low_cash_loop_env, sample_risk_input_factory) -> None:
    runner, ledger, _, broker = low_cash_loop_env
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="reject-001",
        action=AnalysisAction.BUY,
        target_weight="5",
        context_overrides={
            "current_symbol_market_value": Money.from_str("0", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("0", Currency.KRW),
        },
    )

    cash_before = broker.get_cash(Currency.KRW, AccountRole.PAPER)
    result = runner.run(loop_input)

    assert result.status == PaperLoopStatus.BROKER_REJECTED
    assert result.broker_order_result is not None
    assert result.broker_order_result.status == OrderStatus.REJECTED
    assert result.fill is None
    assert broker.get_cash(Currency.KRW, AccountRole.PAPER).amount == cash_before.amount
    assert broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER) is None


def test_duplicate_run_id_validation_failed(loop_env, sample_risk_input_factory) -> None:
    runner, _, decision_store, _ = loop_env
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="dup-001",
        action=AnalysisAction.HOLD,
        target_weight="5",
    )

    first = runner.run(loop_input)
    assert first.status == PaperLoopStatus.NOOP

    second_input = loop_input.model_copy(
        update={
            "analysis_decision": loop_input.analysis_decision.model_copy(
                update={"decision_id": DecisionId("analysis-dup-002")}
            ),
            "allocator_decision": loop_input.allocator_decision.model_copy(
                update={"decision_id": DecisionId("allocator-dup-002")}
            ),
        }
    )
    second = runner.run(second_input)
    assert second.status == PaperLoopStatus.VALIDATION_FAILED
    assert len(decision_store.list_decision_snapshots()) == 3


def test_duplicate_run_id_buy_path_no_broker_leak(loop_env, sample_risk_input_factory) -> None:
    """same run_id 재실행 시 broker/ledger side effect가 발생하지 않아야 한다."""
    runner, ledger, decision_store, broker = loop_env
    shared_run_id = DecisionId("paper-loop-dup-buy-shared")

    first_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="dup-buy-a",
        action=AnalysisAction.BUY,
        target_weight="5",
    ).model_copy(update={"run_id": shared_run_id})

    first = runner.run(first_input)
    assert first.status == PaperLoopStatus.FILLED

    cash_after_first = broker.get_cash(Currency.KRW, AccountRole.PAPER).amount
    position_after_first = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    assert position_after_first is not None
    position_qty_after_first = position_after_first.quantity
    nav_count_after_first = len(ledger.list_nav_snapshots())
    snapshot_count_after_first = len(decision_store.list_decision_snapshots())

    second_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="dup-buy-b",
        action=AnalysisAction.BUY,
        target_weight="5",
    ).model_copy(update={"run_id": shared_run_id})

    second = runner.run(second_input)

    assert second.status == PaperLoopStatus.VALIDATION_FAILED
    assert second.decision_snapshot_ids == ()
    assert broker.get_cash(Currency.KRW, AccountRole.PAPER).amount == cash_after_first
    position_after_second = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    assert position_after_second is not None
    assert position_after_second.quantity == position_qty_after_first
    assert len(ledger.list_nav_snapshots()) == nav_count_after_first
    assert len(decision_store.list_decision_snapshots()) == snapshot_count_after_first
    assert ledger.get_order_intent("order-analysis-dup-buy-b") is None
    assert ledger.get_order_result("order-analysis-dup-buy-b") is None
    assert ledger.get_fill_by_order_id("order-analysis-dup-buy-b") is None


def test_noop_target_equal_no_broker(loop_env, sample_risk_input_factory) -> None:
    runner, ledger, _, broker = loop_env
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="noop-qty-001",
        action=AnalysisAction.BUY,
        target_weight="2",
    )

    result = runner.run(loop_input)

    assert result.status == PaperLoopStatus.NOOP
    assert result.generated_order_intent is not None
    assert result.executable_order_intent is None
    assert result.broker_order_result is None
    assert broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER) is None


def test_result_deterministic_for_same_input(tmp_path, sample_risk_input_factory) -> None:
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="det-001",
        action=AnalysisAction.HOLD,
        target_weight="5",
    )

    ledger1 = SQLiteLedger(tmp_path / "det1.db")
    store1 = SQLiteDecisionStore(tmp_path / "dec1.db")
    broker1 = PaperBrokerAdapter(
        ledger1,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=INITIAL_CASH,
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
    )
    runner1 = PaperLoopRunner(ledger=ledger1, decision_store=store1, broker=broker1)

    ledger2 = SQLiteLedger(tmp_path / "det2.db")
    store2 = SQLiteDecisionStore(tmp_path / "dec2.db")
    broker2 = PaperBrokerAdapter(
        ledger2,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=INITIAL_CASH,
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
    )
    runner2 = PaperLoopRunner(ledger=ledger2, decision_store=store2, broker=broker2)

    first = runner1.run(loop_input)
    second = runner2.run(loop_input)

    assert first.status == PaperLoopStatus.NOOP
    assert second.status == PaperLoopStatus.NOOP
    assert first.risk_result.to_canonical_dict() == second.risk_result.to_canonical_dict()
    ledger1.close()
    ledger2.close()
    store1.close()
    store2.close()
