"""RTM-7c.2 — fast-loop execution orchestrator unit tests (offline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from analysis import AnalysisAction
from domain import Currency, Percent
from domain.enums import Market
from domain.identifiers import DecisionId
from market_data.conditions import Comparator, ConditionClause, Metric
from market_data.health_policy import (
    HealthThresholds,
    HealthVerdict,
    MarketDataHealthStatus,
    MarketHealthTracker,
    TransportHealthStatus,
)
from market_data.latest_state import LatestMarketStateStore
from market_data.market_session import MarketSession, MarketSessionState
from market_data.models import MarketEventType, NormalizedBestBidAsk, NormalizedTradeTick, ProviderSequence
from market_data.monitor import AppliedMarketUpdate
from market_data.trigger_engine import (
    DecisionTriggerBundle,
    TriggerPlan,
    TriggerReason,
    TriggerSignal,
    TriggerStatus,
)
from orchestration.active_decision_store import ActiveBundle, PublicationError
from orchestration.execution_gate import ExecutionGateSnapshot, SessionHealthExecutionGate
from orchestration.fast_loop_execution import (
    FastLoopExecutionOrchestrator,
    FastLoopExecutionStatus,
    StaticExecutionInputsProvider,
)

from execution.paper_execution_coordinator import CoordinatorResult, CoordinatorStatus
from execution.paper_portfolio_context import PaperPortfolioPolicy
from risk.models import RiskMode

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
KRW = Currency.KRW
_PRICE = "70000"
_THRESHOLD = "100000"


def _update(**kwargs: Any) -> AppliedMarketUpdate:
    base = {
        "market": Market.KR,
        "symbol": "005930",
        "event_type": MarketEventType.TRADE,
        "provider": "kis",
        "channel": "t",
        "sequence": 1,
        "applied_at": NOW,
    }
    base.update(kwargs)
    return AppliedMarketUpdate(**base)


def _quote(at: datetime = NOW) -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider="kis", symbol="005930", market=Market.KR, currency=KRW,
        bid_price="70000", ask_price="70000", bid_quantity="10", ask_quantity="10",
        quote_at=at, received_at=at,
        provider_sequence=ProviderSequence(provider="kis", channel="q", sequence=1, received_at=at),
    )


def _trade(at: datetime = NOW) -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="kis", symbol="005930", market=Market.KR, currency=KRW,
        price="70000", quantity="1", trade_at=at, received_at=at,
        provider_sequence=ProviderSequence(provider="kis", channel="t", sequence=1, received_at=at),
    )


def _active_bundle(bundle: DecisionTriggerBundle, **kwargs: Any) -> ActiveBundle:
    base = {
        "publication_id": "pub-1",
        "market": "KR",
        "symbol": "005930",
        "decision_id": bundle.decision.decision_id.value,
        "plan_id": bundle.plan.plan_id if bundle.plan else None,
        "decision_created_at": bundle.decision.created_at,
        "valid_from": NOW,
        "expires_at": NOW + DAY,
        "bundle": bundle,
        "bundle_hash": "hash",
        "source_payload_hash": "src",
        "published_at": NOW,
    }
    base.update(kwargs)
    return ActiveBundle(**base)


def _bundle(sample_risk_input_factory) -> tuple[Any, DecisionTriggerBundle]:
    ri = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        correlation_id="idem-fl",
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
        action=AnalysisAction.BUY,
        rules=(
            ConditionClause(
                metric=Metric.LAST_TRADE_PRICE, comparator=Comparator.LTE, threshold=_THRESHOLD
            ),
        ),
    )
    return ri, DecisionTriggerBundle(decision=analysis, plan=plan)


class _FakeReader:
    def __init__(self, active: ActiveBundle | None = None, *, corrupt: bool = False) -> None:
        self._active = active
        self._corrupt = corrupt

    def read_active(self, market: Market | str, symbol: str) -> ActiveBundle | None:
        if self._corrupt:
            raise PublicationError("corrupt")
        return self._active


class _FakeCoordinator:
    def __init__(self, result: CoordinatorResult | None = None, *, boom: bool = False) -> None:
        self._result = result or CoordinatorResult(status=CoordinatorStatus.SUPPRESSED)
        self._boom = boom
        self.calls = 0

    def process(self, **kwargs: object) -> CoordinatorResult:
        self.calls += 1
        if self._boom:
            raise RuntimeError("coord boom")
        return self._result


class _FixedGate:
    def __init__(self, snapshot: ExecutionGateSnapshot | None = None, *, boom: bool = False) -> None:
        self._snapshot = snapshot
        self._boom = boom

    def evaluate(self, *, market: Market, now: datetime) -> ExecutionGateSnapshot:
        if self._boom:
            raise RuntimeError("gate boom")
        assert self._snapshot is not None
        return self._snapshot


def _open_gate(*, now: datetime = NOW, market: Market = Market.KR) -> ExecutionGateSnapshot:
    session = MarketSession(market=market, state=MarketSessionState.OPEN, as_of=now)
    health = HealthVerdict(
        transport=TransportHealthStatus.HEALTHY,
        market_data=MarketDataHealthStatus.HEALTHY,
        session_state="OPEN",
        short_epochs_in_window=0,
        last_quote_age_seconds=1.0,
        reasons=(),
    )
    return ExecutionGateSnapshot(market=market, evaluated_at=now, session=session, health=health)


def _orch(
    sample_risk_input_factory,
    *,
    active: ActiveBundle | None,
    coordinator: _FakeCoordinator | None = None,
    gate: _FixedGate | None = None,
    latest: LatestMarketStateStore | None = None,
    reader: _FakeReader | None = None,
) -> FastLoopExecutionOrchestrator:
    ri, bundle = _bundle(sample_risk_input_factory)
    active_bundle = active if active is not None else _active_bundle(bundle)
    store = latest or LatestMarketStateStore()
    if latest is None:
        store.apply(_trade(at=NOW), now=NOW)
        store.apply(_quote(at=NOW), now=NOW)
    coord = coordinator or _FakeCoordinator()
    gate_impl = gate or _FixedGate(_open_gate())
    inputs = StaticExecutionInputsProvider(
        allocator_decision=ri.allocator_decision,
        portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
    )
    return FastLoopExecutionOrchestrator(
        active_reader=reader or _FakeReader(active_bundle),
        latest_store=store,
        execution_gate=gate_impl,
        execution_inputs_provider=inputs,
        coordinator=coord,  # type: ignore[arg-type]
    )


def test_gate_held_session_no_coordinator(sample_risk_input_factory) -> None:
    session = MarketSession(market=Market.KR, state=MarketSessionState.CLOSED, as_of=NOW)
    health = HealthVerdict(
        transport=TransportHealthStatus.HEALTHY,
        market_data=MarketDataHealthStatus.HEALTHY,
        session_state="CLOSED",
        short_epochs_in_window=0,
        last_quote_age_seconds=1.0,
        reasons=(),
    )
    gate = _FixedGate(ExecutionGateSnapshot(market=Market.KR, evaluated_at=NOW, session=session, health=health))
    coord = _FakeCoordinator()
    _, bundle = _bundle(sample_risk_input_factory)
    orch = _orch(sample_risk_input_factory, active=_active_bundle(bundle), coordinator=coord, gate=gate)
    result = orch.handle_applied_update(_update())
    assert result.status is FastLoopExecutionStatus.HELD_SESSION
    assert coord.calls == 0


def test_gate_held_health_no_coordinator(sample_risk_input_factory) -> None:
    session = MarketSession(market=Market.KR, state=MarketSessionState.OPEN, as_of=NOW)
    health = HealthVerdict(
        transport=TransportHealthStatus.WARMING,
        market_data=MarketDataHealthStatus.HEALTHY,
        session_state="OPEN",
        short_epochs_in_window=0,
        last_quote_age_seconds=1.0,
        reasons=("transport_warming",),
    )
    gate = _FixedGate(ExecutionGateSnapshot(market=Market.KR, evaluated_at=NOW, session=session, health=health))
    coord = _FakeCoordinator()
    _, bundle = _bundle(sample_risk_input_factory)
    orch = _orch(sample_risk_input_factory, active=_active_bundle(bundle), coordinator=coord, gate=gate)
    result = orch.handle_applied_update(_update())
    assert result.status is FastLoopExecutionStatus.HELD_HEALTH
    assert coord.calls == 0


def test_gate_provider_exception_sanitized(sample_risk_input_factory) -> None:
    coord = _FakeCoordinator()
    _, bundle = _bundle(sample_risk_input_factory)
    orch = _orch(
        sample_risk_input_factory,
        active=_active_bundle(bundle),
        coordinator=coord,
        gate=_FixedGate(None, boom=True),
    )
    result = orch.handle_applied_update(_update())
    assert result.status is FastLoopExecutionStatus.GATE_PROVIDER_ERROR
    assert coord.calls == 0


def test_missing_active_no_coordinator(sample_risk_input_factory) -> None:
    coord = _FakeCoordinator()
    orch = _orch(
        sample_risk_input_factory,
        active=_active_bundle(_bundle(sample_risk_input_factory)[1]),
        coordinator=coord,
        reader=_FakeReader(None),
    )
    result = orch.handle_applied_update(_update())
    assert result.status is FastLoopExecutionStatus.MISSING_ACTIVE_DECISION
    assert coord.calls == 0


def test_corrupt_active_global_terminal(sample_risk_input_factory) -> None:
    coord = _FakeCoordinator()
    _, bundle = _bundle(sample_risk_input_factory)
    orch = _orch(
        sample_risk_input_factory,
        active=_active_bundle(bundle),
        coordinator=coord,
        reader=_FakeReader(None, corrupt=True),
    )
    first = orch.handle_applied_update(_update())
    assert first.status is FastLoopExecutionStatus.ACTIVE_DECISION_CORRUPT
    second = orch.handle_applied_update(_update())
    assert second.status is FastLoopExecutionStatus.GLOBAL_TERMINAL_FAIL_CLOSED
    assert coord.calls == 0


def test_expired_active_no_coordinator(sample_risk_input_factory) -> None:
    _, bundle = _bundle(sample_risk_input_factory)
    assert bundle.plan is not None
    expired_decision = bundle.decision.model_copy(update={"created_at": NOW - DAY})
    expired_plan = bundle.plan.model_copy(
        update={
            "created_at": NOW - DAY,
            "valid_from": NOW - DAY,
            "expires_at": NOW - timedelta(seconds=1),
        }
    )
    expired_bundle = DecisionTriggerBundle(decision=expired_decision, plan=expired_plan)
    active = _active_bundle(
        expired_bundle,
        valid_from=NOW - DAY,
        expires_at=NOW - timedelta(seconds=1),
    )
    coord = _FakeCoordinator()
    orch = _orch(sample_risk_input_factory, active=active, coordinator=coord)
    result = orch.handle_applied_update(_update())
    assert result.status is FastLoopExecutionStatus.ACTIVE_DECISION_EXPIRED
    assert coord.calls == 0


def test_uncertain_halts_symbol(sample_risk_input_factory) -> None:
    coord = _FakeCoordinator(CoordinatorResult(status=CoordinatorStatus.UNCERTAIN))
    _, bundle = _bundle(sample_risk_input_factory)
    orch = _orch(sample_risk_input_factory, active=_active_bundle(bundle), coordinator=coord)
    first = orch.handle_applied_update(_update())
    assert first.status is FastLoopExecutionStatus.UNCERTAIN
    second = orch.handle_applied_update(_update(applied_at=NOW + timedelta(seconds=1)))
    assert second.status is FastLoopExecutionStatus.HALTED_RECONCILE_REQUIRED
    assert coord.calls == 1


def test_committed_maps_evidence_fields(sample_risk_input_factory) -> None:
    _, bundle = _bundle(sample_risk_input_factory)
    signal = TriggerSignal(
        trigger_id="trig-1",
        idempotency_key="idem-1",
        decision_id=bundle.decision.decision_id,
        plan_id="plan-1",
        market=Market.KR,
        symbol="005930",
        action=AnalysisAction.BUY,
        reference_price=Decimal(_PRICE),
        triggered_at=NOW,
        condition_values=(),
    )
    coord = _FakeCoordinator(
        CoordinatorResult(
            status=CoordinatorStatus.COMMITTED,
            trigger_status=TriggerStatus.TRIGGERED,
            trigger_reason=TriggerReason.DEBOUNCE_PENDING,
            signal=signal,
        )
    )
    orch = _orch(sample_risk_input_factory, active=_active_bundle(bundle), coordinator=coord)
    result = orch.handle_applied_update(_update())
    assert result.status is FastLoopExecutionStatus.COMMITTED
    assert result.trigger_id == "trig-1"
    assert result.idempotency_key == "idem-1"
    assert coord.calls == 1


def test_session_health_gate_evaluates_open_healthy() -> None:
    from market_data.market_session import FixtureMarketCalendar

    kst_open = datetime(2026, 5, 22, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    connected_at = kst_open - timedelta(seconds=5)
    cal = FixtureMarketCalendar.for_krx()
    tracker = MarketHealthTracker(
        HealthThresholds(
            subscription_grace_seconds=30.0,
            heartbeat_timeout_seconds=60.0,
            minimum_stable_uptime_seconds=1.0,
            flapping_window_seconds=120.0,
            flapping_max_short_epochs=3,
            flapping_min_uptime_seconds=30.0,
            flapping_min_market_events=1,
            quote_grace_seconds=30.0,
            quote_starvation_seconds=30.0,
            max_quote_age_seconds=60.0,
        )
    )
    tracker.record_transport_event(kind="connected", at=connected_at, now=kst_open)
    tracker.record_transport_event(kind="all_subscribed", at=connected_at, now=kst_open)
    tracker.record_market_event(event_type="best_bid_ask", at=kst_open, now=kst_open)
    gate = SessionHealthExecutionGate(calendar=cal, tracker=tracker)
    snap = gate.evaluate(market=Market.KR, now=kst_open)
    assert snap.session.is_open
    assert snap.health.is_execution_ready
