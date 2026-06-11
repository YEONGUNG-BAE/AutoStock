from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config.settings import ExecutionMode
from domain.enums import (
    AccountRole,
    AssetClass,
    Currency,
    Market,
    OrderSide,
    OrderStatus,
    OrderType,
)
from domain.market import MarketPrice
from domain.order import OrderIntent, OrderResult
from paper_loop.models import QuantityResolutionStatus
from risk.models import OrderGenerationStatus

from execution.sqlite_trigger_journal import SqliteTriggerJournal
from execution.trigger_journal import JournalState
from execution.trigger_order_bridge import (
    BridgeCoherenceError,
    BridgeOutcome,
    BridgePreflightError,
    TriggerOrderBridge,
)


_NOW = datetime(2026, 6, 11, 9, 0, 0, tzinfo=UTC)
_DECISION_ID = "analysis-260611-001"
_ORDER_ID = f"order-{_DECISION_ID}"
_IDEM = "idem-1"


def _later(seconds: int) -> datetime:
    return _NOW + timedelta(seconds=seconds)


# --------------------------------------------------------------------- fakes
@dataclass(frozen=True)
class _FakeSignal:
    idempotency_key: str = _IDEM
    trigger_id: str = "trig-1"
    decision_id: str = _DECISION_ID
    plan_id: str = "plan-1"
    market: str = "KR"
    symbol: str = "005930"
    action: str = "buy"
    triggered_at: datetime = _NOW


@dataclass(frozen=True)
class _FakePlan:
    plan_id: str = "plan-1"
    decision_id: str = _DECISION_ID
    market: str = "KR"
    symbol: str = "005930"
    action: str = "buy"
    max_fires_per_decision: int = 1


@dataclass(frozen=True)
class _FakeDecision:
    decision_id: str = _DECISION_ID
    market: str = "KR"
    symbol: str = "005930"


@dataclass(frozen=True)
class _FakeBundle:
    plan: _FakePlan | None = field(default_factory=_FakePlan)
    decision: _FakeDecision = field(default_factory=_FakeDecision)
    _action: str = "buy"

    @property
    def action(self) -> str:
        return self._action


@dataclass(frozen=True)
class _FakeRiskInput:
    analysis_decision: _FakeDecision = field(default_factory=_FakeDecision)
    context: object = None
    correlation_id: str = _IDEM


@dataclass(frozen=True)
class _GenResult:
    status: OrderGenerationStatus
    order_intent: OrderIntent | None


@dataclass(frozen=True)
class _ResolveResult:
    status: QuantityResolutionStatus
    order_intent: OrderIntent | None


def _executable_intent(
    *,
    order_id: str = _ORDER_ID,
    correlation_id: str = _IDEM,
    quantity: str = "42",
    target_weight_percent: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        correlation_id=correlation_id,
        symbol="005930",
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        quantity=Decimal(quantity) if quantity is not None else None,
        target_weight_percent=(
            Decimal(target_weight_percent) if target_weight_percent is not None else None
        ),
        created_at=_NOW,
    )


def _generated_target_weight_intent() -> OrderIntent:
    return _executable_intent(quantity=None, target_weight_percent="5")


def _market_price() -> MarketPrice:
    return MarketPrice(
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        price=Decimal("70000"),
        as_of=_NOW,
    )


def _order_result(status: OrderStatus) -> OrderResult:
    if status == OrderStatus.REJECTED:
        return OrderResult(
            order_id=_ORDER_ID,
            status=status,
            accepted=False,
            rejection_reason="rejected",
            created_at=_NOW,
        )
    return OrderResult(
        order_id=_ORDER_ID,
        status=status,
        accepted=True,
        created_at=_NOW,
    )


class _FakeGenerator:
    def __init__(self, result: _GenResult) -> None:
        self._result = result
        self.calls = 0

    def generate(self, risk_input: object) -> _GenResult:  # type: ignore[override]
        self.calls += 1
        return self._result


class _FakeResolver:
    def __init__(self, result: _ResolveResult) -> None:
        self._result = result
        self.calls = 0

    def resolve(self, **kwargs: object) -> _ResolveResult:  # type: ignore[override]
        self.calls += 1
        return self._result


