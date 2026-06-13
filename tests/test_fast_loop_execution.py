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
        "symbol": bundle.decision.symbol,
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
        allocator_decision=ri.allocator_decision.model_copy(
            update={"universe": bundle.decision.universe}
        ),
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


# --- Hardening A: COMMITTED evidence sink terminal latch ---------------------


def test_committed_evidence_sink_failure_latches_terminal(sample_risk_input_factory) -> None:
    _, bundle = _bundle(sample_risk_input_factory)
    coord = _FakeCoordinator(
        CoordinatorResult(status=CoordinatorStatus.COMMITTED)
    )

    def _fail_sink(_evidence: object) -> None:
        raise RuntimeError("secret sink failure")

    orch = FastLoopExecutionOrchestrator(
        active_reader=_FakeReader(_active_bundle(bundle)),
        latest_store=_seed_latest(NOW),
        execution_gate=_FixedGate(_open_gate()),
        execution_inputs_provider=_inputs_provider(sample_risk_input_factory),
        coordinator=coord,  # type: ignore[arg-type]
        on_evidence=_fail_sink,
    )
    first = orch.handle_applied_update(_update())
    assert first.status is FastLoopExecutionStatus.COMMITTED
    assert coord.calls == 1
    second = orch.handle_applied_update(_update(applied_at=NOW + timedelta(seconds=1)))
    assert second.status is FastLoopExecutionStatus.GLOBAL_TERMINAL_FAIL_CLOSED
    assert coord.calls == 1


def test_non_committed_evidence_sink_failure_returns_evidence_error(
    sample_risk_input_factory,
) -> None:
    _, bundle = _bundle(sample_risk_input_factory)
    coord = _FakeCoordinator(CoordinatorResult(status=CoordinatorStatus.SUPPRESSED))

    def _fail_sink(_evidence: object) -> None:
        raise RuntimeError("secret")

    orch = FastLoopExecutionOrchestrator(
        active_reader=_FakeReader(_active_bundle(bundle)),
        latest_store=_seed_latest(NOW),
        execution_gate=_FixedGate(_open_gate()),
        execution_inputs_provider=_inputs_provider(sample_risk_input_factory),
        coordinator=coord,  # type: ignore[arg-type]
        on_evidence=_fail_sink,
    )
    result = orch.handle_applied_update(_update())
    assert result.status is FastLoopExecutionStatus.EVIDENCE_SINK_ERROR
    assert coord.calls == 1
    assert "secret" not in str(result.reason_code)


# --- Hardening B: malformed AppliedMarketUpdate --------------------------------


class _BadUpdate:
    """AppliedMarketUpdate를 흉내 내는 mutable 테스트 stub."""

    pass


def _bad(**attrs: object) -> _BadUpdate:
    u = _BadUpdate()
    defaults: dict[str, object] = {
        "market": Market.KR,
        "symbol": "005930",
        "event_type": MarketEventType.TRADE,
        "provider": "kis",
        "channel": "t",
        "sequence": 1,
        "applied_at": NOW,
    }
    defaults.update(attrs)
    for k, v in defaults.items():
        setattr(u, k, v)
    return u


class _TrackingGate:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, *, market: Market, now: datetime) -> ExecutionGateSnapshot:
        self.calls += 1
        return _open_gate(now=now, market=market)


class _TrackingReader:
    def __init__(self) -> None:
        self.calls = 0

    def read_active(self, market: Market | str, symbol: str) -> ActiveBundle | None:
        self.calls += 1
        return None


def _inputs_provider(sample_risk_input_factory) -> StaticExecutionInputsProvider:
    ri, _ = _bundle(sample_risk_input_factory)
    return StaticExecutionInputsProvider(
        allocator_decision=ri.allocator_decision.model_copy(
            update={"universe": ri.analysis_decision.universe}
        ),
        portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
    )


def _seed_latest(at: datetime) -> LatestMarketStateStore:
    store = LatestMarketStateStore()
    store.apply(_trade(at=at), now=at)
    store.apply(_quote(at=at), now=at)
    return store


