"""RTM-7c.3 — deterministic full-day offline slow+fast two-loop rehearsal."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import date, datetime, time, timedelta
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
from data.market_supervisor_adapter import MarketSupervisorAdapter
from domain import DateId, DecisionId, Percent
from domain.decision import DecisionSnapshot
from domain.enums import AccountRole, Market
from domain.position import CashSnapshot
from domain.validation import ValidationResult
from execution.paper_execution_coordinator import PaperExecutionCoordinator
from execution.paper_portfolio_context import PaperPortfolioContextService, PaperPortfolioPolicy
from execution.sqlite_trigger_journal import SqliteTriggerJournal
from execution.trigger_order_bridge import TriggerOrderBridge
from ledger.sqlite_ledger import SQLiteLedger
from market_data.conditions import Comparator, ConditionClause, Metric
from market_data.health_policy import HealthThresholds, MarketHealthTracker
from market_data.indicators import IndicatorWindowSpec
from market_data.latest_state import LatestMarketStateStore
from market_data.market_session import SessionWindow, build_explicit_schedule
from market_data.models import MarketEvent, NormalizedBestBidAsk, NormalizedTradeTick, ProviderSequence
from market_data.monitor import AppliedMarketUpdate, MarketMonitor, MonitorEvidence, MonitorState, MonitorSummary, ReconnectPolicy
from market_data.replay_source import ReplayMarketEventSource
from market_data.rolling_window import RollingRetentionPolicy, RollingTradeHistoryStore
from market_data.supervisor import MarketSupervisor, SupervisorPolicy, SupervisorState
from market_data.trigger_engine import TriggerEngine, TriggerPlan
from orchestration.active_decision_store import (
    ActiveDecisionStore,
    DecisionPublicationCandidate,
    PublicationError,
    SlotState,
)
from orchestration.decision_refresh_scheduler import (
    DecisionRefreshScheduler,
    RefreshSlotOutcome,
    SchedulerState,
    SlotConfig,
)
from orchestration.execution_gate import REASON_HELD_HEALTH, REASON_HELD_SESSION, SessionHealthExecutionGate
from orchestration.fast_loop_execution import (
    FastLoopExecutionOrchestrator,
    FastLoopExecutionStatus,
    StaticExecutionInputsProvider,
)
from paper_loop import QuantityResolver
from risk import OrderIntentGenerator
from risk.models import RiskMode

_KST = ZoneInfo("Asia/Seoul")
_DAY = date(2026, 6, 15)
_WINDOW = SessionWindow(
    pre_open=time(8, 30), open=time(9, 0), close=time(15, 30), post_close_end=time(16, 0)
)
_SLOTS = (
    SlotConfig(slot_id="s1", at=time(9, 30)),
    SlotConfig(slot_id="s2", at=time(11, 0)),
    SlotConfig(slot_id="s3", at=time(13, 0)),
    SlotConfig(slot_id="s4", at=time(14, 50)),
)
_DAY_DELTA = timedelta(days=1)
_PRICE = Decimal("70000")
_TRADE_CH = "H0STCNT0|005930"
_QUOTE_CH = "H0STASP0|005930"
_KRW = __import__("domain.enums", fromlist=["Currency"]).Currency.KRW


def _at(h: int, mi: int, s: int = 0) -> datetime:
    return datetime(2026, 6, 15, h, mi, s, tzinfo=_KST)


def _reason(date_id: str = "260615-1") -> AnalysisReason:
    return AnalysisReason(reason="근거", date_id=DateId(date_id))


def _analysis_decision(*, action: AnalysisAction, decision_id: str, created_at: datetime) -> AnalysisDecision:
    return AnalysisDecision(
        decision_id=DecisionId(decision_id),
        created_at=created_at,
        universe="KR_LARGE",
        symbol="005930",
        market="KR",
        summary_one_liner="요약",
        bear=BearPerspective(summary="하방", risks=("리스크",), reasons=(_reason(),)),
        bull=BullPerspective(summary="상방", catalysts=("촉매",), reasons=(_reason("260615-2"),)),
        risk_manager=RiskManagerEvaluation(summary="중립", reasons=(_reason("260615-3"),)),
        fund_manager=FundManagerDecision(
            action=action,
            target_weight_percent=Percent("4"),
            rationale="근거",
            reasons=(_reason("260615-4"),),
        ),
        reasons=(_reason("260615-5"),),
    )


def _snapshot(decision: AnalysisDecision) -> DecisionSnapshot:
    return DecisionSnapshot.create(
        decision_id=decision.decision_id,
        created_at=decision.created_at,
        schema_name=ANALYSIS_DECISION_SCHEMA,
        raw_payload=decision.model_dump(mode="json"),
        validation_result=ValidationResult(
            passed=True, issues=(), schema_name=ANALYSIS_DECISION_SCHEMA
        ),
    )


def _trade(*, sequence: int, at: datetime) -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=_KRW,
        price=_PRICE,
        quantity=Decimal("10"),
        trade_at=at,
        received_at=at,
        provider_sequence=ProviderSequence(
            provider="kis", channel=_TRADE_CH, sequence=sequence, received_at=at
        ),
    )


def _quote(*, sequence: int, at: datetime) -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=_KRW,
        bid_price=_PRICE,
        ask_price=_PRICE,
        bid_quantity=Decimal("10"),
        ask_quantity=Decimal("10"),
        quote_at=at,
        received_at=at,
        provider_sequence=ProviderSequence(
            provider="kis", channel=_QUOTE_CH, sequence=sequence, received_at=at
        ),
    )


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        self._now = at


class _SequencedClock:
    def __init__(self, times: Sequence[datetime]) -> None:
        self._times = list(times)
        self._i = 0

    def __call__(self) -> datetime:
        t = self._times[min(self._i, len(self._times) - 1)]
        self._i += 1
        return t


async def _fake_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


class _DropAfterFirst:
    """첫 이벤트 후 transport drop — reconnect epoch 모사."""

    def __init__(self, event: MarketEvent) -> None:
        self._event = event

    async def events(self) -> AsyncIterator[MarketEvent]:
        yield self._event
        raise RuntimeError("simulated transport drop")


class _ScriptedRunner:
    """slot별 scripted publication — LLM/Scout/Allocator/Analysis 미호출."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def refresh(
        self, *, market: Market, session_date: date, slot_id: str, scheduled_at: datetime
    ) -> DecisionPublicationCandidate:
        self.calls.append(slot_id)
        if slot_id == "s1":
            analysis = _analysis_decision(
                action=AnalysisAction.BUY, decision_id="s1-buy", created_at=scheduled_at
            )
            plan = TriggerPlan(
                plan_id="plan-s1",
                decision_id=analysis.decision_id,
                created_at=scheduled_at,
                valid_from=scheduled_at,
                expires_at=scheduled_at + _DAY_DELTA,
                universe=analysis.universe,
                market=Market.KR,
                symbol=analysis.symbol,
                action=AnalysisAction.BUY,
                rules=(
                    ConditionClause(
                        metric=Metric.LAST_TRADE_PRICE,
                        comparator=Comparator.LTE,
                        threshold="70000",
                    ),
                ),
            )
            return DecisionPublicationCandidate(
                snapshot=_snapshot(analysis),
                plan=plan,
                valid_from=scheduled_at,
                expires_at=scheduled_at + _DAY_DELTA,
            )
        if slot_id == "s2":
            analysis = _analysis_decision(
                action=AnalysisAction.HOLD, decision_id="s2-hold", created_at=scheduled_at
            )
            return DecisionPublicationCandidate(
                snapshot=_snapshot(analysis),
                plan=None,
                valid_from=scheduled_at,
                expires_at=scheduled_at + _DAY_DELTA,
            )
        if slot_id == "s3":
            raise RuntimeError("scripted runner failure")
        if slot_id == "s4":
            analysis = _analysis_decision(
                action=AnalysisAction.BUY, decision_id="s4-buy-false", created_at=scheduled_at
            )
            plan = TriggerPlan(
                plan_id="plan-s4",
                decision_id=analysis.decision_id,
                created_at=scheduled_at,
                valid_from=scheduled_at,
                expires_at=scheduled_at + _DAY_DELTA,
                universe=analysis.universe,
                market=Market.KR,
                symbol=analysis.symbol,
                action=AnalysisAction.BUY,
                rules=(
                    ConditionClause(
                        metric=Metric.LAST_TRADE_PRICE,
                        comparator=Comparator.LTE,
                        threshold="60000",
                    ),
                ),
            )
            return DecisionPublicationCandidate(
                snapshot=_snapshot(analysis),
                plan=plan,
                valid_from=scheduled_at,
                expires_at=scheduled_at + _DAY_DELTA,
            )
        raise AssertionError(f"unexpected slot_id: {slot_id}")


