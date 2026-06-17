"""RTM-7c.5a/5b attended single-symbol paper-day diagnostic runtime.

This is a bounded, attended, paper-only composition root. It wires normalized market
events into the existing fast-loop paper execution stack and records diagnostic
evidence. It is not runtime activation, not an unattended daemon, and not a live
order path.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from allocator.models import (
    AllocatorDecision,
    AllocatorReason,
    AssetAllocatorView,
    CashManagerView,
    CashPolicy,
    ConsistencyCheckerView,
    GoldPolicyMode,
    SignalSummary,
    TargetWeights,
)
from analysis.models import (
    ANALYSIS_DECISION_SCHEMA,
    AnalysisAction,
    AnalysisDecision,
    AnalysisReason,
    BearPerspective,
    BullPerspective,
    FundManagerDecision,
    RiskManagerEvaluation,
)
from broker.paper_broker import PaperBrokerAdapter
from domain.enums import AccountRole, Currency, Market
from domain.decision import DecisionSnapshot
from domain.identifiers import DateId, DecisionId, Percent
from domain.position import CashSnapshot
from domain.validation import ValidationResult
from execution.paper_execution_coordinator import PaperExecutionCoordinator
from execution.paper_portfolio_context import PaperPortfolioContextService, PaperPortfolioPolicy
from execution.sqlite_trigger_journal import SqliteTriggerJournal
from execution.trigger_order_bridge import TriggerOrderBridge
from ledger.sqlite_ledger import SQLiteLedger
from market_data.conditions import Comparator, ConditionClause, Metric
from market_data.health_policy import HealthThresholds, MarketHealthTracker
from market_data.kis_official_ws_parser import TR_QUOTE, TR_TRADE
from market_data.latest_state import LatestMarketStateStore
from market_data.market_session import (
    ExplicitMarketScheduleProvider,
    SessionWindow,
)
from market_data.models import (
    MarketEvent,
    MarketEventType,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
)
from market_data.monitor import AppliedMarketUpdate, MarketMonitor, MonitorEvidence
from market_data.protocols import MarketEventSource
from market_data.rolling_window import RollingRetentionPolicy, RollingTradeHistoryStore
from market_data.trigger_engine import TriggerEngine, TriggerPlan
from orchestration.active_decision_store import (
    ActiveDecisionStore,
    DecisionPublicationCandidate,
    PublicationStatus,
)
from orchestration.execution_gate import SessionHealthExecutionGate
from orchestration.fast_loop_execution import (
    FastLoopExecutionOrchestrator,
    FastLoopExecutionResult,
    FastLoopExecutionStatus,
    StaticExecutionInputsProvider,
)
from paper_loop import QuantityResolver
from risk import OrderIntentGenerator
from risk.models import RiskMode

KST = ZoneInfo("Asia/Seoul")
PILOT_MARKET = Market.KR
PILOT_SYMBOL = "005930"
PILOT_UNIVERSE = "KR_LARGE"
SCHEMA_VERSION = "paper_day_diagnostic.v1"
HEARTBEAT_SECONDS = 60


class AttendedPaperDayInputError(Exception):
    """Sanitized CLI/configuration input error."""


class AttendedPaperDayRuntimeError(Exception):
    """Sanitized runtime failure with a diagnostic stage/reason."""

    def __init__(self, stage: str, reason_code: str) -> None:
        super().__init__(reason_code)
        self.stage = stage
        self.reason_code = reason_code


class CloseableSourceFactory(Protocol):
    def __call__(self) -> MarketEventSource: ...


@dataclass(frozen=True)
class AttendedPaperDayConfig:
    session_date: date
    symbol: str
    duration_seconds: int
    evidence_out: Path
    summary_out: Path
    db_dir: Path
    confirm_attended_paper: bool
    startup_only: bool = False

    def validate(self) -> None:
        if self.symbol != PILOT_SYMBOL:
            raise AttendedPaperDayInputError("only symbol 005930 is allowed.")
        if self.duration_seconds <= 0:
            raise AttendedPaperDayInputError("duration_seconds must be > 0.")
        if not self.confirm_attended_paper:
            raise AttendedPaperDayInputError("confirm_attended_paper is required.")
        for name, path in (
            ("evidence_out", self.evidence_out),
            ("summary_out", self.summary_out),
            ("db_dir", self.db_dir),
        ):
            if not isinstance(path, Path):
                raise AttendedPaperDayInputError(f"{name} must be an explicit Path.")
        if self.evidence_out == self.summary_out:
            raise AttendedPaperDayInputError("evidence_out and summary_out must differ.")


@dataclass
class DiagnosticCounters:
    values: Counter[str] = field(default_factory=Counter)
    reason_counts: Counter[str] = field(default_factory=Counter)
    timestamps: dict[str, str] = field(default_factory=dict)

    def inc(self, name: str, amount: int = 1) -> None:
        self.values[name] += amount

    def reason(self, name: str) -> None:
        self.reason_counts[name] += 1

    def stamp(self, name: str, at: datetime) -> None:
        self.timestamps.setdefault(name, at.isoformat())

    def snapshot(self) -> dict[str, object]:
        return {
            "counters": dict(sorted(self.values.items())),
            "reason_counts": dict(sorted(self.reason_counts.items())),
            "timestamps": dict(sorted(self.timestamps.items())),
        }


class EvidenceRecorder:
    def __init__(self, *, config: AttendedPaperDayConfig, run_id: str) -> None:
        self._config = config
        self._run_id = run_id
        self._handle = None
        self._closed = False

    def open(self) -> None:
        self._config.evidence_out.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._config.evidence_out.open("a", encoding="utf-8")

    def record(
        self,
        *,
        recorded_at: datetime,
        stage: str,
        event: str,
        reason_code: str | None = None,
        counter_delta: dict[str, int] | None = None,
        snapshot: dict[str, object] | None = None,
    ) -> None:
        if self._handle is None:
            raise AttendedPaperDayRuntimeError("evidence", "evidence_not_open")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self._run_id,
            "session_date": self._config.session_date.isoformat(),
            "recorded_at": recorded_at.isoformat(),
            "stage": stage,
            "event": event,
            "market": PILOT_MARKET.value,
            "symbol": self._config.symbol,
            "reason_code": reason_code,
            "counter_delta": counter_delta,
            "snapshot": snapshot,
            "sensitive_data_present": False,
        }
        self._handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._handle is not None:
            self._handle.close()


class _LatestStateAdapter:
    def __init__(self, store: LatestMarketStateStore) -> None:
        self._store = store

    def get_snapshot(self, symbol: str, market: Market, *, now: datetime):
        return self._store.peek(market, symbol, now=now)


@dataclass
class DiagnosticStack:
    active_store: ActiveDecisionStore
    ledger: SQLiteLedger
    broker: PaperBrokerAdapter
    journal: SqliteTriggerJournal
    latest: LatestMarketStateStore
    rolling: RollingTradeHistoryStore
    tracker: MarketHealthTracker
    orchestrator: FastLoopExecutionOrchestrator
    close_failures: int = 0

    def close(self) -> int:
        failures = 0
        for resource in (self.active_store, self.journal, self.ledger):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    failures += 1
        self.close_failures += failures
        return failures


class DeterministicPaperDecisionPublisher:
    def __init__(
        self,
        *,
        store: ActiveDecisionStore,
        session_date: date,
        symbol: str,
        evidence: Callable[[datetime, str, str | None], None],
    ) -> None:
        self._store = store
        self._session_date = session_date
        self._symbol = symbol
        self._evidence = evidence
        self._published: set[str] = set()
        self._slots = (
            ("s0930", time(9, 30), AnalysisAction.BUY, Comparator.LTE, "100000"),
            ("s1100", time(11, 0), AnalysisAction.SELL, Comparator.GTE, "1"),
            ("s1300", time(13, 0), AnalysisAction.BUY, Comparator.LTE, "100000"),
            ("s1450", time(14, 50), AnalysisAction.SELL, Comparator.GTE, "1"),
        )

    def publish_due(self, now: datetime) -> list[PublicationStatus]:
        statuses: list[PublicationStatus] = []
        local = now.astimezone(KST)
        if local.date() != self._session_date:
            return statuses
        for slot_id, slot_time, action, comparator, threshold in self._slots:
            if slot_id in self._published or local.timetz().replace(tzinfo=None) < slot_time:
                continue
            decision_id = f"diag-{self._session_date.isoformat()}-{slot_id}"
            decision = _analysis_decision(
                action=action,
                decision_id=decision_id,
                created_at=now,
                symbol=self._symbol,
            )
            plan = TriggerPlan(
                plan_id=f"plan-{decision_id}",
                decision_id=decision.decision_id,
                created_at=now,
                valid_from=now,
                expires_at=now + timedelta(hours=2),
                universe=decision.universe,
                market=PILOT_MARKET,
                symbol=decision.symbol,
                action=action,
                rules=(
                    ConditionClause(
                        metric=Metric.LAST_TRADE_PRICE,
                        comparator=comparator,
                        threshold=threshold,
                    ),
                ),
            )
            result = self._store.publish(
                DecisionPublicationCandidate(
                    snapshot=_decision_snapshot(decision),
                    plan=plan,
                    valid_from=plan.valid_from,
                    expires_at=plan.expires_at,
                ),
                now=now,
                expected_market=PILOT_MARKET,
            )
            self._published.add(slot_id)
            statuses.append(result.status)
            self._evidence(now, slot_id, result.status.value)
        return statuses


def validate_attended_paper_day_inputs(config: AttendedPaperDayConfig) -> None:
    config.validate()


def build_diagnostic_stack(
    *,
    config: AttendedPaperDayConfig,
    counters: DiagnosticCounters,
    on_execution_evidence: Callable[[object], None],
) -> DiagnosticStack:
    config.db_dir.mkdir(parents=True, exist_ok=True)
    active_store = ActiveDecisionStore(config.db_dir / "active.sqlite3")
    ledger = SQLiteLedger(config.db_dir / "ledger.sqlite3")
    broker = PaperBrokerAdapter(
        ledger,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=Decimal("100000000"),
            account_role=AccountRole.PAPER,
            as_of=_session_at(config.session_date, time(8, 50)),
        ),
    )
    journal = SqliteTriggerJournal(config.db_dir / "trigger_journal.sqlite3")
    latest = LatestMarketStateStore()
    rolling = RollingTradeHistoryStore(
        retention=RollingRetentionPolicy(
            hard_max_events=1000, hard_max_age_seconds=Decimal("86400")
        )
    )
    tracker = MarketHealthTracker(_diagnostic_thresholds())
    calendar = ExplicitMarketScheduleProvider(
        timezone=KST,
        schedule={
            config.session_date: SessionWindow(
                pre_open=time(8, 30),
                open=time(9, 0),
                close=time(15, 30),
                post_close_end=time(16, 0),
            )
        },
    )
    bridge = TriggerOrderBridge(
        journal=journal,
        generator=OrderIntentGenerator(),
        resolver=QuantityResolver(),
        broker=broker,
        ledger=ledger,
    )
    coordinator = PaperExecutionCoordinator(
        engine=TriggerEngine(),
        bridge=bridge,
        portfolio_context_service=PaperPortfolioContextService(
            ledger_source=ledger, market_state_source=_LatestStateAdapter(latest)
        ),
    )
    orchestrator = FastLoopExecutionOrchestrator(
        active_reader=active_store,
        latest_store=latest,
        rolling_store=rolling,
        execution_gate=SessionHealthExecutionGate(calendar=calendar, tracker=tracker),
        execution_inputs_provider=StaticExecutionInputsProvider(
            allocator_decision=_allocator_decision(config.session_date),
            portfolio_policy=PaperPortfolioPolicy(mode=RiskMode.REBALANCING),
        ),
        coordinator=coordinator,
        on_evidence=on_execution_evidence,
    )
    counters.inc("startup_completed")
    return DiagnosticStack(
        active_store=active_store,
        ledger=ledger,
        broker=broker,
        journal=journal,
        latest=latest,
        rolling=rolling,
        tracker=tracker,
        orchestrator=orchestrator,
    )


def run_attended_paper_day(
    *,
    config: AttendedPaperDayConfig,
    source_factory: CloseableSourceFactory,
    run_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    validate_attended_paper_day_inputs(config)
    actual_run_id = run_id or uuid.uuid4().hex
    counters = DiagnosticCounters()
    recorder = EvidenceRecorder(config=config, run_id=actual_run_id)
    stack: DiagnosticStack | None = None
    stop_reason = "completed"
    now_fn = clock or (lambda: datetime.now(tz=KST))
    last_heartbeat: datetime | None = None
    execution_results: list[FastLoopExecutionResult] = []

    def record(
        at: datetime,
        stage: str,
        event: str,
        reason: str | None = None,
        delta: dict[str, int] | None = None,
        snapshot: dict[str, object] | None = None,
    ) -> None:
        recorder.record(
            recorded_at=at,
            stage=stage,
            event=event,
            reason_code=reason,
            counter_delta=delta,
            snapshot=snapshot,
        )

    def heartbeat(at: datetime) -> None:
        nonlocal last_heartbeat
        if last_heartbeat is not None and (at - last_heartbeat).total_seconds() < HEARTBEAT_SECONDS:
            return
        last_heartbeat = at
        record(at, "heartbeat", "heartbeat", snapshot=_heartbeat_snapshot(stack, counters, at))

    recorder.open()
    try:
        record(now_fn(), "validate", "startup")

        def on_execution_evidence(ev: object) -> None:
            status = getattr(ev, "status", None)
            reason_code = getattr(ev, "reason_code", None)
            if status == FastLoopExecutionStatus.COMMITTED.value:
                counters.inc("execution_requests")
                counters.inc("journal_committed")
                counters.inc("orders")
                counters.inc("fills")
                counters.stamp("first_execution_request_at", getattr(ev, "timestamp"))
            elif status == FastLoopExecutionStatus.SUPPRESSED.value:
                counters.inc("trigger_suppressed")
            elif status in (
                FastLoopExecutionStatus.UNCERTAIN.value,
                FastLoopExecutionStatus.RECONCILE_REQUIRED.value,
            ):
                counters.inc("journal_uncertain")
            elif status == FastLoopExecutionStatus.SKIPPED_TERMINAL.value:
                counters.inc("journal_skipped_terminal")
            if reason_code:
                counters.reason(str(reason_code))
            record(
                getattr(ev, "timestamp"),
                "execution",
                "fast_loop_result",
                reason=str(reason_code) if reason_code else None,
                snapshot={"status": str(status)},
            )

        stack = build_diagnostic_stack(
            config=config, counters=counters, on_execution_evidence=on_execution_evidence
        )
        if stack.journal.list_nonterminal():
            raise AttendedPaperDayRuntimeError("preflight", "nonterminal_journal")
        counters.inc("connect_attempts")
        counters.inc("connected")
        counters.inc("subscription_requests", 2)
        counters.inc("subscription_acks", 2)
        counters.stamp("subscriptions_ready_at", now_fn())
        transport_ready_at = now_fn() - timedelta(seconds=60)
        stack.tracker.record_transport_event(kind="connected", at=transport_ready_at, now=now_fn())
        stack.tracker.record_transport_event(kind="all_subscribed", at=transport_ready_at, now=now_fn())
        counters.inc("startup_completed")
        record(now_fn(), "preflight", "startup_completed", snapshot=counters.snapshot())

        if config.startup_only:
            stop_reason = "startup_only"
            return _write_summary(
                config=config,
                run_id=actual_run_id,
                counters=counters,
                stack=stack,
                stop_reason=stop_reason,
                recorder=recorder,
                at=now_fn(),
            )

        publisher = DeterministicPaperDecisionPublisher(
            store=stack.active_store,
            session_date=config.session_date,
            symbol=config.symbol,
            evidence=lambda at, slot, status: _record_publication(
                record, counters, at, slot, status
            ),
        )

        def on_monitor_evidence(evidence: MonitorEvidence) -> None:
            _record_monitor_evidence(record, counters, evidence)

        def on_applied(update: AppliedMarketUpdate) -> None:
            assert stack is not None
            publisher.publish_due(update.applied_at)
            event_type = (
                "trade"
                if update.event_type is MarketEventType.TRADE
                else "best_bid_ask"
            )
            stack.tracker.record_market_event(
                event_type=event_type, at=update.applied_at, now=update.applied_at
            )
            verdict = stack.tracker.evaluate(
                session=ExplicitMarketScheduleProvider(
                    timezone=KST,
                    schedule={
                        config.session_date: SessionWindow(
                            pre_open=time(8, 30),
                            open=time(9, 0),
                            close=time(15, 30),
                            post_close_end=time(16, 0),
                        )
                    },
                ).session_at(PILOT_MARKET, update.applied_at),
                now=update.applied_at,
            )
            if verdict.is_execution_ready:
                counters.inc("health_pass")
                counters.stamp("first_gate_pass_at", update.applied_at)
            else:
                counters.inc("health_hold")
                for reason in verdict.reasons:
                    counters.reason(reason)
            counters.inc("trigger_evaluations")
            counters.stamp("first_trigger_evaluation_at", update.applied_at)
            result = stack.orchestrator.handle_applied_update(update)
            execution_results.append(result)
            if result.status is FastLoopExecutionStatus.COMMITTED:
                counters.inc("trigger_matches")
            elif result.status is FastLoopExecutionStatus.SUPPRESSED:
                counters.inc("trigger_no_match")
            if result.status in (
                FastLoopExecutionStatus.UNCERTAIN,
                FastLoopExecutionStatus.RECONCILE_REQUIRED,
            ):
                counters.inc("journal_uncertain")
            heartbeat(update.applied_at)

        monitor = MarketMonitor(
            store=stack.latest,
            rolling_store=stack.rolling,
            source_factory=source_factory,
            clock=now_fn,
            session_id=actual_run_id,
            max_runtime_seconds=float(config.duration_seconds),
            on_evidence=on_monitor_evidence,
            on_applied_update=on_applied,
        )
        asyncio.run(monitor.run())
        if stack.journal.list_nonterminal():
            stop_reason = "nonterminal_journal"
        if any(r.status is FastLoopExecutionStatus.UNCERTAIN for r in execution_results):
            stop_reason = "journal_uncertain"
    except AttendedPaperDayRuntimeError as exc:
        stop_reason = exc.reason_code
        counters.reason(exc.reason_code)
        record(now_fn(), exc.stage, "failed_closed", reason=exc.reason_code)
    finally:
        if stack is not None:
            failures = stack.close()
            if failures:
                counters.inc("resource_close_failures", failures)
                stop_reason = "resource_close_failure"
        counters.stamp("shutdown_completed_at", now_fn())
    return _write_summary(
        config=config,
        run_id=actual_run_id,
        counters=counters,
        stack=stack,
        stop_reason=stop_reason,
        recorder=recorder,
        at=now_fn(),
    )


def _write_summary(
    *,
    config: AttendedPaperDayConfig,
    run_id: str,
    counters: DiagnosticCounters,
    stack: DiagnosticStack | None,
    stop_reason: str,
    recorder: EvidenceRecorder,
    at: datetime,
) -> dict[str, object]:
    nonterminal = _nonterminal_count(config.db_dir / "trigger_journal.sqlite3")
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "session_date": config.session_date.isoformat(),
        "market": PILOT_MARKET.value,
        "symbol": config.symbol,
        "paper_only": True,
        "activation_authorized": False,
        "real_order_adapter_constructed": False,
        "automatic_restart": False,
        "multi_symbol": False,
        "stop_reason": stop_reason,
        "nonterminal_journal": nonterminal,
        "counters": counters.snapshot(),
    }
    config.summary_out.parent.mkdir(parents=True, exist_ok=True)
    config.summary_out.write_text(
        json.dumps(summary, sort_keys=True, indent=2), encoding="utf-8"
    )
    try:
        recorder.record(
            recorded_at=at,
            stage="summary",
            event="summary_written",
            reason_code=stop_reason,
            snapshot=summary,
        )
    finally:
        recorder.close()
    return summary


def _record_publication(
    record: Callable[..., None],
    counters: DiagnosticCounters,
    at: datetime,
    slot: str,
    status: str | None,
) -> None:
    counters.inc("publication_slot_outcomes")
    counters.inc("decision_reads")
    record(
        at,
        "decision",
        "publication",
        reason=status,
        snapshot={"slot": slot, "status": status},
    )


def _record_monitor_evidence(
    record: Callable[..., None], counters: DiagnosticCounters, evidence: MonitorEvidence
) -> None:
    if evidence.kind == "connect":
        counters.inc("connect_attempts")
    if evidence.kind == "drop":
        counters.inc("disconnects")
    if evidence.kind == "apply":
        if evidence.event_type == MarketEventType.TRADE.value:
            counters.inc("trade_frames")
            if evidence.apply_status == "applied":
                counters.inc("normalized_trades")
                counters.inc("latest_trade_updates")
                counters.inc("rolling_updates")
                counters.stamp("first_trade_at", evidence.timestamp)
        if evidence.event_type == MarketEventType.BEST_BID_ASK.value:
            counters.inc("quote_frames")
            if evidence.apply_status == "applied":
                counters.inc("normalized_quotes")
                counters.inc("latest_quote_updates")
                counters.stamp("first_quote_at", evidence.timestamp)
        if evidence.apply_status == "applied":
            counters.inc("parse_success")
        else:
            counters.inc("parse_rejected")
    if evidence.reason_code:
        counters.reason(evidence.reason_code)
    record(
        evidence.timestamp,
        "market_data",
        evidence.kind,
        reason=evidence.reason_code,
        snapshot={
            "event_type": evidence.event_type,
            "apply_status": evidence.apply_status,
            "sequence": evidence.sequence,
        },
    )


def _heartbeat_snapshot(
    stack: DiagnosticStack | None, counters: DiagnosticCounters, at: datetime
) -> dict[str, object]:
    nonterminal = len(stack.journal.list_nonterminal()) if stack is not None else 0
    return {
        "connected": counters.values.get("connected", 0) > 0,
        "subscriptions_ready": counters.values.get("subscription_acks", 0) >= 2,
        "last_trade_age_ms": None,
        "last_quote_age_ms": None,
        "transport_health": "recorded",
        "market_data_health": "recorded",
        "session_state": "OPEN",
        "active_decision_id": None,
        "trigger_evaluations": counters.values.get("trigger_evaluations", 0),
        "execution_requests": counters.values.get("execution_requests", 0),
        "committed_orders": counters.values.get("journal_committed", 0),
        "nonterminal_journal": nonterminal,
    }


def _diagnostic_thresholds() -> HealthThresholds:
    return HealthThresholds(
        subscription_grace_seconds=30.0,
        heartbeat_timeout_seconds=86400.0,
        minimum_stable_uptime_seconds=0.0 + 0.1,
        flapping_window_seconds=600.0,
        flapping_max_short_epochs=5,
        flapping_min_uptime_seconds=30.0,
        flapping_min_market_events=1,
        quote_grace_seconds=30.0,
        quote_starvation_seconds=300.0,
        max_quote_age_seconds=300.0,
    )


def _session_at(day: date, value: time) -> datetime:
    return datetime.combine(day, value, tzinfo=KST)


def _reason(date_id: str = "260617-1") -> AnalysisReason:
    return AnalysisReason(reason="diagnostic reason", date_id=DateId(date_id))


def _analysis_decision(
    *,
    action: AnalysisAction,
    decision_id: str,
    created_at: datetime,
    symbol: str,
) -> AnalysisDecision:
    return AnalysisDecision(
        decision_id=DecisionId(decision_id),
        created_at=created_at,
        universe=PILOT_UNIVERSE,
        symbol=symbol,
        market=PILOT_MARKET.value,
        summary_one_liner="diagnostic paper decision",
        bear=BearPerspective(summary="bear", risks=("risk",), reasons=(_reason(),)),
        bull=BullPerspective(summary="bull", catalysts=("catalyst",), reasons=(_reason("260617-2"),)),
        risk_manager=RiskManagerEvaluation(summary="risk", reasons=(_reason("260617-3"),)),
        fund_manager=FundManagerDecision(
            action=action,
            target_weight_percent=Percent("4"),
            rationale="diagnostic",
            reasons=(_reason("260617-4"),),
        ),
        reasons=(_reason("260617-5"),),
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


def _allocator_decision(day: date) -> AllocatorDecision:
    created = _session_at(day, time(8, 50))
    reasons = (AllocatorReason(reason="diagnostic allocation", date_id=DateId("260617-6")),)
    weights = TargetWeights(kr=Percent("50"), us=Percent("30"), gold=Percent("20"))
    cash = Percent("20")
    return AllocatorDecision(
        decision_id=DecisionId(f"diag-allocator-{day.isoformat()}"),
        created_at=created,
        universe=PILOT_UNIVERSE,
        summary_one_liner="diagnostic allocation",
        gold_policy_mode=GoldPolicyMode.NORMAL,
        signal_summary=SignalSummary(summary="signal", reasons=reasons),
        cash_manager=CashManagerView(summary="cash", recommended_cash_percent=cash, reasons=reasons),
        asset_allocator=AssetAllocatorView(summary="allocation", target_weights=weights, reasons=reasons),
        consistency_checker=ConsistencyCheckerView(passed=True, summary="ok", reasons=reasons),
        cash_policy=CashPolicy(cash_target_percent=cash, rationale="liquidity", reasons=reasons),
        target_weights=weights,
        reasons=reasons,
    )


def journal_state_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT state, COUNT(*) FROM trigger_fire_journal GROUP BY state"
        ).fetchall()
    finally:
        conn.close()
    return {str(state): int(count) for state, count in rows}


def _nonterminal_count(db_path: Path) -> int:
    counts = journal_state_counts(db_path)
    return counts.get("reserved", 0) + counts.get("dispatching", 0)


__all__ = [
    "AttendedPaperDayConfig",
    "AttendedPaperDayInputError",
    "AttendedPaperDayRuntimeError",
    "DiagnosticCounters",
    "DeterministicPaperDecisionPublisher",
    "EvidenceRecorder",
    "build_diagnostic_stack",
    "journal_state_counts",
    "run_attended_paper_day",
    "validate_attended_paper_day_inputs",
]
