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
    Percent,
)
from ledger import SQLiteLedger
from paper_loop import (
    PaperLoopInput,
    PaperLoopRunner,
    PaperLoopStatus,
    assert_same_decision_snapshot_hash,
    assert_same_generated_intent,
    assert_same_risk_result,
    replay_paper_loop,
)


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
SYMBOL = "005930"
PRICE = Decimal("70000")
INITIAL_CASH = Decimal("100000000")


def _make_runner(tmp_path: Path, *, db_suffix: str = "") -> tuple[PaperLoopRunner, SQLiteLedger, SQLiteDecisionStore, PaperBrokerAdapter]:
    ledger = SQLiteLedger(tmp_path / f"ledger{db_suffix}.db")
    decision_store = SQLiteDecisionStore(tmp_path / f"decisions{db_suffix}.db")
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
    return runner, ledger, decision_store, broker


def _market_price() -> MarketPrice:
    return MarketPrice(
        symbol=SYMBOL,
        market=Market.KR,
        currency=Currency.KRW,
        price=PRICE,
        as_of=NOW,
    )


def _loop_input(
    sample_risk_input_factory,
    *,
    run_suffix: str,
    action: AnalysisAction,
    target_weight: str,
) -> PaperLoopInput:
    risk_input = sample_risk_input_factory(
        action=action,
        target_weight_percent=Percent(target_weight),
        context_overrides={
            "current_symbol_market_value": Money.from_str("2000000", Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
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


def test_replay_same_risk_validation_result(tmp_path, sample_risk_input_factory) -> None:
    runner_a, ledger_a, _, _ = _make_runner(tmp_path, db_suffix="-a")
    runner_b, ledger_b, _, _ = _make_runner(tmp_path, db_suffix="-b")
    loop_input_a = _loop_input(
        sample_risk_input_factory,
        run_suffix="replay-risk-a",
        action=AnalysisAction.BUY,
        target_weight="5",
    )
    loop_input_b = loop_input_a.model_copy(
        update={"run_id": DecisionId("paper-loop-replay-risk-b")}
    )
    loop_input_b_ids = loop_input_b.model_copy(
        update={
            "allocator_decision": loop_input_a.allocator_decision.model_copy(
                update={"decision_id": DecisionId("allocator-replay-risk-b")}
            ),
            "analysis_decision": loop_input_a.analysis_decision.model_copy(
                update={"decision_id": DecisionId("analysis-replay-risk-b")}
            ),
        }
    )

    first = runner_a.run(loop_input_a)
    second = runner_b.run(loop_input_b_ids)

    assert_same_risk_result(first, second)
    ledger_a.close()
    ledger_b.close()


def test_replay_same_generated_order_intent(tmp_path, sample_risk_input_factory) -> None:
    base = _loop_input(
        sample_risk_input_factory,
        run_suffix="replay-gen",
        action=AnalysisAction.BUY,
        target_weight="5",
    )
    runner_a, ledger_a, store_a, _ = _make_runner(tmp_path, db_suffix="-gen-a")
    runner_b, ledger_b, store_b, _ = _make_runner(tmp_path, db_suffix="-gen-b")

    first = runner_a.run(base)
    second = runner_b.run(base)

    assert_same_generated_intent(first, second)
    assert first.generated_order_intent is not None
    assert first.generated_order_intent.order_id == f"order-{base.analysis_decision.decision_id.value}"
    ledger_a.close()
    ledger_b.close()
    store_a.close()
    store_b.close()


def test_replay_fresh_ledger_same_broker_effect(tmp_path, sample_risk_input_factory) -> None:
    runner_a, ledger_a, _, broker_a = _make_runner(tmp_path, db_suffix="-fx-a")
    runner_b, ledger_b, _, broker_b = _make_runner(tmp_path, db_suffix="-fx-b")

    input_a = _loop_input(
        sample_risk_input_factory,
        run_suffix="replay-fx-a",
        action=AnalysisAction.BUY,
        target_weight="5",
    )
    input_b = input_a.model_copy(
        update={
            "run_id": DecisionId("paper-loop-replay-fx-b"),
            "allocator_decision": input_a.allocator_decision.model_copy(
                update={"decision_id": DecisionId("allocator-replay-fx-b")}
            ),
            "analysis_decision": input_a.analysis_decision.model_copy(
                update={"decision_id": DecisionId("analysis-replay-fx-b")}
            ),
        }
    )

    result_a = runner_a.run(input_a)
    result_b = runner_b.run(input_b)

    assert result_a.status == PaperLoopStatus.FILLED
    assert result_b.status == PaperLoopStatus.FILLED

    pos_a = broker_a.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    pos_b = broker_b.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    cash_a = broker_a.get_cash(Currency.KRW, AccountRole.PAPER)
    cash_b = broker_b.get_cash(Currency.KRW, AccountRole.PAPER)

    assert pos_a is not None and pos_b is not None
    assert pos_a.quantity == pos_b.quantity
    assert cash_a.amount == cash_b.amount
    ledger_a.close()
    ledger_b.close()


def test_duplicate_order_id_on_same_ledger_deterministic(tmp_path, sample_risk_input_factory) -> None:
    runner, ledger, _, broker = _make_runner(tmp_path)
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="dup-order-001",
        action=AnalysisAction.BUY,
        target_weight="5",
    )

    first = runner.run(loop_input)
    assert first.status == PaperLoopStatus.FILLED
    assert first.executable_order_intent is not None

    duplicate_result = broker.submit_order(
        first.executable_order_intent,
        _market_price(),
    )

    assert duplicate_result.rejection_reason == "duplicate order_id"
    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    assert position is not None
    assert position.quantity == Decimal("42")
    ledger.close()


def test_fresh_ledger_twice_identical_final_state(tmp_path, sample_risk_input_factory) -> None:
    def run_once(suffix: str) -> tuple[Decimal, Decimal | None]:
        runner, ledger, _, broker = _make_runner(tmp_path, db_suffix=f"-twice-{suffix}")
        loop_input = _loop_input(
            sample_risk_input_factory,
            run_suffix=f"twice-{suffix}",
            action=AnalysisAction.BUY,
            target_weight="5",
        )
        result = runner.run(loop_input)
        assert result.status == PaperLoopStatus.FILLED
        cash = broker.get_cash(Currency.KRW, AccountRole.PAPER).amount
        position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
        qty = position.quantity if position else None
        ledger.close()
        return cash, qty

    cash_a, qty_a = run_once("a")
    cash_b, qty_b = run_once("b")
    assert cash_a == cash_b
    assert qty_a == qty_b


def test_order_id_deterministic(tmp_path, sample_risk_input_factory) -> None:
    runner, ledger, _, _ = _make_runner(tmp_path)
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="order-id-001",
        action=AnalysisAction.BUY,
        target_weight="5",
    )
    result = runner.run(loop_input)

    expected_order_id = f"order-{loop_input.analysis_decision.decision_id.value}"
    assert result.generated_order_intent is not None
    assert result.generated_order_intent.order_id == expected_order_id
    assert result.executable_order_intent is not None
    assert result.executable_order_intent.order_id == expected_order_id
    ledger.close()


def test_decision_snapshot_hash_deterministic(tmp_path, sample_risk_input_factory) -> None:
    runner, ledger, decision_store, _ = _make_runner(tmp_path)
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="hash-001",
        action=AnalysisAction.HOLD,
        target_weight="5",
    )
    runner.run(loop_input)
    assert_same_decision_snapshot_hash(decision_store, loop_input.normalized_run_id)
    ledger.close()
    decision_store.close()


def test_replay_paper_loop_helper(tmp_path, sample_risk_input_factory) -> None:
    runner, ledger, _, _ = _make_runner(tmp_path)
    loop_input = _loop_input(
        sample_risk_input_factory,
        run_suffix="helper-001",
        action=AnalysisAction.HOLD,
        target_weight="5",
    )
    first = replay_paper_loop(runner, loop_input)
    second = replay_paper_loop(runner, loop_input)
    assert first.status == PaperLoopStatus.NOOP
    assert second.status == PaperLoopStatus.VALIDATION_FAILED
    ledger.close()