@pytest.mark.parametrize(
    "attrs",
    [
        {"market": "KR"},
        {"event_type": "trade"},
        {"symbol": None},
        {"symbol": ""},
        {"provider": 1},
        {"channel": []},
        {"sequence": True},
        {"sequence": "1"},
        {"sequence": 1.5},
        {"sequence": -1},
        {"applied_at": datetime(2026, 5, 22, 12, 0)},
        {"applied_at": "timestamp"},
    ],
)
def test_malformed_update_rejected_no_dependencies(
    sample_risk_input_factory, attrs: dict[str, object]
) -> None:
    gate = _TrackingGate()
    reader = _TrackingReader()
    coord = _FakeCoordinator()
    orch = FastLoopExecutionOrchestrator(
        active_reader=reader,
        latest_store=_seed_latest(NOW),
        execution_gate=gate,
        execution_inputs_provider=_inputs_provider(sample_risk_input_factory),
        coordinator=coord,  # type: ignore[arg-type]
    )
    result = orch.handle_applied_update(_bad(**attrs))
    assert result.status is FastLoopExecutionStatus.MALFORMED_UPDATE
    assert gate.calls == 0
    assert reader.calls == 0
    assert coord.calls == 0


# --- Hardening C: allocator binding --------------------------------------------


def test_allocator_universe_mismatch_rejected(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    coord = _FakeCoordinator()
    orch = _orch(
        sample_risk_input_factory,
        active=_active_bundle(bundle),
        coordinator=coord,
    )
    orch._execution_inputs_provider = StaticExecutionInputsProvider(  # type: ignore[method-assign]
        allocator_decision=ri.allocator_decision.model_copy(update={"universe": "WRONG"}),
        portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
    )
    result = orch.handle_applied_update(_update())
    assert result.status is FastLoopExecutionStatus.EXECUTION_INPUTS_UNAVAILABLE
    assert coord.calls == 0
    assert "WRONG" not in str(result.reason_code)


def test_allocator_future_created_at_rejected(sample_risk_input_factory) -> None:
    ri, bundle = _bundle(sample_risk_input_factory)
    coord = _FakeCoordinator()
    orch = _orch(
        sample_risk_input_factory,
        active=_active_bundle(bundle),
        coordinator=coord,
    )
    orch._execution_inputs_provider = StaticExecutionInputsProvider(  # type: ignore[method-assign]
        allocator_decision=ri.allocator_decision.model_copy(
            update={"universe": bundle.decision.universe, "created_at": NOW + DAY}
        ),
        portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
    )
    result = orch.handle_applied_update(_update())
    assert result.status is FastLoopExecutionStatus.EXECUTION_INPUTS_UNAVAILABLE
    assert coord.calls == 0


# --- Hardening D: rolling orchestration ----------------------------------------


class _CapturingCoordinator:
    def __init__(self, result: CoordinatorResult | None = None) -> None:
        self._result = result or CoordinatorResult(status=CoordinatorStatus.SUPPRESSED)
        self.calls = 0
        self.kwargs: list[dict[str, object]] = []

    def process(self, **kwargs: object) -> CoordinatorResult:
        self.calls += 1
        self.kwargs.append(kwargs)
        return self._result


def _rolling_bundle(sample_risk_input_factory) -> tuple[Any, DecisionTriggerBundle]:
    from market_data.indicators import IndicatorWindowSpec

    ri = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        correlation_id="idem-roll",
    )
    analysis = ri.analysis_decision
    spec = IndicatorWindowSpec(
        lookback_events=3, min_events=2, freshness_max_age_seconds=Decimal("3600")
    )
    plan = TriggerPlan(
        plan_id="plan-roll",
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
                metric=Metric.SMA_PRICE,
                comparator=Comparator.GTE,
                threshold="100",
                window=spec,
            ),
        ),
    )
    return ri, DecisionTriggerBundle(decision=analysis, plan=plan)


def _seed_rolling_history(
    rolling: RollingTradeHistoryStore, *, at: datetime
) -> None:
    from market_data.rolling_window import RollingRetentionPolicy

    for seq, offset in ((1, 1), (2, 2), (3, 3)):
        tick = NormalizedTradeTick(
            provider="kis",
            symbol="005930",
            market=Market.KR,
            currency=KRW,
            price=Decimal("70000"),
            quantity=Decimal("10"),
            trade_at=at - timedelta(seconds=4 - offset),
            received_at=at - timedelta(seconds=4 - offset),
            provider_sequence=ProviderSequence(
                provider="kis",
                channel="H0STCNT0|005930",
                sequence=seq,
                received_at=at - timedelta(seconds=4 - offset),
            ),
        )
        rolling.observe(tick, now=at)