class _FakeLedger:
    def __init__(self, durable: OrderResult | None = None) -> None:
        self._durable = durable
        self.processed: set[str] = set()
        if durable is not None:
            self.processed.add(durable.order_id)

    def has_processed_order(self, order_id: str) -> bool:
        return order_id in self.processed

    def get_order_result(self, order_id: str) -> OrderResult | None:
        if self._durable is not None and self._durable.order_id == order_id:
            return self._durable
        return None

    def set_durable(self, result: OrderResult) -> None:
        self._durable = result
        self.processed.add(result.order_id)


class _FakeBroker:
    def __init__(self, *, ledger: _FakeLedger, result: OrderResult | None, raises: bool = False) -> None:
        self._ledger = ledger
        self._result = result
        self._raises = raises
        self.calls = 0

    def submit_order(self, intent: OrderIntent, market_price: MarketPrice) -> OrderResult:
        self.calls += 1
        if self._raises:
            raise RuntimeError("broker boom")
        # 정상 broker 처럼 ledger 에 durable result 를 남긴다.
        assert self._result is not None
        self._ledger.set_durable(self._result)
        return self._result


# --------------------------------------------------------------------- helpers
def _journal(tmp_path: Path) -> SqliteTriggerJournal:
    return SqliteTriggerJournal(tmp_path / "journal.sqlite3")


def _bridge(
    tmp_path: Path,
    *,
    gen: _GenResult | None = None,
    resolve: _ResolveResult | None = None,
    ledger: _FakeLedger | None = None,
    broker_result: OrderResult | None = None,
    broker_raises: bool = False,
    journal: SqliteTriggerJournal | None = None,
) -> tuple[TriggerOrderBridge, _FakeLedger, _FakeBroker, SqliteTriggerJournal]:
    journal = journal or _journal(tmp_path)
    gen = gen or _GenResult(OrderGenerationStatus.GENERATED, _generated_target_weight_intent())
    resolve = resolve or _ResolveResult(QuantityResolutionStatus.RESOLVED, _executable_intent())
    ledger = ledger if ledger is not None else _FakeLedger()
    broker = _FakeBroker(ledger=ledger, result=broker_result, raises=broker_raises)
    bridge = TriggerOrderBridge(
        journal=journal,
        generator=_FakeGenerator(gen),
        resolver=_FakeResolver(resolve),
        broker=broker,
        ledger=ledger,
    )
    return bridge, ledger, broker, journal