class _NeverRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def refresh(self, **_: object) -> DecisionPublicationCandidate:
        self.calls += 1
        raise AssertionError("runner must not be called")


class _InlineMarketSource:
    def __init__(self, store: LatestMarketStateStore) -> None:
        self._store = store

    def get_snapshot(self, symbol: str, market: Market, *, now: datetime):
        return self._store.peek(market, symbol, now=now)


class _CountingCoordinator:
    """PaperExecutionCoordinator process() 호출 횟수 추적."""

    def __init__(self, inner: PaperExecutionCoordinator) -> None:
        self._inner = inner
        self.calls = 0

    def process(self, **kwargs: object):
        self.calls += 1
        return self._inner.process(**kwargs)

    def recover(self, *, now: datetime):
        return self._inner.recover(now=now)


def _health_thresholds() -> HealthThresholds:
    return HealthThresholds(
        subscription_grace_seconds=30.0,
        heartbeat_timeout_seconds=86400.0,
        minimum_stable_uptime_seconds=1.0,
        flapping_window_seconds=600.0,
        flapping_max_short_epochs=5,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=86400.0,
        max_quote_age_seconds=86400.0,
    )


class TwoLoopHarness:
    """slow-loop writer + fast-loop reader 분리 연결 오프라인 harness."""

    def __init__(self, tmp_path: Path, sample_risk_input_factory) -> None:
        self.tmp_path = tmp_path
        self.clock = _MutableClock(_at(8, 50))
        self.calendar = build_explicit_schedule(timezone=_KST, trading_days=[_DAY], window=_WINDOW)
        self.tracker = MarketHealthTracker(_health_thresholds())
        self.adapter = MarketSupervisorAdapter(clock=self.clock)
        self.latest = LatestMarketStateStore()
        self.rolling = RollingTradeHistoryStore(
            retention=RollingRetentionPolicy(
                hard_max_events=1000, hard_max_age_seconds=Decimal("86400")
            )
        )
        active_path = tmp_path / "active.sqlite3"
        self.scheduler_store = ActiveDecisionStore(active_path)
        self.fast_loop_store = ActiveDecisionStore(active_path)
        self.ledger = SQLiteLedger(tmp_path / "ledger.sqlite3")
        self.broker = PaperBrokerAdapter(
            self.ledger,
            initial_cash=CashSnapshot(
                currency=_KRW,
                amount=Decimal("100000000"),
                account_role=AccountRole.PAPER,
                as_of=_at(8, 50),
            ),
        )
        self.journal = SqliteTriggerJournal(tmp_path / "journal.sqlite3")
        self.runner = _ScriptedRunner()
        self.orchestrator_results: list[Any] = []
        self._seq = 0
        self._build_execution_stack(sample_risk_input_factory)

    def next_market_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _build_execution_stack(self, sample_risk_input_factory) -> None:
        bridge = TriggerOrderBridge(
            journal=self.journal,
            generator=OrderIntentGenerator(),
            resolver=QuantityResolver(),
            broker=self.broker,
            ledger=self.ledger,
        )
        engine = TriggerEngine()
        service = PaperPortfolioContextService(
            ledger_source=self.ledger, market_state_source=_InlineMarketSource(self.latest)
        )
        inner = PaperExecutionCoordinator(
            engine=engine, bridge=bridge, portfolio_context_service=service
        )
        self.engine = engine
        self.bridge = bridge
        self.coordinator = _CountingCoordinator(inner)
        ri = sample_risk_input_factory(
            action=AnalysisAction.BUY,
            target_weight_percent=Percent("4"),
            correlation_id="rehearsal-idem",
        )
        self._allocator_inputs = StaticExecutionInputsProvider(
            allocator_decision=ri.allocator_decision.model_copy(
                update={"universe": "KR_LARGE", "created_at": _at(8, 50)}
            ),
            portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
        )
        gate = SessionHealthExecutionGate(calendar=self.calendar, tracker=self.tracker)
        self.orchestrator = FastLoopExecutionOrchestrator(
            active_reader=self.fast_loop_store,
            latest_store=self.latest,
            rolling_store=self.rolling,
            execution_gate=gate,
            execution_inputs_provider=self._allocator_inputs,
            coordinator=self.coordinator,  # type: ignore[arg-type]
        )

    def restart_fast_loop_stack(self) -> None:
        """process restart: engine/coordinator/orchestrator만 재생성, DB/store 재사용."""
        bridge = TriggerOrderBridge(
            journal=self.journal,
            generator=OrderIntentGenerator(),
            resolver=QuantityResolver(),
            broker=self.broker,
            ledger=self.ledger,
        )
        self.engine = TriggerEngine()
        service = PaperPortfolioContextService(
            ledger_source=self.ledger, market_state_source=_InlineMarketSource(self.latest)
        )
        inner = PaperExecutionCoordinator(
            engine=self.engine, bridge=bridge, portfolio_context_service=service
        )
        self.bridge = bridge
        self.coordinator = _CountingCoordinator(inner)
        gate = SessionHealthExecutionGate(calendar=self.calendar, tracker=self.tracker)
        self.orchestrator = FastLoopExecutionOrchestrator(
            active_reader=self.fast_loop_store,
            latest_store=self.latest,
            rolling_store=self.rolling,
            execution_gate=gate,
            execution_inputs_provider=self._allocator_inputs,
            coordinator=self.coordinator,  # type: ignore[arg-type]
        )

    def publish_initial_hold(self) -> None:
        now = self.clock()
        analysis = _analysis_decision(
            action=AnalysisAction.HOLD, decision_id="init-hold", created_at=now
        )
        self.scheduler_store.publish(
            DecisionPublicationCandidate(
                snapshot=_snapshot(analysis),
                plan=None,
                valid_from=now,
                expires_at=now + _DAY_DELTA,
            ),
            now=now,
        )

    def record_transport(self, kind: str) -> None:
        at = self.clock()
        self.tracker.record_transport_event(kind=kind, at=at, now=at)

    def prime_execution_ready(self) -> None:
        """execution-ready health — 기존 transport epoch를 reconnect로 리셋하지 않는다."""
        at = self.clock()
        if self.tracker._epoch_connected_at is None:
            self.tracker.record_transport_event(kind="connected", at=at, now=at)
        if not self.tracker.all_subscribed:
            self.tracker.record_transport_event(kind="all_subscribed", at=at, now=at)
        self.tracker.record_market_event(event_type="best_bid_ask", at=at, now=at)

    def _next_seq(self) -> int:
        return self.next_market_seq()

    def _monitor_callbacks(self) -> tuple[
        Callable[[MonitorEvidence], None], Callable[[AppliedMarketUpdate], None]
    ]:
        def on_evidence(evidence: MonitorEvidence) -> None:
            # monitor connect/drop transport evidence는 harness가 직접 기록한 epoch를 덮어쓰지 않는다.
            if evidence.kind == "apply":
                self.adapter.forward_monitor_evidence(evidence, self.tracker)

        def on_applied(update: AppliedMarketUpdate) -> None:
            # integration 계약: health tracker는 orchestrator gate 직전에 최신 apply를 반영해야 한다.
            et = "trade" if update.event_type.value == "trade" else "best_bid_ask"
            self.tracker.record_market_event(
                event_type=et, at=update.applied_at, now=update.applied_at
            )
            self.orchestrator_results.append(self.orchestrator.handle_applied_update(update))

        return on_evidence, on_applied

    def run_monitor_events(self, events: list[MarketEvent]) -> None:
        on_evidence, on_applied = self._monitor_callbacks()
        monitor = MarketMonitor(
            store=self.latest,
            rolling_store=self.rolling,
            source_factory=lambda: ReplayMarketEventSource(list(events)),
            clock=self.clock,
            session_id="rehearsal",
            max_events=len(events),
            on_evidence=on_evidence,
            on_applied_update=on_applied,
        )
        asyncio.run(monitor.run())

    def run_monitor_reconnect(
        self, first: MarketEvent, second_events: list[MarketEvent]
    ) -> None:
        on_evidence, on_applied = self._monitor_callbacks()
        sources = iter([_DropAfterFirst(first), ReplayMarketEventSource(second_events)])

        async def _sleep(_s: float) -> None:
            await asyncio.sleep(0)

        monitor = MarketMonitor(
            store=self.latest,
            rolling_store=self.rolling,
            source_factory=lambda: next(sources),
            clock=self.clock,
            sleep=_sleep,
            session_id="rehearsal-reconnect",
            policy=ReconnectPolicy(initial_delay_seconds=0.0, max_attempts=5),
            max_events=len(second_events) + 1,
            on_evidence=on_evidence,
            on_applied_update=on_applied,
        )
        asyncio.run(monitor.run())

    def apply_quote_trade(self) -> None:
        at = self.clock()
        seq_q = self._next_seq()
        seq_t = self._next_seq()
        self.run_monitor_events([_quote(sequence=seq_q, at=at), _trade(sequence=seq_t, at=at)])

    def run_scheduler_tick(self, at: datetime) -> Any:
        sched = DecisionRefreshScheduler(
            market=Market.KR,
            calendar=self.calendar,
            runner=self.runner,
            store=self.scheduler_store,
            slots=_SLOTS,
            timezone=_KST,
            clock=_SequencedClock([at, at]),
            sleep=_fake_sleep,
            poll_interval_seconds=0.01,
            slot_grace_seconds=600.0,
            max_ticks=1,
        )
        return asyncio.run(sched.run())

    def committed_results(self) -> list[Any]:
        return [
            r
            for r in self.orchestrator_results
            if getattr(r, "status", None) is FastLoopExecutionStatus.COMMITTED
        ]

    def fill_count(self) -> int:
        return self.ledger._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]

    def journal_rows(self) -> int:
        return self.journal._conn.execute("SELECT COUNT(*) FROM trigger_fire_journal").fetchone()[0]

    def position_qty(self) -> Decimal:
        pos = self.broker.get_position("005930", Market.KR, AccountRole.PAPER)
        return Decimal("0") if pos is None else pos.quantity

    def cash_amount(self) -> Decimal:
        return self.broker.get_cash(_KRW, AccountRole.PAPER).amount


