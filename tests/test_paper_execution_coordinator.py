"""RTM-5: PaperExecutionCoordinator 테스트.

unit 계층은 *실제* TriggerEngine 과 fake bridge/position_source 로 coordinator 의
fail-closed/suppressed 분기를 검증한다(fake bridge.dispatch 가 호출되지 않으면
journal/broker 가 0 임을 의미한다).

integration 계층은 실제 TriggerEngine + TriggerOrderBridge + OrderIntentGenerator +
QuantityResolver + PaperBrokerAdapter + SQLiteLedger(tmp_path) + SqliteTriggerJournal(
tmp_path) 로 발화→주문 전 경로와 멱등성/재시작/식별자 정합을 검증한다.

모든 DB 는 tmp_path 이며 runtime/paper 경로를 건드리지 않는다. KIS/live/network/scheduler
없음.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import AnalysisAction
from config.settings import ExecutionMode
from domain import Currency, Money, Percent
from domain.enums import (
    AccountRole,
    AssetClass,
    Market,
    OrderSide,
    OrderStatus,
    OrderType,
)
from domain.market import MarketPrice
from domain.order import OrderIntent, OrderResult
from domain.position import CashSnapshot, Position
from market_data.conditions import Comparator, ConditionClause, Metric
from market_data.indicators import IndicatorContext
from market_data.latest_state import LatestMarketStateSnapshot
from market_data.models import (
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)
from market_data.trigger_engine import (
    DecisionTriggerBundle,
    TradingPermission,
    TriggerEngine,
    TriggerPlan,
    TriggerStatus,
)

from broker.paper_broker import PaperBrokerAdapter
from execution.paper_execution_coordinator import (
    REASON_CONTEXT_MARKET_MISMATCH,
    REASON_DECISION_REPLACE_CONFLICT,
    REASON_DECISION_REPLACE_OLDER,
    REASON_INDICATOR_AS_OF_MISMATCH,
    REASON_POSITION_IDENTITY_MISMATCH,
    REASON_SNAPSHOT_AS_OF_MISMATCH,
    REASON_SNAPSHOT_IDENTITY_MISMATCH,
    CoordinatorStatus,
    PaperExecutionCoordinator,
)
from execution.sqlite_trigger_journal import SqliteTriggerJournal
from execution.trigger_journal import JournalState
from execution.trigger_order_bridge import (
    BridgeOutcome,
    BridgeResult,
    TriggerOrderBridge,
)
from ledger.sqlite_ledger import SQLiteLedger
from paper_loop import QuantityResolver
from risk import OrderIntentGenerator


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
_PRICE = "70000"
_THRESHOLD = "100000"


# ---------------------------------------------------------------- market builders
def _quote(*, symbol: str = "005930", price: str = _PRICE, at: datetime = NOW) -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider="kis", symbol=symbol, market=Market.KR, currency=Currency.KRW,
        bid_price=price, ask_price=price, bid_quantity="10", ask_quantity="10",
        quote_at=at, received_at=at,
        provider_sequence=ProviderSequence(provider="kis", channel="q", sequence=1, received_at=at),
    )


def _trade(*, symbol: str = "005930", price: str = _PRICE, at: datetime = NOW) -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="kis", symbol=symbol, market=Market.KR, currency=Currency.KRW,
        price=price, quantity="1", trade_at=at, received_at=at,
        provider_sequence=ProviderSequence(provider="kis", channel="t", sequence=1, received_at=at),
    )


def _snap(
    *,
    symbol: str = "005930",
    trade: NormalizedTradeTick | None = None,
    quote: NormalizedBestBidAsk | None = None,
    evaluated_at: datetime = NOW,
    with_quote: bool = True,
) -> LatestMarketStateSnapshot:
    return LatestMarketStateSnapshot(
        market=Market.KR,
        symbol=symbol,
        trade=trade if trade is not None else _trade(symbol=symbol, at=evaluated_at),
        quote=(quote if quote is not None else _quote(symbol=symbol, at=evaluated_at)) if with_quote else None,
        trade_fresh=True,
        quote_fresh=True,
        evaluated_at=evaluated_at,
    )


def _permission(
    *,
    allowed: bool = True,
    market: Market = Market.KR,
    checked_at: datetime = NOW,
    valid_until: datetime | None = None,
) -> TradingPermission:
    return TradingPermission(
        market=market,
        allowed=allowed,
        checked_at=checked_at,
        valid_until=valid_until if valid_until is not None else NOW + DAY,
        reason_code="open",
    )


def _bundle(
    factory,
    *,
    action: AnalysisAction = AnalysisAction.BUY,
    symbol: str = "005930",
    correlation_id: str = "idem-1",
    current_symbol_market_value: str = "3000000",
):
    """factory 로 RiskFilterInput 을 만들고 동일 AnalysisDecision 으로 bundle 을 조립한다."""
    ri = factory(
        action=action,
        target_weight_percent=Percent("4"),
        symbol=symbol,
        correlation_id=correlation_id,
        context_overrides={
            "current_symbol_market_value": Money.from_str(current_symbol_market_value, Currency.KRW),
            "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
        },
    )
    analysis = ri.analysis_decision
    plan = TriggerPlan(
        plan_id="plan-1",
        decision_id=analysis.decision_id,
        created_at=NOW,
        valid_from=NOW,
        expires_at=NOW + DAY,
        universe=analysis.universe,
        market=Market.KR,
        symbol=analysis.symbol,
        action=action,
        rules=(ConditionClause(metric=Metric.LAST_TRADE_PRICE, comparator=Comparator.LTE, threshold=_THRESHOLD),),
    )
    bundle = DecisionTriggerBundle(decision=analysis, plan=plan)
    return ri, bundle


# ----------------------------------------------------------------------- fakes
class _FakeBridge:
    """dispatch 호출을 기록하고 미리 구성한 BridgeResult 를 반환한다."""

    def __init__(self, *, outcome: BridgeOutcome, order_result: OrderResult | None = None) -> None:
        self._result = BridgeResult(outcome, None, None, order_result)  # type: ignore[arg-type]
        self.dispatch_calls: list[dict[str, object]] = []
        self.reconcile_calls = 0

    def dispatch(self, **kwargs: object) -> BridgeResult:
        self.dispatch_calls.append(kwargs)
        return self._result

    def reconcile_all(self, *, now: datetime) -> tuple[BridgeResult, ...]:
        self.reconcile_calls += 1
        return ()


@dataclass
class _FakePositionSource:
    position: Position | None = None

    def get_position(self, symbol: str, market: Market, account_role: AccountRole) -> Position | None:
        return self.position


def _coordinator(engine: TriggerEngine, bridge, position_source) -> PaperExecutionCoordinator:
    return PaperExecutionCoordinator(engine=engine, bridge=bridge, position_source=position_source)


def _ok_bridge() -> _FakeBridge:
    return _FakeBridge(
        outcome=BridgeOutcome.COMMITTED,
        order_result=OrderResult(
            order_id="order-analysis-260522-001",
            status=OrderStatus.FILLED,
            accepted=True,
            created_at=NOW,
        ),
    )


# ===================================================================== unit: fail-closed
def test_stale_snapshot_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(evaluated_at=NOW + timedelta(seconds=1)),  # != now
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_SNAPSHOT_AS_OF_MISMATCH
    assert bridge.dispatch_calls == []


def test_stale_indicator_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    indicators = IndicatorContext(
        market=Market.KR, symbol="005930", windows=(), evaluated_at=NOW + timedelta(seconds=5)
    )
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(),
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
        indicators=indicators,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_INDICATOR_AS_OF_MISMATCH
    assert bridge.dispatch_calls == []


def test_snapshot_identity_mismatch_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(symbol="000660"),  # plan.symbol == 005930
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_SNAPSHOT_IDENTITY_MISMATCH
    assert bridge.dispatch_calls == []


def test_context_market_mismatch_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    mismatched_ctx = ri.context.model_copy(update={"market": Market.US})
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(),
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=mismatched_ctx,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_CONTEXT_MARKET_MISMATCH
    assert bridge.dispatch_calls == []


def test_replace_bundle_older_fails_closed(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    engine = TriggerEngine()
    # 더 새로운 결정으로 먼저 무장시킨다(다음 process 의 bundle 이 더 오래된 것이 되도록).
    _, newer = _bundle(sample_risk_input_factory)
    newer_decision = newer.decision.model_copy(update={"created_at": NOW + DAY})
    newer_plan = newer.plan.model_copy(
        update={"created_at": NOW + DAY, "valid_from": NOW + DAY, "expires_at": NOW + 2 * DAY}
    )
    engine.replace_bundle(
        DecisionTriggerBundle(decision=newer_decision, plan=newer_plan), now=NOW + DAY
    )
    bridge = _ok_bridge()
    coord = _coordinator(engine, bridge, _FakePositionSource())
    result = coord.process(
        bundle=bundle,  # older created_at
        snapshot=_snap(),
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_DECISION_REPLACE_OLDER
    assert bridge.dispatch_calls == []


def test_replace_bundle_conflict_fails_closed(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-A")
    engine = TriggerEngine()
    engine.replace_bundle(bundle, now=NOW)
    # 같은 시각, 다른 decision_id → REJECTED_CONFLICT.
    ri2, bundle2 = _bundle(sample_risk_input_factory, correlation_id="idem-B")
    conflicting_decision = bundle2.decision.model_copy(
        update={"decision_id": bundle2.decision.decision_id.__class__("analysis-260522-999")}
    )
    conflicting_plan = bundle2.plan.model_copy(
        update={"decision_id": conflicting_decision.decision_id}
    )
    conflicting = DecisionTriggerBundle(decision=conflicting_decision, plan=conflicting_plan)
    bridge = _ok_bridge()
    coord = _coordinator(engine, bridge, _FakePositionSource())
    result = coord.process(
        bundle=conflicting,
        snapshot=_snap(),
        permission=_permission(),
        allocator_decision=ri2.allocator_decision,
        risk_context=ri2.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_DECISION_REPLACE_CONFLICT
    assert bridge.dispatch_calls == []


def test_position_identity_mismatch_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    wrong_position = Position(
        symbol="000660", market=Market.KR, asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER, quantity=Decimal("10"), avg_cost=Decimal("70000"),
        currency=Currency.KRW,
    )
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource(position=wrong_position))
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(),
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_POSITION_IDENTITY_MISMATCH
    assert result.signal is not None  # 발화는 됐으나 sizing 전에 차단
    assert bridge.dispatch_calls == []


# ===================================================================== unit: suppressed
def test_condition_not_met_suppressed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    # trade price 가 threshold(100000) 보다 높아 LTE 조건 false.
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(trade=_trade(price="200000"), quote=_quote(price="200000")),
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.SUPPRESSED
    assert result.trigger_status is not TriggerStatus.TRIGGERED
    assert bridge.dispatch_calls == []


def test_permission_not_allowed_suppressed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(),
        permission=_permission(allowed=False),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.SUPPRESSED
    assert bridge.dispatch_calls == []


def test_permission_expired_suppressed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(),
        permission=_permission(checked_at=NOW - DAY, valid_until=NOW - timedelta(hours=1)),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.SUPPRESSED
    assert bridge.dispatch_calls == []


def test_permission_not_yet_valid_suppressed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(),
        permission=_permission(checked_at=NOW + timedelta(hours=1), valid_until=NOW + DAY),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.SUPPRESSED
    assert bridge.dispatch_calls == []


def test_quote_missing_suppressed_no_dispatch(sample_risk_input_factory) -> None:
    # quote 가 없으면 engine 이 MISSING_QUOTE 로 SUPPRESS → broker 0.
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(with_quote=False),
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.SUPPRESSED
    assert bridge.dispatch_calls == []


# ===================================================================== unit: dispatch path
def test_triggered_dispatches_with_ask_market_price(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(),
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.COMMITTED
    assert result.order_result is not None
    assert result.order_result.status is OrderStatus.FILLED
    assert len(bridge.dispatch_calls) == 1
    call = bridge.dispatch_calls[0]
    mp = call["market_price"]
    assert isinstance(mp, MarketPrice)
    assert mp.price == Decimal(_PRICE)  # BUY → ask
    assert mp.symbol == "005930"
    # coordinator 가 구성한 risk_input 의 correlation_id == signal.idempotency_key.
    risk_input = call["risk_input"]
    assert risk_input.correlation_id == result.signal.idempotency_key
    assert risk_input.analysis_decision is bundle.decision


def test_recover_delegates_to_bridge(sample_risk_input_factory) -> None:
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge, _FakePositionSource())
    out = coord.recover(now=NOW)
    assert out == ()
    assert bridge.reconcile_calls == 1


# ===================================================================== integration helpers
def _real_stack(tmp_path: Path):
    ledger = SQLiteLedger(tmp_path / "ledger.sqlite3")
    broker = PaperBrokerAdapter(
        ledger,
        initial_cash=CashSnapshot(
            currency=Currency.KRW, amount=Decimal("100000000"),
            account_role=AccountRole.PAPER, as_of=NOW,
        ),
    )
    journal = SqliteTriggerJournal(tmp_path / "journal.sqlite3")
    bridge = TriggerOrderBridge(
        journal=journal,
        generator=OrderIntentGenerator(),
        resolver=QuantityResolver(),
        broker=broker,
        ledger=ledger,
    )
    engine = TriggerEngine()
    coord = PaperExecutionCoordinator(engine=engine, bridge=bridge, position_source=broker)
    return coord, engine, bridge, broker, ledger, journal


def _seed_position(broker: PaperBrokerAdapter, *, quantity: str, symbol: str = "005930") -> None:
    """coordinator 경로 밖에서 broker 에 포지션을 심는다(SELL 경로 사전 조건)."""
    intent = OrderIntent(
        order_id=f"seed-{symbol}",
        correlation_id="seed",
        symbol=symbol,
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        quantity=Decimal(quantity),
        source_decision_id="seed",
        created_at=NOW - timedelta(hours=1),
    )
    out = broker.submit_order(
        intent,
        MarketPrice(symbol=symbol, market=Market.KR, currency=Currency.KRW, price=Decimal(_PRICE), as_of=NOW - timedelta(hours=1)),
    )
    assert out.status is OrderStatus.FILLED


# ===================================================================== integration tests
def test_integration_trigger_false_no_order(tmp_path: Path, sample_risk_input_factory) -> None:
    coord, _, _, broker, ledger, journal = _real_stack(tmp_path)
    ri, bundle = _bundle(sample_risk_input_factory)
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(trade=_trade(price="200000"), quote=_quote(price="200000")),
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.SUPPRESSED
    order_id = f"order-{bundle.decision.decision_id.value}"
    assert ledger.get_order_result(order_id) is None
    assert journal.get(bundle.decision.decision_id.value) is None  # idempotency_key 미사용


def test_integration_valid_buy_filled_db_invariants(tmp_path: Path, sample_risk_input_factory) -> None:
    coord, _, _, broker, ledger, journal = _real_stack(tmp_path)
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-buy")
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(),
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.COMMITTED
    assert result.order_result.status is OrderStatus.FILLED
    order_id = f"order-{bundle.decision.decision_id.value}"
    # journal: 정확히 하나의 COMMITTED 행, order_id 일치, correlation_id==idempotency_key.
    record = journal.get(result.signal.idempotency_key)
    assert record is not None
    assert record.state is JournalState.COMMITTED
    assert record.result_status == "FILLED"
    assert record.order_id == order_id
    # ledger: 정확히 하나의 order result + 하나의 fill.
    assert ledger.get_order_result(order_id).status is OrderStatus.FILLED
    assert ledger.get_fill_by_order_id(order_id) is not None
    # 포지션이 생기고 현금이 줄었다.
    position = broker.get_position("005930", Market.KR, AccountRole.PAPER)
    assert position is not None and position.quantity > 0
    cash = broker.get_cash(Currency.KRW, AccountRole.PAPER)
    assert cash.amount < Decimal("100000000")


def test_integration_same_signal_reprocess_no_increase(tmp_path: Path, sample_risk_input_factory) -> None:
    coord, _, broker_bridge, broker, ledger, journal = _real_stack(tmp_path)
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-dup")
    first = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, risk_context=ri.context, now=NOW,
    )
    assert first.status is CoordinatorStatus.COMMITTED
    order_id = f"order-{bundle.decision.decision_id.value}"
    qty_after_first = broker.get_position("005930", Market.KR, AccountRole.PAPER).quantity

    # 같은 engine 인스턴스 재처리 → engine 이 ALREADY_FIRED 로 SUPPRESS(발화 상태기계 멱등성).
    # bridge/journal 에 도달하지 않으며 추가 체결도 없다(journal terminal skip 은 재시작 테스트가 검증).
    second = coord.process(
        bundle=bundle, snapshot=_snap(evaluated_at=NOW + timedelta(seconds=1)),
        permission=_permission(), allocator_decision=ri.allocator_decision,
        risk_context=ri.context, now=NOW + timedelta(seconds=1),
    )
    assert second.status is CoordinatorStatus.SUPPRESSED
    assert second.trigger_status is TriggerStatus.ALREADY_FIRED
    qty_after_second = broker.get_position("005930", Market.KR, AccountRole.PAPER).quantity
    assert qty_after_second == qty_after_first
    assert ledger.get_fill_by_order_id(order_id) is not None


def test_integration_restart_reopen_same_db_no_duplicate(tmp_path: Path, sample_risk_input_factory) -> None:
    coord1, _, _, broker1, ledger1, journal1 = _real_stack(tmp_path)
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-restart")
    first = coord1.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, risk_context=ri.context, now=NOW,
    )
    assert first.status is CoordinatorStatus.COMMITTED
    order_id = f"order-{bundle.decision.decision_id.value}"
    qty1 = broker1.get_position("005930", Market.KR, AccountRole.PAPER).quantity

    # 같은 DB 파일을 새 객체로 다시 연다(프로세스 재시작 시뮬레이션). engine 은 새 in-memory.
    ledger2 = SQLiteLedger(tmp_path / "ledger.sqlite3")
    broker2 = PaperBrokerAdapter(ledger2)  # 재seed 금지(기존 현금 유지)
    journal2 = SqliteTriggerJournal(tmp_path / "journal.sqlite3")
    bridge2 = TriggerOrderBridge(
        journal=journal2, generator=OrderIntentGenerator(), resolver=QuantityResolver(),
        broker=broker2, ledger=ledger2,
    )
    coord2 = PaperExecutionCoordinator(
        engine=TriggerEngine(), bridge=bridge2, position_source=broker2
    )
    again = coord2.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, risk_context=ri.context, now=NOW,
    )
    assert again.status is CoordinatorStatus.SKIPPED_TERMINAL
    qty2 = broker2.get_position("005930", Market.KR, AccountRole.PAPER).quantity
    assert qty2 == qty1  # 중복 체결 없음
    assert ledger2.get_order_result(order_id).status is OrderStatus.FILLED


def test_integration_sizing_noop_aborts(tmp_path: Path, sample_risk_input_factory) -> None:
    # 이미 target(4% of 100M = 4M)을 초과하는 포지션 보유 → BUY delta 0 → NOOP → ABORTED.
    coord, _, _, broker, ledger, journal = _real_stack(tmp_path)
    _seed_position(broker, quantity="100")  # 100*70000 = 7M > 4M
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-noop")
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, risk_context=ri.context, now=NOW,
    )
    assert result.status is CoordinatorStatus.TRIGGERED_ABORTED
    assert result.reason_code == "no_executable_quantity"
    order_id = f"order-{bundle.decision.decision_id.value}"
    assert ledger.get_order_result(order_id) is None  # 주문 미생성
    assert journal.get(result.signal.idempotency_key).state is JournalState.ABORTED


def test_integration_valid_sell_filled(tmp_path: Path, sample_risk_input_factory) -> None:
    # 100주 보유(7M) 상태에서 target 4%(4M)로 줄이는 SELL → 3M 매도 → 42주 FILLED.
    coord, _, _, broker, ledger, journal = _real_stack(tmp_path)
    _seed_position(broker, quantity="100")
    ri, bundle = _bundle(sample_risk_input_factory, action=AnalysisAction.SELL, correlation_id="idem-sell")
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, risk_context=ri.context, now=NOW,
    )
    assert result.status is CoordinatorStatus.COMMITTED
    assert result.order_result.status is OrderStatus.FILLED
    position = broker.get_position("005930", Market.KR, AccountRole.PAPER)
    assert position.quantity < Decimal("100")  # 일부 매도됨


def test_integration_stale_snapshot_no_broker_write(tmp_path: Path, sample_risk_input_factory) -> None:
    coord, _, _, broker, ledger, journal = _real_stack(tmp_path)
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-stale")
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(evaluated_at=NOW + timedelta(seconds=2)),
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        risk_context=ri.context,
        now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    order_id = f"order-{bundle.decision.decision_id.value}"
    assert ledger.get_order_result(order_id) is None
    assert journal.get("idem-stale") is None


def test_integration_position_identity_mismatch_no_broker_write(
    tmp_path: Path, sample_risk_input_factory
) -> None:
    # real bridge + 잘못된 종목을 돌려주는 position_source → broker write 전에 차단.
    ledger = SQLiteLedger(tmp_path / "ledger.sqlite3")
    broker = PaperBrokerAdapter(
        ledger,
        initial_cash=CashSnapshot(
            currency=Currency.KRW, amount=Decimal("100000000"),
            account_role=AccountRole.PAPER, as_of=NOW,
        ),
    )
    journal = SqliteTriggerJournal(tmp_path / "journal.sqlite3")
    bridge = TriggerOrderBridge(
        journal=journal, generator=OrderIntentGenerator(), resolver=QuantityResolver(),
        broker=broker, ledger=ledger,
    )
    wrong_position = Position(
        symbol="000660", market=Market.KR, asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER, quantity=Decimal("10"), avg_cost=Decimal("70000"),
        currency=Currency.KRW,
    )
    coord = PaperExecutionCoordinator(
        engine=TriggerEngine(), bridge=bridge, position_source=_FakePositionSource(position=wrong_position)
    )
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-posid")
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, risk_context=ri.context, now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_POSITION_IDENTITY_MISMATCH
    order_id = f"order-{bundle.decision.decision_id.value}"
    assert ledger.get_order_result(order_id) is None
    assert journal.get("idem-posid") is None


def test_integration_recover_reserved_aborts(tmp_path: Path, sample_risk_input_factory) -> None:
    # 미종결 RESERVED 행을 만들어 둔 뒤 recover → ABORTED(restart_before_dispatch). 자동 재주문 없음.
    coord, _, _, broker, ledger, journal = _real_stack(tmp_path)

    @dataclass(frozen=True)
    class _ReserveSignal:
        idempotency_key: str = "idem-recover"
        trigger_id: str = "trig-r"
        decision_id: str = "analysis-260522-001"
        plan_id: str = "plan-1"
        market: str = "KR"
        symbol: str = "005930"
        action: str = "buy"
        triggered_at: datetime = NOW

    journal.reserve(_ReserveSignal(), NOW)
    results = coord.recover(now=NOW + timedelta(seconds=10))
    assert len(results) == 1
    assert results[0].outcome is BridgeOutcome.ABORTED
    assert journal.get("idem-recover").state is JournalState.ABORTED
    order_id = f"order-analysis-260522-001"
    assert ledger.get_order_result(order_id) is None