def test_rolling_ready_indicator_context_passed_to_coordinator(
    sample_risk_input_factory,
) -> None:
    from market_data.indicators import IndicatorReadiness
    from market_data.rolling_window import RollingRetentionPolicy, RollingTradeHistoryStore

    at = NOW
    ri, bundle = _rolling_bundle(sample_risk_input_factory)
    latest = LatestMarketStateStore()
    trade = NormalizedTradeTick(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=KRW,
        price=Decimal("70000"),
        quantity=Decimal("10"),
        trade_at=at,
        received_at=at,
        provider_sequence=ProviderSequence(
            provider="kis", channel="H0STCNT0|005930", sequence=3, received_at=at
        ),
    )
    latest.apply(trade, now=at)
    latest.apply(_quote(at=at), now=at)
    rolling = RollingTradeHistoryStore(
        retention=RollingRetentionPolicy(hard_max_events=100, hard_max_age_seconds=Decimal("86400"))
    )
    _seed_rolling_history(rolling, at=at)
    coord = _CapturingCoordinator()
    orch = FastLoopExecutionOrchestrator(
        active_reader=_FakeReader(_active_bundle(bundle)),
        latest_store=latest,
        rolling_store=rolling,
        execution_gate=_FixedGate(_open_gate(now=at)),
        execution_inputs_provider=StaticExecutionInputsProvider(
            allocator_decision=ri.allocator_decision.model_copy(
                update={"universe": bundle.decision.universe}
            ),
            portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
        ),
        coordinator=coord,  # type: ignore[arg-type]
    )
    orch.handle_applied_update(_update(applied_at=at))
    assert coord.calls == 1
    indicators = coord.kwargs[0]["indicators"]
    assert indicators is not None
    assert indicators.evaluated_at == at
    window = indicators.windows[0]
    assert window.readiness is IndicatorReadiness.READY


def test_rolling_warming_indicator_not_ready(sample_risk_input_factory) -> None:
    from market_data.indicators import IndicatorReadiness
    from market_data.rolling_window import RollingRetentionPolicy, RollingTradeHistoryStore

    at = NOW
    ri, bundle = _rolling_bundle(sample_risk_input_factory)
    latest = LatestMarketStateStore()
    trade = NormalizedTradeTick(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=KRW,
        price=Decimal("70000"),
        quantity=Decimal("10"),
        trade_at=at,
        received_at=at,
        provider_sequence=ProviderSequence(
            provider="kis", channel="H0STCNT0|005930", sequence=1, received_at=at
        ),
    )
    latest.apply(trade, now=at)
    latest.apply(_quote(at=at), now=at)
    rolling = RollingTradeHistoryStore(
        retention=RollingRetentionPolicy(hard_max_events=100, hard_max_age_seconds=Decimal("86400"))
    )
    rolling.observe(trade, now=at)
    coord = _CapturingCoordinator()
    orch = FastLoopExecutionOrchestrator(
        active_reader=_FakeReader(_active_bundle(bundle)),
        latest_store=latest,
        rolling_store=rolling,
        execution_gate=_FixedGate(_open_gate(now=at)),
        execution_inputs_provider=StaticExecutionInputsProvider(
            allocator_decision=ri.allocator_decision.model_copy(
                update={"universe": bundle.decision.universe}
            ),
            portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
        ),
        coordinator=coord,  # type: ignore[arg-type]
    )
    orch.handle_applied_update(_update(applied_at=at))
    assert coord.calls == 1
    indicators = coord.kwargs[0]["indicators"]
    assert indicators is not None
    window = indicators.windows[0]
    assert window.readiness is IndicatorReadiness.WARMING


def test_rolling_store_none_passes_indicators_none(sample_risk_input_factory) -> None:
    at = NOW
    ri, bundle = _rolling_bundle(sample_risk_input_factory)
    coord = _CapturingCoordinator()
    orch = FastLoopExecutionOrchestrator(
        active_reader=_FakeReader(_active_bundle(bundle)),
        latest_store=_seed_latest(at),
        rolling_store=None,
        execution_gate=_FixedGate(_open_gate(now=at)),
        execution_inputs_provider=StaticExecutionInputsProvider(
            allocator_decision=ri.allocator_decision.model_copy(
                update={"universe": bundle.decision.universe}
            ),
            portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
        ),
        coordinator=coord,  # type: ignore[arg-type]
    )
    orch.handle_applied_update(_update(applied_at=at))
    assert coord.calls == 1
    assert coord.kwargs[0]["indicators"] is None


# --- Hardening E: RECONCILE_REQUIRED halt --------------------------------------


