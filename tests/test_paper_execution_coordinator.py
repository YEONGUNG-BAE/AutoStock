"""RTM-7a: PaperExecutionCoordinator 테스트(canonical context + pre-fire 강화).

unit 계층은 *실제* TriggerEngine + fake bridge + 실제 PaperPortfolioContextService(fake
ledger/market source)로 coordinator 의 fail-closed/suppressed/dispatch 분기를 검증한다
(fake bridge.dispatch 미호출 = journal/broker 0).

integration 계층은 실제 TriggerEngine + TriggerOrderBridge + OrderIntentGenerator +
QuantityResolver + PaperBrokerAdapter + SQLiteLedger(tmp_path) + SqliteTriggerJournal(
tmp_path) + 실제 PaperPortfolioContextService(SQLiteLedger + fake market source)로
발화→주문 경로, canonical sizing, 멱등성/재시작, 그리고 *발화 전 의존성 실패가 fire budget 을
소비하지 않는지*(pre-fire retry 회귀)를 검증한다.

모든 DB 는 tmp_path 이며 runtime/paper 경로를 건드리지 않는다. KIS/live/network/scheduler 없음.
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import AnalysisAction
from config.settings import ExecutionMode
from domain import Currency, Percent
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
from market_data.models import NormalizedBestBidAsk, NormalizedTradeTick, ProviderSequence
from market_data.trigger_engine import (
    DecisionTriggerBundle,
    TradingPermission,
    TriggerEngine,
    TriggerPlan,
    TriggerStatus,
)
from risk.models import RiskMode

from broker.paper_broker import PaperBrokerAdapter
from execution.paper_execution_coordinator import (
    REASON_DECISION_REPLACE_CONFLICT,
    REASON_DECISION_REPLACE_OLDER,
    REASON_INDICATOR_AS_OF_MISMATCH,
    REASON_QUOTE_UNAVAILABLE,
    REASON_RISK_INPUT_BUILD_ERROR,
    REASON_SNAPSHOT_AS_OF_MISMATCH,
    REASON_SNAPSHOT_IDENTITY_MISMATCH,
    CoordinatorStatus,
    PaperExecutionCoordinator,
)
from execution.paper_portfolio_context import (
    REASON_CASH_MISSING,
    REASON_SNAPSHOT_STALE,
    PaperPortfolioContextService,
    PaperPortfolioPolicy,
)
from execution.sqlite_trigger_journal import SqliteTriggerJournal
from execution.trigger_journal import JournalState
from execution.trigger_order_bridge import BridgeOutcome, BridgeResult, TriggerOrderBridge
from ledger.sqlite_ledger import SQLiteLedger
from paper_loop import QuantityResolver
from risk import OrderIntentGenerator


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
_PRICE = "70000"
_THRESHOLD = "100000"
KRW = Currency.KRW


# ---------------------------------------------------------------- market builders
def _quote(*, symbol: str = "005930", price: str = _PRICE, at: datetime = NOW) -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider="kis", symbol=symbol, market=Market.KR, currency=KRW,
        bid_price=price, ask_price=price, bid_quantity="10", ask_quantity="10",
        quote_at=at, received_at=at,
        provider_sequence=ProviderSequence(provider="kis", channel="q", sequence=1, received_at=at),
    )


def _trade(*, symbol: str = "005930", price: str = _PRICE, at: datetime = NOW) -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="kis", symbol=symbol, market=Market.KR, currency=KRW,
        price=price, quantity="1", trade_at=at, received_at=at,
        provider_sequence=ProviderSequence(provider="kis", channel="t", sequence=1, received_at=at),
    )


def _snap(
    *,
    symbol: str = "005930",
    trade: NormalizedTradeTick | None = None,
    quote: NormalizedBestBidAsk | None = None,
    evaluated_at: datetime = NOW,
    quote_fresh: bool = True,
    with_quote: bool = True,
) -> LatestMarketStateSnapshot:
    return LatestMarketStateSnapshot(
        market=Market.KR,
        symbol=symbol,
        trade=trade if trade is not None else _trade(symbol=symbol, at=evaluated_at),
        quote=(quote if quote is not None else _quote(symbol=symbol, at=evaluated_at)) if with_quote else None,
        trade_fresh=True,
        quote_fresh=quote_fresh,
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
        market=market, allowed=allowed, checked_at=checked_at,
        valid_until=valid_until if valid_until is not None else NOW + DAY,
        reason_code="open",
    )


def _bundle(
    factory,
    *,
    action: AnalysisAction = AnalysisAction.BUY,
    symbol: str = "005930",
    correlation_id: str = "idem-1",
):
    """factory 로 allocator/analysis decision 을 만들고 동일 AnalysisDecision 으로 bundle 조립.

    RTM-7a 에서 RiskFilterContext 는 더 이상 주입되지 않는다(서비스가 ledger+스냅샷에서 계산).
    factory 의 context 는 사용하지 않고 allocator/analysis decision 만 취한다.
    """
    ri = factory(
        action=action,
        target_weight_percent=Percent("4"),
        symbol=symbol,
        correlation_id=correlation_id,
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


def _policy(**overrides: object) -> PaperPortfolioPolicy:
    base: dict[str, object] = {"mode": RiskMode.REBALANCING}
    base.update(overrides)
    return PaperPortfolioPolicy(**base)  # type: ignore[arg-type]


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
class _FakeLedger:
    cash: CashSnapshot | None = None
    positions: tuple[Position, ...] = ()
    raise_cash: bool = False

    def get_cash(self, currency: Currency, account_role: AccountRole) -> CashSnapshot | None:
        if self.raise_cash:
            raise RuntimeError("ledger unavailable")
        if self.cash is not None:
            return self.cash
        return CashSnapshot(currency=KRW, amount=Decimal("100000000"), account_role=AccountRole.PAPER, as_of=NOW)

    def list_positions(self) -> tuple[Position, ...]:
        return self.positions


@dataclass
class _FakeMarket:
    """평가 종목별 스냅샷을 보유하는 가변 fake. 기본은 now 시점 fresh 스냅샷."""

    snapshots: dict[tuple[str, Market], LatestMarketStateSnapshot | None] = field(default_factory=dict)

    def set(self, symbol: str, market: Market, snapshot: LatestMarketStateSnapshot | None) -> None:
        self.snapshots[(symbol, market)] = snapshot

    def get_snapshot(self, symbol: str, market: Market, *, now: datetime) -> LatestMarketStateSnapshot | None:
        if (symbol, market) in self.snapshots:
            return self.snapshots[(symbol, market)]
        return _snap(symbol=symbol, evaluated_at=now)


def _service(ledger=None, market=None) -> PaperPortfolioContextService:
    return PaperPortfolioContextService(
        ledger_source=ledger or _FakeLedger(),
        market_state_source=market or _FakeMarket(),
    )


def _coordinator(engine: TriggerEngine, bridge, service=None) -> PaperExecutionCoordinator:
    return PaperExecutionCoordinator(
        engine=engine, bridge=bridge, portfolio_context_service=service or _service()
    )


def _ok_bridge() -> _FakeBridge:
    return _FakeBridge(
        outcome=BridgeOutcome.COMMITTED,
        order_result=OrderResult(
            order_id="order-analysis-260522-001", status=OrderStatus.FILLED, accepted=True, created_at=NOW,
        ),
    )


# ===================================================================== unit: fail-closed (pre-context)
def test_stale_snapshot_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge)
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(evaluated_at=NOW + timedelta(seconds=1)),  # != now
        permission=_permission(),
        allocator_decision=ri.allocator_decision,
        portfolio_policy=_policy(),
        now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_SNAPSHOT_AS_OF_MISMATCH
    assert bridge.dispatch_calls == []


def test_stale_indicator_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge)
    indicators = IndicatorContext(
        market=Market.KR, symbol="005930", windows=(), evaluated_at=NOW + timedelta(seconds=5)
    )
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW, indicators=indicators,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_INDICATOR_AS_OF_MISMATCH
    assert bridge.dispatch_calls == []


def test_snapshot_identity_mismatch_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge)
    result = coord.process(
        bundle=bundle, snapshot=_snap(symbol="000660"), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_SNAPSHOT_IDENTITY_MISMATCH
    assert bridge.dispatch_calls == []


def test_quote_unavailable_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    # RTM-7a: 실행가격을 발화 전에 만들므로 quote 부재는 SUPPRESS 가 아니라 fail-closed.
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge)
    result = coord.process(
        bundle=bundle, snapshot=_snap(with_quote=False), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_QUOTE_UNAVAILABLE
    assert result.signal is None  # 발화 전 차단
    assert bridge.dispatch_calls == []


def test_portfolio_context_build_failure_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    # 서비스가 cash_missing 으로 fail → coordinator 가 그 reason_code 로 fail-closed(발화 전).
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    svc = _service(ledger=_FakeLedger(cash=None, raise_cash=False), market=_FakeMarket())
    # cash=None 분기를 강제하기 위해 get_cash 가 None 을 반환하도록 한다.
    svc = PaperPortfolioContextService(
        ledger_source=_NoCashLedger(), market_state_source=_FakeMarket()
    )
    coord = _coordinator(TriggerEngine(), bridge, svc)
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_CASH_MISSING
    assert result.signal is None  # 발화 전 차단(fire budget 미소비)
    assert bridge.dispatch_calls == []


@dataclass
class _NoCashLedger:
    def get_cash(self, currency: Currency, account_role: AccountRole) -> CashSnapshot | None:
        return None

    def list_positions(self) -> tuple[Position, ...]:
        return ()


def test_risk_input_build_error_fails_closed_no_dispatch(sample_risk_input_factory) -> None:
    # 잘못된 타입의 allocator_decision → RiskFilterInput 구조 검증이 실패 → typed 차단(발화 전).
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge)
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision="not-an-allocator-decision",  # type: ignore[arg-type]
        portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_RISK_INPUT_BUILD_ERROR
    assert result.signal is None  # 발화 전 차단
    assert bridge.dispatch_calls == []


def test_replace_bundle_older_fails_closed(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    engine = TriggerEngine()
    _, newer = _bundle(sample_risk_input_factory)
    newer_decision = newer.decision.model_copy(update={"created_at": NOW + DAY})
    newer_plan = newer.plan.model_copy(
        update={"created_at": NOW + DAY, "valid_from": NOW + DAY, "expires_at": NOW + 2 * DAY}
    )
    engine.replace_bundle(DecisionTriggerBundle(decision=newer_decision, plan=newer_plan), now=NOW + DAY)
    bridge = _ok_bridge()
    coord = _coordinator(engine, bridge)
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_DECISION_REPLACE_OLDER
    assert bridge.dispatch_calls == []


def test_replace_bundle_conflict_fails_closed(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-A")
    engine = TriggerEngine()
    engine.replace_bundle(bundle, now=NOW)
    ri2, bundle2 = _bundle(sample_risk_input_factory, correlation_id="idem-B")
    conflicting_decision = bundle2.decision.model_copy(
        update={"decision_id": bundle2.decision.decision_id.__class__("analysis-260522-999")}
    )
    conflicting_plan = bundle2.plan.model_copy(update={"decision_id": conflicting_decision.decision_id})
    conflicting = DecisionTriggerBundle(decision=conflicting_decision, plan=conflicting_plan)
    bridge = _ok_bridge()
    coord = _coordinator(engine, bridge)
    result = coord.process(
        bundle=conflicting, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri2.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.FAILED_CLOSED
    assert result.reason_code == REASON_DECISION_REPLACE_CONFLICT
    assert bridge.dispatch_calls == []


# ===================================================================== unit: suppressed
def test_condition_not_met_suppressed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge)
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(trade=_trade(price="200000"), quote=_quote(price="200000")),
        permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.SUPPRESSED
    assert result.trigger_status is not TriggerStatus.TRIGGERED
    assert bridge.dispatch_calls == []


def test_permission_not_allowed_suppressed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge)
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(allowed=False),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.SUPPRESSED
    assert bridge.dispatch_calls == []


def test_permission_expired_suppressed_no_dispatch(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge)
    result = coord.process(
        bundle=bundle, snapshot=_snap(),
        permission=_permission(checked_at=NOW - DAY, valid_until=NOW - timedelta(hours=1)),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.SUPPRESSED
    assert bridge.dispatch_calls == []


# ===================================================================== unit: dispatch path
def test_triggered_dispatches_with_ask_market_price_and_ledger_quantity(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge)
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
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
    risk_input = call["risk_input"]
    assert risk_input.correlation_id == result.signal.idempotency_key
    assert risk_input.analysis_decision is bundle.decision
    # canonical valuation 의 ledger truth(포지션 없음 → 0). None 을 흘려보내지 않는다.
    assert call["current_position_quantity"] == Decimal("0")


def test_recover_delegates_to_bridge(sample_risk_input_factory) -> None:
    bridge = _ok_bridge()
    coord = _coordinator(TriggerEngine(), bridge)
    out = coord.recover(now=NOW)
    assert out == ()
    assert bridge.reconcile_calls == 1


def test_process_signature_has_no_caller_context_injection() -> None:
    # 회귀: caller 가 NAV/cash/invested/weight 를 주입할 경로가 없어야 한다.
    params = set(inspect.signature(PaperExecutionCoordinator.process).parameters)
    assert "risk_context" not in params
    assert "portfolio_policy" in params
    assert "portfolio_context_service" not in params  # 생성자 주입이지 per-call 주입이 아니다


# ===================================================================== integration helpers
def _real_stack(tmp_path: Path, *, market: _FakeMarket | None = None, seed_cash: bool = True):
    ledger = SQLiteLedger(tmp_path / "ledger.sqlite3")
    broker = PaperBrokerAdapter(
        ledger,
        initial_cash=(
            CashSnapshot(currency=KRW, amount=Decimal("100000000"), account_role=AccountRole.PAPER, as_of=NOW)
            if seed_cash else None
        ),
    )
    journal = SqliteTriggerJournal(tmp_path / "journal.sqlite3")
    bridge = TriggerOrderBridge(
        journal=journal, generator=OrderIntentGenerator(), resolver=QuantityResolver(),
        broker=broker, ledger=ledger,
    )
    engine = TriggerEngine()
    market_source = market or _FakeMarket()
    service = PaperPortfolioContextService(ledger_source=ledger, market_state_source=market_source)
    coord = PaperExecutionCoordinator(engine=engine, bridge=bridge, portfolio_context_service=service)
    return coord, engine, bridge, broker, ledger, journal, market_source


def _seed_position(broker: PaperBrokerAdapter, *, quantity: str, symbol: str = "005930") -> None:
    intent = OrderIntent(
        order_id=f"seed-{symbol}", correlation_id="seed", symbol=symbol, market=Market.KR,
        asset_class=AssetClass.KR_EQUITY, account_role=AccountRole.PAPER, side=OrderSide.BUY,
        order_type=OrderType.MARKET, execution_mode=ExecutionMode.NORMAL, quantity=Decimal(quantity),
        source_decision_id="seed", created_at=NOW - timedelta(hours=1),
    )
    out = broker.submit_order(
        intent,
        MarketPrice(symbol=symbol, market=Market.KR, currency=KRW, price=Decimal(_PRICE), as_of=NOW - timedelta(hours=1)),
    )
    assert out.status is OrderStatus.FILLED


# ===================================================================== integration tests
def test_integration_trigger_false_no_order(tmp_path: Path, sample_risk_input_factory) -> None:
    coord, _, _, broker, ledger, journal, _ = _real_stack(tmp_path)
    ri, bundle = _bundle(sample_risk_input_factory)
    result = coord.process(
        bundle=bundle,
        snapshot=_snap(trade=_trade(price="200000"), quote=_quote(price="200000")),
        permission=_permission(), allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.SUPPRESSED
    order_id = f"order-{bundle.decision.decision_id.value}"
    assert ledger.get_order_result(order_id) is None
    assert journal.get(bundle.decision.decision_id.value) is None


def test_integration_empty_account_buy_filled_canonical_sizing(tmp_path: Path, sample_risk_input_factory) -> None:
    coord, _, _, broker, ledger, journal, market = _real_stack(tmp_path)
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-buy")
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.COMMITTED
    assert result.order_result.status is OrderStatus.FILLED
    order_id = f"order-{bundle.decision.decision_id.value}"
    record = journal.get(result.signal.idempotency_key)
    assert record is not None
    assert record.state is JournalState.COMMITTED
    assert record.result_status == "FILLED"
    assert record.order_id == order_id
    assert ledger.get_order_result(order_id).status is OrderStatus.FILLED
    assert ledger.get_fill_by_order_id(order_id) is not None
    # canonical sizing: 빈 계좌 + target 4% of 100M = 4M / 70000 = 57주(내림).
    position = broker.get_position("005930", Market.KR, AccountRole.PAPER)
    assert position.quantity == Decimal("57")
    cash = broker.get_cash(KRW, AccountRole.PAPER)
    assert cash.amount == Decimal("96010000")  # 100M - 57*70000
    # NAV 항등식: post-fill cash + 모든 mark == NAV(서비스 재계산으로 검증).
    val = PaperPortfolioContextService(
        ledger_source=ledger, market_state_source=market
    ).build_context(
        symbol="005930", market=Market.KR,
        proposed_price=MarketPrice(symbol="005930", market=Market.KR, currency=KRW, price=Decimal(_PRICE), as_of=NOW),
        policy=_policy(), now=NOW,
    )
    assert val.cash.amount == Decimal("96010000")
    assert val.invested_amount.amount == Decimal("3990000")  # 57 * 70000
    assert val.total_nav.amount == Decimal("100000000")  # 항등식


def test_integration_seeded_buy_respects_cumulative_cost(tmp_path: Path, sample_risk_input_factory) -> None:
    # 20주 보유 상태에서 target 4%(4M)까지 추가 매수. cumulative_after=4M ≤ 5% NAV cap → 통과.
    coord, _, _, broker, ledger, journal, _ = _real_stack(tmp_path)
    _seed_position(broker, quantity="20")  # 20*70000 = 1.4M
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-seedbuy")
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.COMMITTED
    # (4M - 1.4M)=2.6M / 70000 = 37주 추가 → 20+37 = 57.
    position = broker.get_position("005930", Market.KR, AccountRole.PAPER)
    assert position.quantity == Decimal("57")


def test_integration_seeded_sell_filled_canonical(tmp_path: Path, sample_risk_input_factory) -> None:
    # 100주 보유(7M)에서 target 4%(4M)로 축소 SELL → 42주 매도 → 58주 잔여.
    coord, _, _, broker, ledger, journal, _ = _real_stack(tmp_path)
    _seed_position(broker, quantity="100")
    ri, bundle = _bundle(sample_risk_input_factory, action=AnalysisAction.SELL, correlation_id="idem-sell")
    result = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert result.status is CoordinatorStatus.COMMITTED
    assert result.order_result.status is OrderStatus.FILLED
    position = broker.get_position("005930", Market.KR, AccountRole.PAPER)
    assert position.quantity == Decimal("58")


def test_integration_same_signal_reprocess_no_increase(tmp_path: Path, sample_risk_input_factory) -> None:
    coord, _, _, broker, ledger, journal, _ = _real_stack(tmp_path)
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-dup")
    first = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert first.status is CoordinatorStatus.COMMITTED
    order_id = f"order-{bundle.decision.decision_id.value}"
    qty_after_first = broker.get_position("005930", Market.KR, AccountRole.PAPER).quantity
    second = coord.process(
        bundle=bundle, snapshot=_snap(evaluated_at=NOW + timedelta(seconds=1)),
        permission=_permission(), allocator_decision=ri.allocator_decision,
        portfolio_policy=_policy(), now=NOW + timedelta(seconds=1),
    )
    assert second.status is CoordinatorStatus.SUPPRESSED
    assert second.trigger_status is TriggerStatus.ALREADY_FIRED
    qty_after_second = broker.get_position("005930", Market.KR, AccountRole.PAPER).quantity
    assert qty_after_second == qty_after_first
    assert ledger.get_fill_by_order_id(order_id) is not None


def test_integration_restart_reopen_same_db_no_duplicate(tmp_path: Path, sample_risk_input_factory) -> None:
    coord1, _, _, broker1, ledger1, journal1, _ = _real_stack(tmp_path)
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-restart")
    first = coord1.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert first.status is CoordinatorStatus.COMMITTED
    order_id = f"order-{bundle.decision.decision_id.value}"
    qty1 = broker1.get_position("005930", Market.KR, AccountRole.PAPER).quantity

    # 같은 DB 파일을 새 객체로 다시 연다(재시작 시뮬레이션). engine 은 새 in-memory.
    ledger2 = SQLiteLedger(tmp_path / "ledger.sqlite3")
    broker2 = PaperBrokerAdapter(ledger2)  # 재seed 금지(기존 현금 유지)
    journal2 = SqliteTriggerJournal(tmp_path / "journal.sqlite3")
    bridge2 = TriggerOrderBridge(
        journal=journal2, generator=OrderIntentGenerator(), resolver=QuantityResolver(),
        broker=broker2, ledger=ledger2,
    )
    service2 = PaperPortfolioContextService(ledger_source=ledger2, market_state_source=_FakeMarket())
    coord2 = PaperExecutionCoordinator(engine=TriggerEngine(), bridge=bridge2, portfolio_context_service=service2)
    again = coord2.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert again.status is CoordinatorStatus.SKIPPED_TERMINAL
    qty2 = broker2.get_position("005930", Market.KR, AccountRole.PAPER).quantity
    assert qty2 == qty1
    assert ledger2.get_order_result(order_id).status is OrderStatus.FILLED


def test_integration_stale_portfolio_mark_no_fire_then_retry_fills(tmp_path: Path, sample_risk_input_factory) -> None:
    # 핵심 회귀(pre-fire): 첫 tick 에 포트폴리오 mark 가 stale → 발화 전 fail-closed, broker 0,
    # fire budget 미소비. 둘째 tick 에 fresh → 같은 결정/엔진으로 정상 발화→체결.
    market = _FakeMarket()
    market.set("005930", Market.KR, _snap(quote_fresh=False))  # stale portfolio mark
    coord, engine, _, broker, ledger, journal, _ = _real_stack(tmp_path, market=market)
    ri, bundle = _bundle(sample_risk_input_factory, correlation_id="idem-retry")

    first = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert first.status is CoordinatorStatus.FAILED_CLOSED
    assert first.reason_code == REASON_SNAPSHOT_STALE
    assert first.signal is None
    order_id = f"order-{bundle.decision.decision_id.value}"
    assert ledger.get_order_result(order_id) is None  # broker 0
    assert journal.get("idem-retry") is None

    # mark 를 fresh 로 복구. 같은 engine/coord 로 재처리 → fire 가 소비되지 않았으므로 정상 발화.
    market.set("005930", Market.KR, _snap(quote_fresh=True))  # fresh portfolio mark
    second = coord.process(
        bundle=bundle, snapshot=_snap(), permission=_permission(),
        allocator_decision=ri.allocator_decision, portfolio_policy=_policy(), now=NOW,
    )
    assert second.status is CoordinatorStatus.COMMITTED
    assert second.order_result.status is OrderStatus.FILLED
    assert ledger.get_fill_by_order_id(order_id) is not None
    assert broker.get_position("005930", Market.KR, AccountRole.PAPER).quantity == Decimal("57")


def test_integration_recover_reserved_aborts(tmp_path: Path, sample_risk_input_factory) -> None:
    coord, _, _, broker, ledger, journal, _ = _real_stack(tmp_path)

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
    assert ledger.get_order_result("order-analysis-260522-001") is None