def test_full_day_two_loop_rehearsal(tmp_path: Path, sample_risk_input_factory) -> None:
    h = TwoLoopHarness(tmp_path, sample_risk_input_factory)
    coord_at_start = h.coordinator.calls

    # 08:50 PRE_OPEN — initial HOLD direct publish, market 이벤트 없음(tracker 오염 방지)
    h.publish_initial_hold()
    pre_gate = SessionHealthExecutionGate(
        calendar=h.calendar, tracker=MarketHealthTracker(_health_thresholds())
    )
    pre_snap = pre_gate.evaluate(market=Market.KR, now=h.clock())
    assert pre_snap.session.state.value == "PRE_OPEN"
    assert h.coordinator.calls == coord_at_start

    # 09:00 OPEN — connected but not execution-ready (WARMING, quote 없음)
    h.clock.set(_at(9, 0))
    h.record_transport("connected")
    warming_coord = h.coordinator.calls
    assert warming_coord == coord_at_start

    # 09:00:30 all_subscribed, 09:01 first quote/trade → health READY (still HOLD → no fill)
    h.clock.set(_at(9, 0, 30))
    h.record_transport("all_subscribed")
    h.clock.set(_at(9, 1))
    before_buy = h.coordinator.calls
    h.apply_quote_trade()
    assert h.coordinator.calls > before_buy
    assert not h.committed_results()

    # 09:30 s1 BUY publication
    h.clock.set(_at(9, 30))
    s1_summary = h.run_scheduler_tick(_at(9, 30))
    assert s1_summary.slots_run == 1
    assert h.scheduler_store.slot_states(Market.KR, _DAY)["s1"] is SlotState.PUBLISHED

    # 09:31 condition true → exactly one COMMITTED fill
    h.clock.set(_at(9, 31))
    h.apply_quote_trade()
    assert len(h.committed_results()) == 1
    qty_after_fill = h.position_qty()
    cash_after_fill = h.cash_amount()
    assert qty_after_fill == Decimal("57")
    assert cash_after_fill == Decimal("96010000")

    # 09:32 additional ticks → duplicate fill 0
    h.clock.set(_at(9, 32))
    fills_before = len(h.committed_results())
    h.apply_quote_trade()
    assert len(h.committed_results()) == fills_before
    assert h.fill_count() == 1

    # 10:00 transport drop + 10:00:02 reconnect epoch reset — duplicate 0
    h.clock.set(_at(10, 0))
    h.record_transport("disconnect")
    h.clock.set(_at(10, 0, 2))
    h.record_transport("connected")
    h.record_transport("all_subscribed")
    at_reconnect = h.clock()
    seq_q = h._next_seq()
    seq_t = h._next_seq()
    h.run_monitor_reconnect(
        _trade(sequence=seq_q, at=at_reconnect),
        [_quote(sequence=1, at=at_reconnect), _trade(sequence=1, at=at_reconnect)],
    )
    assert h.fill_count() == 1
    assert h.position_qty() == qty_after_fill

    # 10:30 fast-loop process restart — journal blocks duplicate
    h.clock.set(_at(10, 30))
    h.restart_fast_loop_stack()
    h.apply_quote_trade()
    assert h.fill_count() == 1
    assert h.position_qty() == qty_after_fill
    assert h.cash_amount() == cash_after_fill

    # 11:00 s2 HOLD replaces BUY
    h.clock.set(_at(11, 0))
    s2_summary = h.run_scheduler_tick(_at(11, 0))
    assert s2_summary.slots_run == 1
    assert h.scheduler_store.slot_states(Market.KR, _DAY)["s2"] is SlotState.PUBLISHED
    active_after_s2 = h.fast_loop_store.read_active(Market.KR, "005930")
    assert active_after_s2 is not None and active_after_s2.decision_id == "s2-hold"

    h.clock.set(_at(11, 1))
    coord_before_hold_tick = h.coordinator.calls
    h.apply_quote_trade()
    assert h.coordinator.calls > coord_before_hold_tick
    assert h.fill_count() == 1

    # 13:00 s3 runner failure — HOLD(s2) 유지
    hold_before_s3 = h.fast_loop_store.read_active(Market.KR, "005930")
    assert hold_before_s3 is not None
    h.clock.set(_at(13, 0))
    s3_summary = h.run_scheduler_tick(_at(13, 0))
    assert s3_summary.runner_failures == 1
    assert h.scheduler_store.slot_states(Market.KR, _DAY)["s3"] is SlotState.FAILED
    after_s3 = h.fast_loop_store.read_active(Market.KR, "005930")
    assert after_s3 is not None and after_s3.decision_id == hold_before_s3.decision_id
    for ev in s3_summary.evidence:
        assert ev.reason is None or "scripted" not in (ev.reason or "").lower()
        assert ev.reason is None or "failure" not in (ev.reason or "").lower()

    h.clock.set(_at(13, 1))
    h.apply_quote_trade()
    assert h.fill_count() == 1

    # 14:50 s4 false-condition BUY publication
    h.clock.set(_at(14, 50))
    s4_summary = h.run_scheduler_tick(_at(14, 50))
    assert s4_summary.slots_run == 1
    final_active = h.fast_loop_store.read_active(Market.KR, "005930")
    assert final_active is not None and final_active.decision_id == "s4-buy-false"

    h.clock.set(_at(14, 51))
    h.apply_quote_trade()
    assert h.fill_count() == 1

    # slow-loop terminal assertions
    assert h.runner.calls == ["s1", "s2", "s3", "s4"]
    states = h.scheduler_store.slot_states(Market.KR, _DAY)
    assert states["s1"] is SlotState.PUBLISHED
    assert states["s2"] is SlotState.PUBLISHED
    assert states["s3"] is SlotState.FAILED
    assert states["s4"] is SlotState.PUBLISHED
    assert len(states) == 4
    history = h.scheduler_store.list_history(Market.KR, "005930")
    assert len(history) == 4  # init HOLD + s1 + s2 + s4 (s3 failed → no new publication)

    # scheduler restart exactly-once
    restart_runner = _NeverRunner()
    restart_summary = asyncio.run(
        DecisionRefreshScheduler(
            market=Market.KR,
            calendar=h.calendar,
            runner=restart_runner,
            store=h.scheduler_store,
            slots=_SLOTS,
            timezone=_KST,
            clock=_SequencedClock([_at(15, 0), _at(15, 0)]),
            sleep=_fake_sleep,
            poll_interval_seconds=0.01,
            slot_grace_seconds=600.0,
            max_ticks=2,
            owner_id="restart-owner",
        ).run()
    )
    assert restart_runner.calls == 0
    assert restart_summary.slots_run == 0
    assert h.fast_loop_store.read_active(Market.KR, "005930").decision_id == "s4-buy-false"

    # fast-loop final counts
    assert len(h.committed_results()) == 1
    assert h.journal_rows() >= 1