def test_reconcile_required_halts_symbol(sample_risk_input_factory) -> None:
    coord = _FakeCoordinator(CoordinatorResult(status=CoordinatorStatus.RECONCILE_REQUIRED))
    _, bundle = _bundle(sample_risk_input_factory)
    orch = _orch(sample_risk_input_factory, active=_active_bundle(bundle), coordinator=coord)
    first = orch.handle_applied_update(_update())
    assert first.status is FastLoopExecutionStatus.RECONCILE_REQUIRED
    second = orch.handle_applied_update(_update(applied_at=NOW + timedelta(seconds=1)))
    assert second.status is FastLoopExecutionStatus.HALTED_RECONCILE_REQUIRED
    assert coord.calls == 1


def test_halt_does_not_affect_other_symbol(sample_risk_input_factory) -> None:
    coord = _FakeCoordinator(CoordinatorResult(status=CoordinatorStatus.UNCERTAIN))
    ri, bundle = _bundle(sample_risk_input_factory)
    ri2 = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        symbol="000660",
        correlation_id="idem-other",
    )
    analysis2 = ri2.analysis_decision
    plan2 = TriggerPlan(
        plan_id="plan-2",
        decision_id=analysis2.decision_id,
        created_at=NOW,
        valid_from=NOW,
        expires_at=NOW + DAY,
        universe=analysis2.universe,
        market=Market.KR,
        symbol="000660",
        action=AnalysisAction.BUY,
        rules=(
            ConditionClause(
                metric=Metric.LAST_TRADE_PRICE, comparator=Comparator.LTE, threshold=_THRESHOLD
            ),
        ),
    )
    bundle2 = DecisionTriggerBundle(decision=analysis2, plan=plan2)
    store = LatestMarketStateStore()
    for sym in ("005930", "000660"):
        store.apply(
            NormalizedTradeTick(
                provider="kis", symbol=sym, market=Market.KR, currency=KRW,
                price="70000", quantity="1", trade_at=NOW, received_at=NOW,
                provider_sequence=ProviderSequence(
                    provider="kis", channel=f"t|{sym}", sequence=1, received_at=NOW
                ),
            ),
            now=NOW,
        )
        store.apply(
            NormalizedBestBidAsk(
                provider="kis", symbol=sym, market=Market.KR, currency=KRW,
                bid_price="70000", ask_price="70000", bid_quantity="10", ask_quantity="10",
                quote_at=NOW, received_at=NOW,
                provider_sequence=ProviderSequence(
                    provider="kis", channel=f"q|{sym}", sequence=1, received_at=NOW
                ),
            ),
            now=NOW,
        )

    class _MultiReader:
        def read_active(self, market: Market | str, symbol: str) -> ActiveBundle | None:
            if symbol == "005930":
                return _active_bundle(bundle)
            if symbol == "000660":
                return _active_bundle(bundle2)
            return None

    orch = FastLoopExecutionOrchestrator(
        active_reader=_MultiReader(),
        latest_store=store,
        execution_gate=_FixedGate(_open_gate()),
        execution_inputs_provider=StaticExecutionInputsProvider(
            allocator_decision=ri.allocator_decision.model_copy(
                update={"universe": bundle.decision.universe}
            ),
            portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
        ),
        coordinator=coord,  # type: ignore[arg-type]
    )
    orch.handle_applied_update(_update(symbol="005930"))
    other = orch.handle_applied_update(_update(symbol="000660"))
    assert other.status is not FastLoopExecutionStatus.HALTED_RECONCILE_REQUIRED
    assert coord.calls == 2


def test_new_orchestrator_instance_clears_halt(sample_risk_input_factory) -> None:
    coord1 = _FakeCoordinator(CoordinatorResult(status=CoordinatorStatus.RECONCILE_REQUIRED))
    _, bundle = _bundle(sample_risk_input_factory)
    orch1 = _orch(sample_risk_input_factory, active=_active_bundle(bundle), coordinator=coord1)
    orch1.handle_applied_update(_update())
    orch2 = _orch(sample_risk_input_factory, active=_active_bundle(bundle), coordinator=_FakeCoordinator())
    result = orch2.handle_applied_update(_update(applied_at=NOW + timedelta(seconds=1)))
    assert result.status is not FastLoopExecutionStatus.HALTED_RECONCILE_REQUIRED


def test_orchestration_lazy_export() -> None:
    import orchestration

    assert orchestration.FastLoopExecutionOrchestrator is not None
    assert orchestration.ExecutionGateSnapshot is not None
    assert orchestration.ActiveDecisionStore is not None