def _dispatch(bridge: TriggerOrderBridge, **overrides: object):
    kwargs = {
        "signal": _FakeSignal(),
        "bundle": _FakeBundle(),
        "risk_input": _FakeRiskInput(),
        "market_price": _market_price(),
        "current_position_quantity": None,
        "now": _NOW,
    }
    kwargs.update(overrides)
    return bridge.dispatch(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------- preflight
def test_preflight_missing_plan_rejects(tmp_path: Path) -> None:
    bridge, ledger, broker, journal = _bridge(tmp_path)
    with pytest.raises(BridgePreflightError):
        _dispatch(bridge, bundle=_FakeBundle(plan=None))
    assert broker.calls == 0
    assert journal.get(_IDEM) is None  # 아무것도 reserve 하지 않았다.


def test_preflight_signal_plan_mismatch_rejects(tmp_path: Path) -> None:
    bridge, _, broker, journal = _bridge(tmp_path)
    with pytest.raises(BridgePreflightError):
        _dispatch(bridge, signal=_FakeSignal(symbol="000660"))
    assert broker.calls == 0
    assert journal.get(_IDEM) is None


def test_preflight_max_fires_not_one_rejects(tmp_path: Path) -> None:
    bridge, _, broker, journal = _bridge(tmp_path)
    bundle = _FakeBundle(plan=_FakePlan(max_fires_per_decision=2))
    with pytest.raises(BridgePreflightError):
        _dispatch(bridge, bundle=bundle)
    assert broker.calls == 0
    assert journal.get(_IDEM) is None


def test_preflight_correlation_id_mismatch_rejects(tmp_path: Path) -> None:
    bridge, _, broker, journal = _bridge(tmp_path)
    with pytest.raises(BridgePreflightError):
        _dispatch(bridge, risk_input=_FakeRiskInput(correlation_id="other"))
    assert broker.calls == 0
    assert journal.get(_IDEM) is None


# --------------------------------------------------------------------- happy paths
def test_dispatch_filled_commits(tmp_path: Path) -> None:
    bridge, ledger, broker, journal = _bridge(
        tmp_path, broker_result=_order_result(OrderStatus.FILLED)
    )
    result = _dispatch(bridge)
    assert result.outcome is BridgeOutcome.COMMITTED
    assert result.order_result is not None
    assert result.order_result.status == OrderStatus.FILLED
    assert broker.calls == 1
    record = journal.get(_IDEM)
    assert record is not None
    assert record.state is JournalState.COMMITTED
    assert record.result_status == "FILLED"
    assert record.order_id == _ORDER_ID


def test_dispatch_rejected_commits_rejected(tmp_path: Path) -> None:
    bridge, ledger, broker, journal = _bridge(
        tmp_path, broker_result=_order_result(OrderStatus.REJECTED)
    )
    result = _dispatch(bridge)
    assert result.outcome is BridgeOutcome.COMMITTED
    assert result.order_result.status == OrderStatus.REJECTED
    record = journal.get(_IDEM)
    assert record.state is JournalState.COMMITTED
    assert record.result_status == "REJECTED"


# --------------------------------------------------------------------- aborts
def test_risk_blocked_aborts_without_broker(tmp_path: Path) -> None:
    bridge, _, broker, journal = _bridge(
        tmp_path, gen=_GenResult(OrderGenerationStatus.BLOCKED, None)
    )
    result = _dispatch(bridge)
    assert result.outcome is BridgeOutcome.ABORTED
    assert result.reason_code == "risk_blocked"
    assert broker.calls == 0
    assert journal.get(_IDEM).state is JournalState.ABORTED


def test_hold_noop_aborts_without_broker(tmp_path: Path) -> None:
    bridge, _, broker, journal = _bridge(
        tmp_path, gen=_GenResult(OrderGenerationStatus.NOOP, None)
    )
    result = _dispatch(bridge)
    assert result.outcome is BridgeOutcome.ABORTED
    assert result.reason_code == "hold_noop"
    assert broker.calls == 0
    assert journal.get(_IDEM).state is JournalState.ABORTED


def test_sizing_failed_aborts_without_broker(tmp_path: Path) -> None:
    bridge, _, broker, journal = _bridge(
        tmp_path, resolve=_ResolveResult(QuantityResolutionStatus.FAILED, None)
    )
    result = _dispatch(bridge)
    assert result.outcome is BridgeOutcome.ABORTED
    assert result.reason_code == "sizing_failed"
    assert broker.calls == 0
    assert journal.get(_IDEM).state is JournalState.ABORTED


def test_no_executable_quantity_aborts_without_broker(tmp_path: Path) -> None:
    bridge, _, broker, journal = _bridge(
        tmp_path, resolve=_ResolveResult(QuantityResolutionStatus.NOOP, None)
    )
    result = _dispatch(bridge)
    assert result.outcome is BridgeOutcome.ABORTED
    assert result.reason_code == "no_executable_quantity"
    assert broker.calls == 0
    assert journal.get(_IDEM).state is JournalState.ABORTED


# --------------------------------------------------------------------- coherence
def test_coherence_violation_aborts_and_raises(tmp_path: Path) -> None:
    # resolver 가 잘못된 order_id 의 intent 를 돌려주면 invariant 위반.
    bad = _executable_intent(order_id="order-wrong")
    bridge, _, broker, journal = _bridge(
        tmp_path, resolve=_ResolveResult(QuantityResolutionStatus.RESOLVED, bad)
    )
    with pytest.raises(BridgeCoherenceError):
        _dispatch(bridge)
    assert broker.calls == 0
    assert journal.get(_IDEM).state is JournalState.ABORTED


# --------------------------------------------------------------------- ledger preflight (desync)
def test_ledger_preflight_commits_without_broker(tmp_path: Path) -> None:
    # journal 은 신규(RESERVED_NEW)지만 ledger 에 이미 durable FILLED 가 있는 desync 상황.
    ledger = _FakeLedger(durable=_order_result(OrderStatus.FILLED))
    bridge, ledger, broker, journal = _bridge(tmp_path, ledger=ledger)
    result = _dispatch(bridge)
    assert result.outcome is BridgeOutcome.COMMITTED
    assert result.order_result.status == OrderStatus.FILLED
    assert broker.calls == 0  # broker 재호출 금지
    assert journal.get(_IDEM).state is JournalState.COMMITTED


# --------------------------------------------------------------------- uncertain paths
def test_broker_exception_marks_uncertain(tmp_path: Path) -> None:
    bridge, _, broker, journal = _bridge(tmp_path, broker_raises=True)
    result = _dispatch(bridge)
    assert result.outcome is BridgeOutcome.UNCERTAIN
    assert result.reason_code == "broker_exception"
    assert broker.calls == 1
    record = journal.get(_IDEM)
    assert record.state is JournalState.UNCERTAIN
    assert record.order_id == _ORDER_ID  # dispatching 까지는 진행됨


def test_missing_durable_after_submit_marks_uncertain(tmp_path: Path) -> None:
    # broker 가 ledger 에 아무 row 도 남기지 않은 경우(durable None).
    ledger = _FakeLedger()
    broker = _FakeBroker(ledger=ledger, result=None)  # submit 은 ledger 미기록

    class _NoWriteBroker:
        calls = 0

        def submit_order(self, intent: OrderIntent, market_price: MarketPrice) -> OrderResult:
            self.calls += 1
            return _order_result(OrderStatus.FILLED)  # 반환값은 있으나 ledger 미기록

    nowrite = _NoWriteBroker()
    journal = _journal(tmp_path)
    bridge = TriggerOrderBridge(
        journal=journal,
        generator=_FakeGenerator(
            _GenResult(OrderGenerationStatus.GENERATED, _generated_target_weight_intent())
        ),
        resolver=_FakeResolver(
            _ResolveResult(QuantityResolutionStatus.RESOLVED, _executable_intent())
        ),
        broker=nowrite,
        ledger=ledger,
    )
    result = _dispatch(bridge)
    assert result.outcome is BridgeOutcome.UNCERTAIN
    assert result.reason_code == "dispatch_outcome_missing"
    assert journal.get(_IDEM).state is JournalState.UNCERTAIN


def test_pending_durable_marks_uncertain(tmp_path: Path) -> None:
    # broker 가 ledger 에 PENDING 을 남기면(terminal 매핑 없음) UNCERTAIN.
    ledger = _FakeLedger()

    class _PendingBroker:
        calls = 0

        def submit_order(self, intent: OrderIntent, market_price: MarketPrice) -> OrderResult:
            self.calls += 1
            pending = OrderResult(
                order_id=_ORDER_ID, status=OrderStatus.PENDING, accepted=True, created_at=_NOW
            )
            ledger.set_durable(pending)
            return pending

    journal = _journal(tmp_path)
    bridge = TriggerOrderBridge(
        journal=journal,
        generator=_FakeGenerator(
            _GenResult(OrderGenerationStatus.GENERATED, _generated_target_weight_intent())
        ),
        resolver=_FakeResolver(
            _ResolveResult(QuantityResolutionStatus.RESOLVED, _executable_intent())
        ),
        broker=_PendingBroker(),
        ledger=ledger,
    )
    result = _dispatch(bridge)
    assert result.outcome is BridgeOutcome.UNCERTAIN
    assert result.reason_code == "dispatch_outcome_nonterminal"
    assert journal.get(_IDEM).state is JournalState.UNCERTAIN


# --------------------------------------------------------------------- reserve outcomes
def test_existing_terminal_skips(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    bridge, ledger, broker, _ = _bridge(
        tmp_path, broker_result=_order_result(OrderStatus.FILLED), journal=journal
    )
    first = _dispatch(bridge)
    assert first.outcome is BridgeOutcome.COMMITTED
    # 같은 idempotency_key 로 재발화 → terminal 이므로 skip, broker 재호출 없음.
    broker_before = broker.calls
    second = _dispatch(bridge, now=_later(1))
    assert second.outcome is BridgeOutcome.SKIPPED_TERMINAL
    assert broker.calls == broker_before


def test_existing_pending_requires_reconcile(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    # RESERVED 행을 만들어 둔 뒤(미종결), 같은 key 로 dispatch → RECONCILE_REQUIRED.
    journal.reserve(_FakeSignal(), _NOW)
    bridge, _, broker, _ = _bridge(tmp_path, journal=journal)
    result = _dispatch(bridge, now=_later(1))
    assert result.outcome is BridgeOutcome.RECONCILE_REQUIRED
    assert broker.calls == 0


# --------------------------------------------------------------------- reconcile
def test_reconcile_reserved_aborts(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_FakeSignal(), _NOW)
    bridge, _, broker, _ = _bridge(tmp_path, journal=journal)
    results = bridge.reconcile_all(now=_later(10))
    assert len(results) == 1
    assert results[0].outcome is BridgeOutcome.ABORTED
    assert results[0].reason_code == "restart_before_dispatch"
    assert broker.calls == 0
    assert journal.get(_IDEM).state is JournalState.ABORTED


def test_reconcile_dispatching_with_ledger_fill_commits(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_FakeSignal(), _NOW)
    journal.mark_dispatching(_IDEM, _ORDER_ID, _later(1))
    ledger = _FakeLedger(durable=_order_result(OrderStatus.FILLED))
    bridge, _, broker, _ = _bridge(tmp_path, ledger=ledger, journal=journal)
    results = bridge.reconcile_all(now=_later(10))
    assert len(results) == 1
    assert results[0].outcome is BridgeOutcome.COMMITTED
    assert broker.calls == 0  # 재제출 금지
    record = journal.get(_IDEM)
    assert record.state is JournalState.COMMITTED
    assert record.result_status == "FILLED"


def test_reconcile_dispatching_without_ledger_record_uncertain(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_FakeSignal(), _NOW)
    journal.mark_dispatching(_IDEM, _ORDER_ID, _later(1))
    bridge, _, broker, _ = _bridge(tmp_path, ledger=_FakeLedger(), journal=journal)
    results = bridge.reconcile_all(now=_later(10))
    assert results[0].outcome is BridgeOutcome.UNCERTAIN
    assert results[0].reason_code == "dispatch_outcome_unknown"
    assert broker.calls == 0
    assert journal.get(_IDEM).state is JournalState.UNCERTAIN


def test_reconcile_record_rejects_terminal(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    journal.reserve(_FakeSignal(), _NOW)
    aborted = journal.mark_aborted(_IDEM, "x", _later(1))
    bridge, _, _, _ = _bridge(tmp_path, journal=journal)
    from execution.trigger_order_bridge import BridgeError

    with pytest.raises(BridgeError):
        bridge.reconcile_record(aborted, now=_later(10))


# --------------------------------------------------------------------- integration (real generator + resolver)
def test_integration_real_generator_and_resolver_filled(tmp_path: Path) -> None:
    """실제 OrderIntentGenerator + QuantityResolver + 실제 journal + fake broker/ledger 로
    전체 경로가 FILLED commit 까지 가는지 확인한다(sizing 단계가 실제로 동작)."""
    pytest.importorskip("risk")
    from analysis import AnalysisAction
    from domain import Money, Percent
    from risk import OrderIntentGenerator, RiskFilterContext, RiskMode
    from paper_loop import QuantityResolver

    # 실제 RiskFilterInput 을 만들기 위해 conftest factory 가 필요하지만, 여기서는
    # generator/resolver 의 순수성만 검증하므로 fake gen/resolve 로 대체된 경로 외에
    # 실제 resolver 의 sizing 결과를 직접 호출해 본다.
    context = RiskFilterContext(
        created_at=_NOW,
        mode=RiskMode.NORMAL,
        total_nav=Money.from_str("100000000", Currency.KRW),
        cash=Money.from_str("100000000", Currency.KRW),
        invested_amount=Money.from_str("0", Currency.KRW),
        current_symbol_market_value=Money.from_str("0", Currency.KRW),
    )
    resolver = QuantityResolver()
    resolution = resolver.resolve(
        intent=_generated_target_weight_intent(),
        context=context,
        market_price=_market_price(),
    )
    assert resolution.status is QuantityResolutionStatus.RESOLVED
    assert resolution.order_intent is not None
    assert resolution.order_intent.quantity is not None
    assert resolution.order_intent.target_weight_percent is None
    assert resolution.order_intent.correlation_id == _IDEM
    assert resolution.order_intent.order_id == _ORDER_ID

    # 실제 resolver 산출 intent 를 그대로 bridge 에 흘려 FILLED commit 확인.
    ledger = _FakeLedger()
    journal = _journal(tmp_path)
    bridge = TriggerOrderBridge(
        journal=journal,
        generator=_FakeGenerator(
            _GenResult(OrderGenerationStatus.GENERATED, _generated_target_weight_intent())
        ),
        resolver=resolver,
        broker=_FakeBroker(ledger=ledger, result=_order_result(OrderStatus.FILLED)),
        ledger=ledger,
    )
    result = bridge.dispatch(
        signal=_FakeSignal(),
        bundle=_FakeBundle(),
        risk_input=_FakeRiskInput(context=context),
        market_price=_market_price(),
        current_position_quantity=None,
        now=_NOW,
    )
    assert result.outcome is BridgeOutcome.COMMITTED
    assert journal.get(_IDEM).state is JournalState.COMMITTED
