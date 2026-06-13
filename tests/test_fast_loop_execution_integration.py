"""RTM-7c.2 — offline real paper-stack fast-loop integration tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from analysis import (
    AnalysisAction,
    AnalysisDecision,
    AnalysisReason,
    BearPerspective,
    BullPerspective,
    FundManagerDecision,
    RiskManagerEvaluation,
)
from analysis.models import ANALYSIS_DECISION_SCHEMA
from broker.paper_broker import PaperBrokerAdapter
from domain import DateId, DecisionId, Percent
from domain.decision import DecisionSnapshot
from domain.enums import AccountRole, Market, OrderStatus
from domain.position import CashSnapshot
from domain.validation import ValidationResult
from execution.paper_execution_coordinator import PaperExecutionCoordinator
from execution.paper_portfolio_context import PaperPortfolioContextService, PaperPortfolioPolicy
from execution.sqlite_trigger_journal import SqliteTriggerJournal
from execution.trigger_order_bridge import TriggerOrderBridge
from ledger.sqlite_ledger import SQLiteLedger
from market_data.conditions import Comparator, ConditionClause, Metric
from market_data.indicators import IndicatorWindowSpec
from market_data.health_policy import HealthThresholds, MarketHealthTracker, provisional_thresholds
from market_data.latest_state import LatestMarketStateStore
from market_data.market_session import FixtureMarketCalendar
from market_data.models import NormalizedBestBidAsk, NormalizedTradeTick, ProviderSequence
from market_data.monitor import AppliedMarketUpdate, MarketMonitor
from market_data.replay_source import ReplayMarketEventSource
from market_data.rolling_window import RollingRetentionPolicy, RollingTradeHistoryStore
from market_data.trigger_engine import TriggerEngine, TriggerPlan
from orchestration.active_decision_store import ActiveDecisionStore, DecisionPublicationCandidate
from orchestration.execution_gate import SessionHealthExecutionGate
from orchestration.fast_loop_execution import (
    FastLoopExecutionOrchestrator,
    FastLoopExecutionStatus,
    StaticExecutionInputsProvider,
)
from paper_loop import QuantityResolver
from risk import OrderIntentGenerator
from risk.models import RiskMode

KST = ZoneInfo("Asia/Seoul")
T0 = datetime(2026, 5, 22, 10, 0, 0, tzinfo=KST)
DAY = timedelta(days=1)
KRW = __import__("domain.enums", fromlist=["Currency"]).Currency.KRW
_PRICE = Decimal("70000")
_THRESHOLD = "100000"
_TRADE_CH = "H0STCNT0|005930"
_QUOTE_CH = "H0STASP0|005930"


def _reason(date_id: str = "260522-1") -> AnalysisReason:
    return AnalysisReason(reason="근거", date_id=DateId(date_id))


def _analysis_decision(
    *,
    action: AnalysisAction,
    decision_id: str,
    created_at: datetime,
) -> AnalysisDecision:
    return AnalysisDecision(
        decision_id=DecisionId(decision_id),
        created_at=created_at,
        universe="KR_LARGE",
        symbol="005930",
        market="KR",
        summary_one_liner="요약",
        bear=BearPerspective(summary="하방", risks=("리스크",), reasons=(_reason(),)),
        bull=BullPerspective(summary="상방", catalysts=("촉매",), reasons=(_reason("260522-2"),)),
        risk_manager=RiskManagerEvaluation(summary="중립", reasons=(_reason("260522-3"),)),
        fund_manager=FundManagerDecision(
            action=action,
            target_weight_percent=Percent("4"),
            rationale="근거",
            reasons=(_reason("260522-4"),),
        ),
        reasons=(_reason("260522-5"),),
    )


def _decision_snapshot(decision: AnalysisDecision) -> DecisionSnapshot:
    return DecisionSnapshot.create(
        decision_id=decision.decision_id,
        created_at=decision.created_at,
        schema_name=ANALYSIS_DECISION_SCHEMA,
        raw_payload=decision.model_dump(mode="json"),
        validation_result=ValidationResult(
            passed=True, issues=(), schema_name=ANALYSIS_DECISION_SCHEMA
        ),
    )


def _trade(*, sequence: int, at: datetime = T0) -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="kis", symbol="005930", market=Market.KR, currency=KRW,
        price=_PRICE, quantity=Decimal("10"), trade_at=at, received_at=at,
        provider_sequence=ProviderSequence(
            provider="kis", channel=_TRADE_CH, sequence=sequence, received_at=at
        ),
    )


def _quote(*, sequence: int, at: datetime = T0) -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider="kis", symbol="005930", market=Market.KR, currency=KRW,
        bid_price=_PRICE, ask_price=_PRICE,
        bid_quantity=Decimal("10"), ask_quantity=Decimal("10"),
        quote_at=at, received_at=at,
        provider_sequence=ProviderSequence(
            provider="kis", channel=_QUOTE_CH, sequence=sequence, received_at=at
        ),
    )


class _SteppingClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class _InlineMarketSource:
    def __init__(self, store: LatestMarketStateStore) -> None:
        self._store = store

    def get_snapshot(self, symbol: str, market: Market, *, now: datetime):
        return self._store.peek(market, symbol, now=now)


def _stack(tmp_path: Path, sample_risk_input_factory) -> dict[str, Any]:
    clock = _SteppingClock(T0)
    latest = LatestMarketStateStore()
    rolling = RollingTradeHistoryStore(
        retention=RollingRetentionPolicy(hard_max_events=1000, hard_max_age_seconds=Decimal("86400"))
    )
    active_store = ActiveDecisionStore(tmp_path / "active.sqlite3")
    ledger = SQLiteLedger(tmp_path / "ledger.sqlite3")
    broker = PaperBrokerAdapter(
        ledger,
        initial_cash=CashSnapshot(
            currency=KRW, amount=Decimal("100000000"), account_role=AccountRole.PAPER, as_of=T0
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
    service = PaperPortfolioContextService(
        ledger_source=ledger, market_state_source=_InlineMarketSource(latest)
    )
    coordinator = PaperExecutionCoordinator(
        engine=engine, bridge=bridge, portfolio_context_service=service
    )
    ri = sample_risk_input_factory(
        action=AnalysisAction.BUY,
        target_weight_percent=Percent("4"),
        correlation_id="idem-stack",
    )
    cal = FixtureMarketCalendar.for_krx()
    thr = HealthThresholds(
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
    tracker = MarketHealthTracker(thr)
    connected_at = T0 - timedelta(seconds=5)
    tracker.record_transport_event(kind="connected", at=connected_at, now=T0)
    tracker.record_transport_event(kind="all_subscribed", at=connected_at, now=T0)
    tracker.record_market_event(event_type="best_bid_ask", at=T0, now=T0)
    gate = SessionHealthExecutionGate(calendar=cal, tracker=tracker)
    orch = FastLoopExecutionOrchestrator(
        active_reader=active_store,
        latest_store=latest,
        rolling_store=rolling,
        execution_gate=gate,
        execution_inputs_provider=StaticExecutionInputsProvider(
            allocator_decision=ri.allocator_decision.model_copy(
                update={"universe": "KR_LARGE", "created_at": T0}
            ),
            portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
        ),
        coordinator=coordinator,
    )
    inputs = StaticExecutionInputsProvider(
        allocator_decision=ri.allocator_decision.model_copy(
            update={"universe": "KR_LARGE", "created_at": T0}
        ),
        portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
    )
    results: list[object] = []

    def _on_applied(update: AppliedMarketUpdate) -> None:
        et = "trade" if update.event_type.value == "trade" else "best_bid_ask"
        tracker.record_market_event(event_type=et, at=update.applied_at, now=update.applied_at)
        results.append(orch.handle_applied_update(update))

    def _publish_buy() -> None:
        now = clock()
        analysis = _analysis_decision(
            action=AnalysisAction.BUY, decision_id="buy-stack-1", created_at=now
        )
        plan = TriggerPlan(
            plan_id="plan-stack",
            decision_id=analysis.decision_id,
            created_at=now,
            valid_from=now,
            expires_at=now + DAY,
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
        active_store.publish(
            DecisionPublicationCandidate(
                snapshot=_decision_snapshot(analysis),
                plan=plan,
                valid_from=now,
                expires_at=now + DAY,
            ),
            now=now,
        )

    def _publish_hold(*, decision_id: str = "hold-1") -> None:
        now = clock()
        analysis = _analysis_decision(
            action=AnalysisAction.HOLD, decision_id=decision_id, created_at=now
        )
        active_store.publish(
            DecisionPublicationCandidate(
                snapshot=_decision_snapshot(analysis),
                plan=None,
                valid_from=now,
                expires_at=now + DAY,
            ),
            now=now,
        )

    def _run(events: list) -> None:
        monitor = MarketMonitor(
            store=latest,
            rolling_store=rolling,
            source_factory=lambda: ReplayMarketEventSource(events),
            clock=clock,
            session_id="stack",
            max_events=len(events),
            on_applied_update=_on_applied,
        )
        asyncio.run(monitor.run())

    return {
        "clock": clock,
        "broker": broker,
        "ledger": ledger,
        "journal": journal,
        "results": results,
        "publish_buy": _publish_buy,
        "publish_hold": _publish_hold,
        "run": _run,
        "gate": gate,
        "tracker": tracker,
        "coordinator": coordinator,
        "inputs": inputs,
        "latest": latest,
        "rolling": rolling,
    }


def test_integration_buy_fill_once_no_duplicate(tmp_path: Path, sample_risk_input_factory) -> None:
    ctx = _stack(tmp_path, sample_risk_input_factory)
    ctx["publish_buy"]()
    ctx["run"]([_quote(sequence=1), _trade(sequence=1), _quote(sequence=2), _trade(sequence=2)])

    fills = [r for r in ctx["results"] if getattr(r, "status", None) is FastLoopExecutionStatus.COMMITTED]
    assert len(fills) == 1
    order_id = f"order-{fills[0].decision_id}"
    assert ctx["broker"].get_position("005930", Market.KR, AccountRole.PAPER).quantity == Decimal("57")
    assert ctx["ledger"].get_fill_by_order_id(order_id) is not None
    row_count = ctx["journal"]._conn.execute("SELECT COUNT(*) FROM trigger_fire_journal").fetchone()[0]
    assert row_count >= 1


def _rolling_stack(
    tmp_path: Path,
    sample_risk_input_factory,
    *,
    rolling_store: RollingTradeHistoryStore | None,
    threshold: str = "1",
    lookback_events: int = 2,
    min_events: int = 2,
) -> dict[str, Any]:
    """rolling rule이 있는 real paper stack — MarketMonitor 경유 store 동기화."""
    ctx = _stack(tmp_path, sample_risk_input_factory)
    spec = IndicatorWindowSpec(
        lookback_events=lookback_events,
        min_events=min_events,
        freshness_max_age_seconds=Decimal("3600"),
    )
    now = ctx["clock"]()
    analysis = _analysis_decision(
        action=AnalysisAction.BUY, decision_id="buy-roll", created_at=now
    )
    plan = TriggerPlan(
        plan_id="plan-roll",
        decision_id=analysis.decision_id,
        created_at=now,
        valid_from=now,
        expires_at=now + DAY,
        universe=analysis.universe,
        market=Market.KR,
        symbol=analysis.symbol,
        action=AnalysisAction.BUY,
        rules=(
            ConditionClause(
                metric=Metric.SMA_PRICE,
                comparator=Comparator.GTE,
                threshold=threshold,
                window=spec,
            ),
        ),
    )
    store = ActiveDecisionStore(tmp_path / "active.sqlite3")
    store.publish(
        DecisionPublicationCandidate(
            snapshot=_decision_snapshot(analysis),
            plan=plan,
            valid_from=now,
            expires_at=now + DAY,
        ),
        now=now,
    )
    rolling = rolling_store
    latest = LatestMarketStateStore()
    orch = FastLoopExecutionOrchestrator(
        active_reader=store,
        latest_store=latest,
        rolling_store=rolling,
        execution_gate=ctx["gate"],  # type: ignore[index]
        execution_inputs_provider=ctx["inputs"],  # type: ignore[index]
        coordinator=ctx["coordinator"],  # type: ignore[index]
    )
    results: list[object] = []

    def _on_applied(update: AppliedMarketUpdate) -> None:
        et = "trade" if update.event_type.value == "trade" else "best_bid_ask"
        ctx["tracker"].record_market_event(event_type=et, at=update.applied_at, now=update.applied_at)  # type: ignore[index]
        results.append(orch.handle_applied_update(update))

    def _run(events: list) -> None:
        monitor = MarketMonitor(
            store=latest,
            rolling_store=rolling,
            source_factory=lambda: ReplayMarketEventSource(events),
            clock=ctx["clock"],
            session_id="roll",
            max_events=len(events),
            on_applied_update=_on_applied,
        )
        asyncio.run(monitor.run())

    ctx.update(
        {
            "rolling": rolling,
            "latest": latest,
            "orch_results": results,
            "run_rolling": _run,
            "rolling_store": rolling_store,
        }
    )
    return ctx


def test_integration_rolling_ready_committed_once_no_duplicate(
    tmp_path: Path, sample_risk_input_factory
) -> None:
    """READY rolling: 두 trade 후 COMMITTED 1회, 추가 tick duplicate 0."""
    rolling = RollingTradeHistoryStore(
        retention=RollingRetentionPolicy(hard_max_events=1000, hard_max_age_seconds=Decimal("86400"))
    )
    ctx = _rolling_stack(tmp_path, sample_risk_input_factory, rolling_store=rolling)
    ctx["run_rolling"]([_quote(sequence=1), _trade(sequence=1)])
    warming = ctx["orch_results"][-1]
    assert warming.status is FastLoopExecutionStatus.SUPPRESSED
    pos = ctx["broker"].get_position("005930", Market.KR, AccountRole.PAPER)
    assert pos is None or pos.quantity == Decimal("0")

    ctx["clock"].advance(timedelta(seconds=1))
    ctx["run_rolling"]([_quote(sequence=2), _trade(sequence=2)])
    committed = [r for r in ctx["orch_results"] if r.status is FastLoopExecutionStatus.COMMITTED]
    assert len(committed) == 1
    assert ctx["broker"].get_position("005930", Market.KR, AccountRole.PAPER).quantity == Decimal("57")

    ctx["clock"].advance(timedelta(seconds=1))
    before_fills = len(
        [r for r in ctx["orch_results"] if r.status is FastLoopExecutionStatus.COMMITTED]
    )
    ctx["run_rolling"]([_quote(sequence=3), _trade(sequence=3)])
    after_fills = [r for r in ctx["orch_results"] if r.status is FastLoopExecutionStatus.COMMITTED]
    assert len(after_fills) == before_fills


def test_integration_rolling_warming_suppressed_no_order(
    tmp_path: Path, sample_risk_input_factory
) -> None:
    """WARMING: trade 1건만 → INDICATOR_WARMING, broker/ledger/journal 0."""
    rolling = RollingTradeHistoryStore(
        retention=RollingRetentionPolicy(hard_max_events=1000, hard_max_age_seconds=Decimal("86400"))
    )
    ctx = _rolling_stack(tmp_path, sample_risk_input_factory, rolling_store=rolling)
    ctx["run_rolling"]([_quote(sequence=1), _trade(sequence=1)])
    result = ctx["orch_results"][-1]
    assert result.status is FastLoopExecutionStatus.SUPPRESSED
    assert result.coordinator_status == "suppressed"
    from market_data.trigger_engine import TriggerReason

    assert result.reason_code == TriggerReason.INDICATOR_WARMING.value
    pos = ctx["broker"].get_position("005930", Market.KR, AccountRole.PAPER)
    assert pos is None or pos.quantity == Decimal("0")
    row_count = ctx["journal"]._conn.execute("SELECT COUNT(*) FROM trigger_fire_journal").fetchone()[0]
    assert row_count == 0
    fill_count = ctx["ledger"]._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    assert fill_count == 0


def test_integration_rolling_store_none_missing_indicator(
    tmp_path: Path, sample_risk_input_factory
) -> None:
    """rolling rule + rolling_store=None → MISSING_INDICATOR suppression, order 0."""
    ctx = _rolling_stack(tmp_path, sample_risk_input_factory, rolling_store=None)
    ctx["run_rolling"]([_quote(sequence=1), _trade(sequence=1), _quote(sequence=2), _trade(sequence=2)])
    from market_data.trigger_engine import TriggerReason

    suppressed = [r for r in ctx["orch_results"] if r.status is FastLoopExecutionStatus.SUPPRESSED]
    assert len(suppressed) >= 1
    assert any(r.reason_code == TriggerReason.MISSING_INDICATOR.value for r in suppressed)
    assert not any(r.status is FastLoopExecutionStatus.COMMITTED for r in ctx["orch_results"])
    pos = ctx["broker"].get_position("005930", Market.KR, AccountRole.PAPER)
    assert pos is None or pos.quantity == Decimal("0")


def test_integration_hold_replaces_buy_no_new_order(tmp_path: Path, sample_risk_input_factory) -> None:
    ctx = _stack(tmp_path, sample_risk_input_factory)
    ctx["publish_buy"]()
    ctx["run"]([_quote(sequence=1), _trade(sequence=1)])
    qty_after_buy = ctx["broker"].get_position("005930", Market.KR, AccountRole.PAPER).quantity
    results_after_buy = len(ctx["results"])

    ctx["clock"].advance(timedelta(minutes=5))
    ctx["publish_hold"](decision_id="hold-new")
    ctx["run"]([_quote(sequence=3), _trade(sequence=3)])

    qty_after_hold = ctx["broker"].get_position("005930", Market.KR, AccountRole.PAPER).quantity
    assert qty_after_hold == qty_after_buy
    new_results = ctx["results"][results_after_buy:]
    assert not any(getattr(r, "status", None) is FastLoopExecutionStatus.COMMITTED for r in new_results)