def test_supervisor_post_close_stops_monitor(tmp_path: Path, sample_risk_input_factory) -> None:
    """15:30 POST_CLOSE supervisor cancel + 15:31 event coordinator 0."""
    h = TwoLoopHarness(tmp_path, sample_risk_input_factory)
    h.publish_initial_hold()
    h.clock.set(_at(9, 0))
    h.record_transport("connected")
    h.clock.set(_at(9, 0, 30))
    h.record_transport("all_subscribed")
    h.clock.set(_at(9, 1))
    h.prime_execution_ready()

    cancels = {"count": 0}

    class _OneShotMonitor:
        async def run(self) -> MonitorSummary:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancels["count"] += 1
                raise
            return MonitorSummary(
                monitor_session_id="post-close",
                connection_attempts=1,
                consecutive_failures=0,
                applied=0,
                duplicate=0,
                out_of_order=0,
                stream_mismatch=0,
                future_event_error=0,
                final_state=MonitorState.STOPPED,
            )

    timeline = [_at(9, 5), _at(15, 30), _at(15, 31), _at(16, 0)]
    tick = {"i": 0}

    def clock() -> datetime:
        return timeline[min(tick["i"], len(timeline) - 1)]

    async def sleep(_s: float) -> None:
        tick["i"] += 1
        await asyncio.sleep(0)

    sup = MarketSupervisor(
        market=Market.KR,
        calendar=h.calendar,
        monitor_factory=_OneShotMonitor,
        tracker=h.tracker,
        clock=clock,
        sleep=sleep,
        policy=SupervisorPolicy(
            poll_interval_seconds=0.01,
            max_restarts_in_window=2,
            restart_window_seconds=600.0,
            restart_backoff_seconds=0.0,
        ),
        max_ticks=len(timeline),
    )

    async def _run() -> None:
        summary = await sup.run()
        assert summary.final_state is SupervisorState.STOPPED
        assert cancels["count"] == 1
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
        ]
        assert pending == []

    asyncio.run(_run())

    h.clock.set(_at(15, 31))
    coord_before = h.coordinator.calls
    h.apply_quote_trade()
    assert h.coordinator.calls == coord_before


def test_failure_active_store_corruption(tmp_path: Path, sample_risk_input_factory) -> None:
    h = TwoLoopHarness(tmp_path, sample_risk_input_factory)
    h.publish_initial_hold()
    h.clock.set(_at(9, 0))
    h.record_transport("connected")
    h.clock.set(_at(9, 0, 30))
    h.record_transport("all_subscribed")
    h.clock.set(_at(9, 1))
    at = h.clock()
    h.run_monitor_events(
        [_quote(sequence=h.next_market_seq(), at=at), _trade(sequence=h.next_market_seq(), at=at)]
    )
    h.orchestrator_results.clear()
    h.coordinator.calls = 0

    class _CorruptReader:
        def read_active(self, market: Market | str, symbol: str):
            raise PublicationError("corrupt")

    h.orchestrator._active_reader = _CorruptReader()  # type: ignore[method-assign]
    h.clock.set(_at(9, 2))
    at2 = h.clock()
    on_evidence, on_applied = h._monitor_callbacks()
    monitor = MarketMonitor(
        store=h.latest,
        rolling_store=h.rolling,
        source_factory=lambda: ReplayMarketEventSource(
            [
                _quote(sequence=h.next_market_seq(), at=at2),
                _trade(sequence=h.next_market_seq(), at=at2),
            ]
        ),
        clock=h.clock,
        session_id="corrupt",
        max_events=2,
        on_evidence=on_evidence,
        on_applied_update=on_applied,
    )
    asyncio.run(monitor.run())
    corrupt = [
        r
        for r in h.orchestrator_results
        if r.status is FastLoopExecutionStatus.ACTIVE_DECISION_CORRUPT
    ]
    assert corrupt
    second = h.orchestrator.handle_applied_update(
        AppliedMarketUpdate(
            market=Market.KR,
            symbol="005930",
            event_type=__import__(
                "market_data.models", fromlist=["MarketEventType"]
            ).MarketEventType.TRADE,
            provider="kis",
            channel=_TRADE_CH,
            sequence=h.next_market_seq(),
            applied_at=at2,
        )
    )
    assert second.status is FastLoopExecutionStatus.GLOBAL_TERMINAL_FAIL_CLOSED
    assert h.coordinator.calls == 0


def test_failure_health_starvation_no_restart(tmp_path: Path, sample_risk_input_factory) -> None:
    h = TwoLoopHarness(tmp_path, sample_risk_input_factory)
    # starvation 전용 tight threshold tracker
    h.tracker = MarketHealthTracker(
        HealthThresholds(
            subscription_grace_seconds=30.0,
            heartbeat_timeout_seconds=86400.0,
            minimum_stable_uptime_seconds=1.0,
            flapping_window_seconds=600.0,
            flapping_max_short_epochs=5,
            flapping_min_uptime_seconds=30.0,
            flapping_min_market_events=1,
            quote_grace_seconds=30.0,
            quote_starvation_seconds=30.0,
            max_quote_age_seconds=60.0,
        )
    )
    h.orchestrator._execution_gate = SessionHealthExecutionGate(  # type: ignore[attr-defined]
        calendar=h.calendar, tracker=h.tracker
    )
    h.publish_initial_hold()
    h.clock.set(_at(9, 0))
    h.record_transport("connected")
    h.record_transport("all_subscribed")
    h.clock.set(_at(9, 5))
    gate = SessionHealthExecutionGate(calendar=h.calendar, tracker=h.tracker)
    snap = gate.evaluate(market=Market.KR, now=h.clock())
    assert not snap.health.is_execution_ready
    from market_data.health_policy import MarketDataHealthStatus

    assert snap.health.market_data is MarketDataHealthStatus.STARVED
    assert h.coordinator.calls == 0


def test_failure_scheduler_evidence_sink(tmp_path: Path) -> None:
    store = ActiveDecisionStore(tmp_path / "active.sqlite3")
    runner = _ScriptedRunner()

    def _boom(_evidence: object) -> None:
        raise RuntimeError("super-secret-sink")

    summary = asyncio.run(
        DecisionRefreshScheduler(
            market=Market.KR,
            calendar=build_explicit_schedule(timezone=_KST, trading_days=[_DAY], window=_WINDOW),
            runner=runner,
            store=store,
            slots=_SLOTS,
            timezone=_KST,
            clock=_SequencedClock([_at(9, 30), _at(9, 30), _at(11, 0), _at(11, 0)]),
            sleep=_fake_sleep,
            poll_interval_seconds=0.01,
            slot_grace_seconds=600.0,
            max_ticks=3,
            on_evidence=_boom,
        ).run()
    )
    assert summary.final_state is SchedulerState.FAILED_CLOSED
    assert runner.calls == ["s1"]
    assert store.read_active(Market.KR, "005930") is not None
    assert store.slot_states(Market.KR, _DAY)["s1"] is SlotState.PUBLISHED
    assert "s2" not in store.slot_states(Market.KR, _DAY)
    for ev in summary.evidence:
        assert ev.reason is None or "secret" not in ev.reason


def test_failure_fast_loop_evidence_sink_after_committed(
    tmp_path: Path, sample_risk_input_factory
) -> None:
    h = TwoLoopHarness(tmp_path, sample_risk_input_factory)
    h.publish_initial_hold()
    h.clock.set(_at(9, 0))
    h.record_transport("connected")
    h.clock.set(_at(9, 0, 30))
    h.record_transport("all_subscribed")
    h.clock.set(_at(9, 30))
    h.run_scheduler_tick(_at(9, 30))
    h.clock.set(_at(9, 31))
    at = h.clock()
    # quote만 선반영 — trade 한 번의 orchestrator 호출에서 COMMITTED+sink 실패를 재현
    quote = _quote(sequence=h.next_market_seq(), at=at)
    h.latest.apply(quote, now=at)
    h.tracker.record_market_event(event_type="best_bid_ask", at=at, now=at)

    def _boom(_evidence: object) -> None:
        raise RuntimeError("leak-secret-token")

    h.orchestrator = FastLoopExecutionOrchestrator(
        active_reader=h.fast_loop_store,
        latest_store=h.latest,
        rolling_store=h.rolling,
        execution_gate=SessionHealthExecutionGate(calendar=h.calendar, tracker=h.tracker),
        execution_inputs_provider=h._allocator_inputs,
        coordinator=h.coordinator,  # type: ignore[arg-type]
        on_evidence=_boom,
    )
    on_evidence, on_applied = h._monitor_callbacks()
    monitor = MarketMonitor(
        store=h.latest,
        rolling_store=h.rolling,
        source_factory=lambda: ReplayMarketEventSource(
            [_trade(sequence=h.next_market_seq(), at=at)]
        ),
        clock=h.clock,
        session_id="evidence-sink",
        max_events=1,
        on_evidence=on_evidence,
        on_applied_update=on_applied,
    )
    asyncio.run(monitor.run())
    assert len(h.committed_results()) == 1
    assert h.orchestrator_results[-1].status is FastLoopExecutionStatus.COMMITTED
    h.apply_quote_trade()
    assert h.orchestrator_results[-1].status is FastLoopExecutionStatus.GLOBAL_TERMINAL_FAIL_CLOSED
    assert h.fill_count() == 1
    assert "secret" not in str(h.orchestrator_results[-1].reason_code)
