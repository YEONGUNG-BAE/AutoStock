"""RTM-7c.5a/5b attended single-symbol paper-day diagnostic runtime.

This is a bounded, attended, paper-only composition root. It wires normalized market
events into the existing fast-loop paper execution stack and records diagnostic
evidence. It is not runtime activation, not an unattended daemon, and not a live
order path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sqlite3
import stat
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
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
from market_data.replay_source import ReplayMarketEventSource
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
PILOT_DB_FILES = frozenset({"active.sqlite3", "ledger.sqlite3", "trigger_journal.sqlite3"})
PILOT_DB_SIDECAR_SUFFIXES = (".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal")


class RuntimeOutcome:
    PASS = "PASS"
    NO_GO = "NO_GO"
    FAIL = "FAIL"


class AttendedPaperDayInputError(Exception):
    """Sanitized CLI/configuration input error."""


class AttendedPaperDayRuntimeError(Exception):
    """Sanitized runtime failure with a diagnostic stage/reason."""

    def __init__(self, stage: str, reason_code: str) -> None:
        super().__init__(reason_code)
        self.stage = stage
        self.reason_code = reason_code


class DiagnosticMarketSourceLifecycle(Protocol):
    def on_connect_attempt(self, *, at: datetime) -> None: ...

    def on_connected(self, *, at: datetime) -> None: ...

    def on_subscription_requested(self, *, tr_id: str | None, symbol: str | None, at: datetime) -> None: ...

    def on_subscription_ack(
        self, *, tr_id: str | None, symbol: str | None, accepted: bool, at: datetime
    ) -> None: ...

    def on_all_subscribed(self, *, at: datetime) -> None: ...

    def on_disconnected(self, *, at: datetime) -> None: ...


class DiagnosticSourceFactory(Protocol):
    """Single source-construction contract.

    The factory is invoked exactly once per connection attempt with a keyword-only
    ``lifecycle``. There is no no-arg fallback and no exception-based arity probing:
    a ``TypeError`` raised from inside the factory is a genuine source failure, never
    a signal to retry with a different signature.
    """

    def __call__(self, *, lifecycle: DiagnosticMarketSourceLifecycle) -> MarketEventSource: ...


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
    source_kind: str = "replay"
    reuse_pilot_db: bool = False

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
        if self.source_kind not in ("replay", "kis_live"):
            raise AttendedPaperDayInputError("source_kind must be replay or kis_live.")
        _validate_runtime_paths(self, reuse_pilot_db=self.reuse_pilot_db)


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
        self._handle = self._config.evidence_out.open("x", encoding="utf-8")

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


class PilotRuntimeLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(self._fd, b"locked\n")
        except FileExistsError as exc:
            raise AttendedPaperDayRuntimeError("lock", "runtime_lock_exists") from exc
        except OSError as exc:
            raise AttendedPaperDayRuntimeError("lock", "runtime_lock_failed") from exc

    def release(self) -> None:
        if self._fd is None:
            return
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()


class _CriticalStop(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class DiagnosticLifecycle:
    def __init__(
        self,
        *,
        counters: DiagnosticCounters,
        tracker: MarketHealthTracker | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._counters = counters
        self._tracker = tracker
        self._clock = clock
        self.all_subscribed = False
        self.rejected = False
        # Set by the startup probe within the running loop so readiness (ACK) can be
        # awaited directly instead of blocking on the first market event.
        self.ready_event: asyncio.Event | None = None

    def _signal_ready(self) -> None:
        if self.ready_event is not None:
            self.ready_event.set()

    def on_connect_attempt(self, *, at: datetime) -> None:
        self._counters.inc("connect_attempts")

    def on_connected(self, *, at: datetime) -> None:
        self._counters.inc("connected")
        if self._tracker is not None:
            self._tracker.record_transport_event(kind="connected", at=at, now=self._clock())

    def on_subscription_requested(
        self, *, tr_id: str | None, symbol: str | None, at: datetime
    ) -> None:
        self._counters.inc("subscription_requests")

    def on_subscription_ack(
        self, *, tr_id: str | None, symbol: str | None, accepted: bool, at: datetime
    ) -> None:
        if accepted:
            self._counters.inc("subscription_acks")
        else:
            self.rejected = True
            self._counters.inc("subscription_rejections")
            self._signal_ready()

    def on_all_subscribed(self, *, at: datetime) -> None:
        self.all_subscribed = True
        self._counters.inc("all_subscribed")
        self._counters.stamp("subscriptions_ready_at", at)
        if self._tracker is not None:
            self._tracker.record_transport_event(kind="all_subscribed", at=at, now=self._clock())
        self._signal_ready()

    def on_disconnected(self, *, at: datetime) -> None:
        self._counters.inc("disconnects")
        if self._tracker is not None:
            self._tracker.record_transport_event(kind="disconnect", at=at, now=self._clock())

    def on_kis_transport_event(self, event: object) -> None:
        at = getattr(event, "at", None)
        if not isinstance(at, datetime):
            at = self._clock()
        kind = getattr(event, "kind", None)
        tr_id = getattr(event, "tr_id", None)
        symbol = getattr(event, "symbol", None)
        if kind == "connected":
            self.on_connected(at=at)
        elif kind == "subscription_sent":
            self.on_subscription_requested(tr_id=tr_id, symbol=symbol, at=at)
        elif kind == "ack":
            self.on_subscription_ack(
                tr_id=tr_id,
                symbol=symbol,
                accepted=getattr(event, "rt_cd", None) == "0",
                at=at,
            )
        elif kind == "all_subscribed":
            self.on_all_subscribed(at=at)
        elif kind == "disconnect":
            self.on_disconnected(at=at)

    def now(self) -> datetime:
        return self._clock()


class DiagnosticReplayMarketEventSource:
    def __init__(
        self,
        source: ReplayMarketEventSource,
        *,
        lifecycle: DiagnosticMarketSourceLifecycle,
        clock: Callable[[], datetime],
    ) -> None:
        self._source = source
        self._lifecycle = lifecycle
        self._clock = clock

    async def events(self):
        at = self._clock() - timedelta(seconds=1)
        self._lifecycle.on_connected(at=at)
        self._lifecycle.on_subscription_requested(tr_id=TR_TRADE, symbol=PILOT_SYMBOL, at=at)
        self._lifecycle.on_subscription_ack(
            tr_id=TR_TRADE, symbol=PILOT_SYMBOL, accepted=True, at=at
        )
        self._lifecycle.on_subscription_requested(tr_id=TR_QUOTE, symbol=PILOT_SYMBOL, at=at)
        self._lifecycle.on_subscription_ack(
            tr_id=TR_QUOTE, symbol=PILOT_SYMBOL, accepted=True, at=at
        )
        self._lifecycle.on_all_subscribed(at=at)
        try:
            async for event in self._source.events():
                yield event
        finally:
            self._lifecycle.on_disconnected(at=self._clock())


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


def _validate_runtime_paths(
    config: AttendedPaperDayConfig, *, reuse_pilot_db: bool
) -> None:
    paths = {
        "evidence_out": config.evidence_out,
        "summary_out": config.summary_out,
        "db_dir": config.db_dir,
    }
    for name, path in paths.items():
        # ``is_symlink()`` is checked without an ``exists()`` guard so that a dangling
        # symlink (whose target is missing) is still rejected; ``exists()`` follows the
        # link and would report False for a dangling one, silently admitting it.
        if path.is_symlink():
            raise AttendedPaperDayInputError(f"{name} final component must not be a symlink.")
        parent = path.parent
        if parent.is_symlink():
            raise AttendedPaperDayInputError(f"{name} parent must not be a symlink.")
    evidence_resolved = config.evidence_out.resolve(strict=False)
    summary_resolved = config.summary_out.resolve(strict=False)
    db_resolved = config.db_dir.resolve(strict=False)
    if evidence_resolved == summary_resolved:
        raise AttendedPaperDayInputError("evidence_out and summary_out must differ.")
    for name, resolved in (
        ("evidence_out", evidence_resolved),
        ("summary_out", summary_resolved),
    ):
        if resolved == db_resolved or db_resolved in resolved.parents:
            raise AttendedPaperDayInputError(f"{name} must not be inside db_dir.")
        if resolved in db_resolved.parents:
            raise AttendedPaperDayInputError(f"db_dir must not be inside {name}.")
    if config.db_dir.exists():
        if not config.db_dir.is_dir():
            raise AttendedPaperDayInputError("db_dir must be a directory.")
        entries = [
            p
            for p in config.db_dir.iterdir()
            if p.name not in {".DS_Store", ".paper_day.lock"}
        ]
        sidecars = [
            p.name
            for p in entries
            if any(p.name.endswith(suffix) for suffix in PILOT_DB_SIDECAR_SUFFIXES)
        ]
        if sidecars:
            raise AttendedPaperDayInputError("pilot DB sidecar files are not allowed.")
        if entries and not reuse_pilot_db:
            raise AttendedPaperDayInputError(
                "existing non-empty pilot db_dir requires reuse_pilot_db."
            )
    for path in (config.evidence_out, config.summary_out):
        if path.exists():
            raise AttendedPaperDayInputError(f"{path.name} output already exists.")


def build_diagnostic_stack(
    *,
    config: AttendedPaperDayConfig,
    counters: DiagnosticCounters,
    on_execution_evidence: Callable[[object], None],
) -> DiagnosticStack:
    config.db_dir.mkdir(parents=True, exist_ok=True)
    # Track SQLite-backed resources in construction order so that, if any later
    # constructor fails, the already-opened handles are closed in reverse order and
    # the original exception is preserved (the temp db_dir stays deletable).
    opened: list[object] = []
    try:
        active_store = ActiveDecisionStore(config.db_dir / "active.sqlite3")
        opened.append(active_store)
        ledger = SQLiteLedger(config.db_dir / "ledger.sqlite3")
        opened.append(ledger)
        journal = SqliteTriggerJournal(config.db_dir / "trigger_journal.sqlite3")
        opened.append(journal)
        broker = PaperBrokerAdapter(
            ledger,
            initial_cash=CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("100000000"),
                account_role=AccountRole.PAPER,
                as_of=_session_at(config.session_date, time(8, 50)),
            ),
        )
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
    except BaseException:
        _close_partial_resources(opened)
        raise
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


def _close_partial_resources(resources: list[object]) -> None:
    for resource in reversed(resources):
        close = getattr(resource, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


def run_attended_paper_day(
    *,
    config: AttendedPaperDayConfig,
    source_factory: DiagnosticSourceFactory,
    run_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    actual_run_id = run_id or uuid.uuid4().hex
    counters = DiagnosticCounters()
    recorder = EvidenceRecorder(config=config, run_id=actual_run_id)
    stack: DiagnosticStack | None = None
    stop_reason = "completed"
    outcome = RuntimeOutcome.PASS
    now_fn = clock or (lambda: datetime.now(tz=KST))
    last_heartbeat: datetime | None = None
    execution_results: list[FastLoopExecutionResult] = []
    lock = PilotRuntimeLock(config.db_dir / ".paper_day.lock")
    lifecycle: DiagnosticLifecycle | None = None
    # Only the stack owner observes the journal. ``None`` means "never evaluated"
    # (e.g. lock conflict, path failure, factory failure before the stack exists),
    # so the summary writer never re-opens the DB to recompute it.
    nonterminal_journal: int | None = None
    ran_market_loop = False
    # Output-ownership state. Output files belong to the lock owner only, and are
    # written only after path ownership is confirmed (validation passed + lock held).
    evidence_owned = False
    summary_path_owned = False

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

    # --- Admission phase 1: validation. A validation failure writes NOTHING: no
    #     evidence, no summary, no symlink target — only a memory result is returned.
    try:
        validate_attended_paper_day_inputs(config)
    except AttendedPaperDayInputError:
        stop_reason = "invalid_input"
        outcome = RuntimeOutcome.FAIL
        counters.reason(stop_reason)
        return _build_summary(
            config=config,
            run_id=actual_run_id,
            counters=counters,
            nonterminal_journal=nonterminal_journal,
            stop_reason=stop_reason,
            outcome=outcome,
        )

    # --- Admission phase 2: runtime lock. Until the lock is held we own no output
    #     path, so a lock conflict also returns a memory result with zero writes.
    try:
        lock.acquire()
    except AttendedPaperDayRuntimeError as exc:
        stop_reason = exc.reason_code
        outcome = _outcome_for_stop_reason(stop_reason)
        counters.reason(stop_reason)
        return _build_summary(
            config=config,
            run_id=actual_run_id,
            counters=counters,
            nonterminal_journal=nonterminal_journal,
            stop_reason=stop_reason,
            outcome=outcome,
        )
    # Lock held: validation already confirmed the output paths, so the lock owner now
    # owns them. Every path from here MUST run the finalize/cleanup with lock release
    # as the last bounded step, including under a fatal exception.
    summary_path_owned = True
    body_fatal: BaseException | None = None

    try:
        try:
            recorder.open()
        except AttendedPaperDayRuntimeError:
            raise
        except Exception as exc:
            raise AttendedPaperDayRuntimeError("evidence", "evidence_failed") from exc
        evidence_owned = True
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
            elif status == FastLoopExecutionStatus.UNCERTAIN.value:
                counters.inc("journal_uncertain")
            elif status == FastLoopExecutionStatus.RECONCILE_REQUIRED.value:
                counters.inc("reconcile_required")
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

        try:
            stack = build_diagnostic_stack(
                config=config, counters=counters, on_execution_evidence=on_execution_evidence
            )
        except AttendedPaperDayRuntimeError:
            raise
        except Exception as exc:
            raise AttendedPaperDayRuntimeError("startup", "db_failed") from exc
        lifecycle = DiagnosticLifecycle(counters=counters, tracker=stack.tracker, clock=now_fn)
        if stack.journal.list_nonterminal():
            raise AttendedPaperDayRuntimeError("preflight", "nonterminal_journal")
        record(now_fn(), "preflight", "startup_completed", snapshot=counters.snapshot())

        if config.startup_only:
            stop_reason = "startup_only"
            if config.source_kind == "kis_live":
                # The probe now fully classifies startup: a source exception becomes
                # source_failed, ACK rejection subscription_rejected, exhaustion
                # transport_not_ready, timeout health_not_ready. No post-probe
                # re-derivation is needed (and none must silently downgrade a source
                # failure to health_not_ready).
                _run_live_startup_probe(
                    source_factory=source_factory,
                    lifecycle=lifecycle,
                    timeout_seconds=float(config.duration_seconds),
                )
        else:
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
                    # Counter ownership lives in on_execution_evidence; here we only
                    # halt the run so a single applied update is not double-counted.
                    raise _CriticalStop(result.status.value)
                heartbeat(update.applied_at)

            monitor = MarketMonitor(
                store=stack.latest,
                rolling_store=stack.rolling,
                source_factory=lambda: _source_with_lifecycle(
                    source_factory, lifecycle=lifecycle, source_kind=config.source_kind
                ),
                clock=now_fn,
                session_id=actual_run_id,
                max_runtime_seconds=float(config.duration_seconds),
                on_evidence=on_monitor_evidence,
                on_applied_update=on_applied,
            )
            try:
                asyncio.run(monitor.run())
            except Exception as exc:
                critical = _critical_stop_from_exception(exc)
                if critical is not None:
                    stop_reason = _normalize_critical_reason(critical.reason_code)
                    outcome = RuntimeOutcome.NO_GO
                else:
                    raise
            ran_market_loop = True
    except AttendedPaperDayRuntimeError as exc:
        stop_reason = exc.reason_code
        outcome = _outcome_for_stop_reason(stop_reason)
        counters.reason(exc.reason_code)
        with contextlib.suppress(Exception):
            record(now_fn(), exc.stage, "failed_closed", reason=exc.reason_code)
    except AttendedPaperDayInputError:
        stop_reason = "invalid_input"
        outcome = RuntimeOutcome.FAIL
        counters.reason(stop_reason)
    except (KeyboardInterrupt, asyncio.CancelledError):
        stop_reason = "graceful_stop"
        outcome = RuntimeOutcome.NO_GO
        with contextlib.suppress(Exception):
            record(now_fn(), "signal", "graceful_stop", reason=stop_reason)
    except MemoryError as exc:
        # MemoryError is an Exception subclass, but the process is in an unreliable
        # state: preserve its identity (re-raise) rather than reporting a normal FAIL
        # summary. Cleanup (incl. lock release) still runs below.
        body_fatal = exc
    except Exception:
        stop_reason = "internal_runtime_error"
        outcome = RuntimeOutcome.FAIL
        counters.reason(stop_reason)
        with contextlib.suppress(Exception):
            record(now_fn(), "runtime", "failed_closed", reason=stop_reason)
    except BaseException as exc:  # noqa: BLE001 — fatal (SystemExit/GeneratorExit)
        # Preserve the original fatal; cleanup (incl. lock release) still runs below.
        body_fatal = exc

    summary, cleanup_fatal = _finalize_run(
        config=config,
        run_id=actual_run_id,
        counters=counters,
        stack=stack,
        recorder=recorder,
        lock=lock,
        outcome=outcome,
        stop_reason=stop_reason,
        ran_market_loop=ran_market_loop,
        evidence_owned=evidence_owned,
        summary_path_owned=summary_path_owned,
        now_fn=now_fn,
    )
    # Fatal precedence: an operation (body) fatal outranks any cleanup fatal.
    fatal = body_fatal if body_fatal is not None else cleanup_fatal
    if fatal is not None:
        raise fatal
    return summary


def _build_summary(
    *,
    config: AttendedPaperDayConfig,
    run_id: str,
    counters: DiagnosticCounters,
    nonterminal_journal: int | None,
    stop_reason: str,
    outcome: str,
) -> dict[str, object]:
    # Pure serialization of immutable scalars. It never imports/opens SQLite, so a
    # lock conflict (no stack) reports a null journal observation with zero DB opens.
    return {
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
        "outcome": outcome,
        "stop_reason": stop_reason,
        "nonterminal_journal": nonterminal_journal,
        "counters": counters.snapshot(),
        "source_kind": config.source_kind,
    }


def _finalize_run(
    *,
    config: AttendedPaperDayConfig,
    run_id: str,
    counters: DiagnosticCounters,
    stack: DiagnosticStack | None,
    recorder: EvidenceRecorder,
    lock: PilotRuntimeLock,
    outcome: str,
    stop_reason: str,
    ran_market_loop: bool,
    evidence_owned: bool,
    summary_path_owned: bool,
    now_fn: Callable[[], datetime],
) -> tuple[dict[str, object], BaseException | None]:
    """Single outer lifecycle owner: decide the final result, persist it, release.

    Cleanup order is fixed: stack reverse-close -> final journal observation ->
    completion verdict -> final evidence record -> recorder close -> create-new
    summary publish -> runtime lock release. The runtime lock release is always the
    last bounded cleanup. The returned summary dict is byte-identical to any persisted
    summary file. Cleanup-step fatals are captured (not masked) and the first one is
    returned for the caller to re-raise after lock release; ordinary cleanup failures
    are normalized into the runtime result (resource_close_failure / evidence_failed /
    summary_failed).
    """
    nonterminal_journal: int | None = None
    cleanup_fatal: BaseException | None = None

    # 1. Stack reverse-close (DiagnosticStack.close swallows ordinary per-resource
    #    errors into a failure count; a fatal there is captured as a cleanup fatal).
    if stack is not None:
        with contextlib.suppress(Exception):
            nonterminal_journal = len(stack.journal.list_nonterminal())
        try:
            failures = stack.close()
        except BaseException as exc:  # noqa: BLE001
            cleanup_fatal = cleanup_fatal or exc
            failures = 0
        if failures:
            counters.inc("resource_close_failures", failures)
            stop_reason = "resource_close_failure"
            outcome = RuntimeOutcome.NO_GO
    counters.stamp("shutdown_completed_at", now_fn())

    # 2. Completion verdict only refines a still-PASS market-loop run.
    if ran_market_loop and outcome == RuntimeOutcome.PASS:
        outcome, stop_reason = _completion_verdict(counters, nonterminal_journal)

    # 3. Final evidence record + 4. recorder close. An evidence failure here downgrades
    #    to FAIL/evidence_failed BEFORE the summary is built, so no PASS summary is ever
    #    persisted after an evidence failure.
    if evidence_owned:
        try:
            recorder.record(
                recorded_at=now_fn(),
                stage="shutdown",
                event="finalized",
                reason_code=stop_reason,
            )
        except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
            cleanup_fatal = cleanup_fatal or exc
        except Exception:
            outcome = RuntimeOutcome.FAIL
            stop_reason = "evidence_failed"
    try:
        recorder.close()
    except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
        cleanup_fatal = cleanup_fatal or exc
    except Exception:
        if outcome != RuntimeOutcome.FAIL:
            outcome = RuntimeOutcome.FAIL
            stop_reason = "evidence_failed"

    # 5. Build the immutable summary from the now-final outcome and publish it
    #    create-new/atomically. Only the lock owner with confirmed path ownership
    #    writes the file. A publish failure yields FAIL/summary_failed with the file
    #    absent (or another owner's file untouched) — never a partial/visible summary.
    summary = _build_summary(
        config=config,
        run_id=run_id,
        counters=counters,
        nonterminal_journal=nonterminal_journal,
        stop_reason=stop_reason,
        outcome=outcome,
    )
    if summary_path_owned:
        publish = _publish_summary_create_new(
            config.summary_out, json.dumps(summary, sort_keys=True, indent=2)
        )
        if publish != _SUMMARY_WRITTEN:
            summary = {**summary, "outcome": RuntimeOutcome.FAIL, "stop_reason": "summary_failed"}

    # 6. Runtime lock release — the last bounded cleanup on every path.
    try:
        lock.release()
    except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
        cleanup_fatal = cleanup_fatal or exc
    except Exception:
        pass
    return summary, cleanup_fatal


# Summary publication outcomes (minimal scope for the runtime summary).
_SUMMARY_WRITTEN = "WRITTEN"
_SUMMARY_NOT_WRITTEN = "NOT_WRITTEN"
_SUMMARY_PUBLISHED_INCOMPLETE = "PUBLISHED_INCOMPLETE"
_SUMMARY_PUBLICATION_UNCERTAIN = "PUBLICATION_UNCERTAIN"
_SUMMARY_TEMP_PREFIX = ".paper_day_summary."


def _summary_open_flags() -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _fsync_directory(directory: Path) -> None:
    dir_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _publish_summary_create_new(path: Path, text: str) -> str:
    """Atomic create-new summary publish; no overwrite, no symlink follow.

    Bytes are written to a same-directory temp opened ``O_EXCL|O_NOFOLLOW``, fsynced,
    then hard-linked to the create-new destination (link fails if the destination
    already exists). The destination therefore only ever appears fully written — a
    partial summary is never visible. Reuses the RTM-7c.4x artifact-file publish
    pattern, limited to the minimum the runtime summary needs.
    """
    payload = text.encode("utf-8")
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return _SUMMARY_NOT_WRITTEN
    temp_path = parent / f"{_SUMMARY_TEMP_PREFIX}{secrets.token_hex(16)}"
    fd: int | None = None
    temp_created = False
    published = False
    temp_stat: os.stat_result | None = None
    try:
        try:
            fd = os.open(str(temp_path), _summary_open_flags(), 0o600)
        except OSError:
            return _SUMMARY_NOT_WRITTEN
        temp_created = True
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            try:
                written = os.write(fd, view[offset:])
            except OSError:
                return _SUMMARY_NOT_WRITTEN
            if written <= 0:
                return _SUMMARY_NOT_WRITTEN
            offset += written
        try:
            os.fsync(fd)
            temp_stat = os.fstat(fd)
        except OSError:
            return _SUMMARY_NOT_WRITTEN
        try:
            os.link(temp_path, path)
            published = True
        except FileExistsError:
            return _SUMMARY_NOT_WRITTEN
        except OSError:
            try:
                dest_stat = os.lstat(path)
            except FileNotFoundError:
                return _SUMMARY_NOT_WRITTEN
            except OSError:
                return _SUMMARY_PUBLICATION_UNCERTAIN
            if (
                stat.S_ISREG(dest_stat.st_mode)
                and temp_stat is not None
                and dest_stat.st_dev == temp_stat.st_dev
                and dest_stat.st_ino == temp_stat.st_ino
            ):
                published = True
            else:
                return _SUMMARY_PUBLICATION_UNCERTAIN
        try:
            dest_stat = os.lstat(path)
        except OSError:
            return _SUMMARY_PUBLICATION_UNCERTAIN
        if (
            not stat.S_ISREG(dest_stat.st_mode)
            or temp_stat is None
            or dest_stat.st_dev != temp_stat.st_dev
            or dest_stat.st_ino != temp_stat.st_ino
        ):
            return _SUMMARY_PUBLICATION_UNCERTAIN
        # Destination content is correct and durable on the inode; a parent-dir fsync
        # failure affects only directory-entry durability, not the returned/persisted
        # consistency, so it does not downgrade WRITTEN.
        with contextlib.suppress(Exception):
            _fsync_directory(parent)
        return _SUMMARY_WRITTEN
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if temp_created:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)


def _source_with_lifecycle(
    source_factory: DiagnosticSourceFactory,
    *,
    lifecycle: DiagnosticLifecycle,
    source_kind: str,
) -> MarketEventSource:
    lifecycle.on_connect_attempt(at=lifecycle.now())
    try:
        source = source_factory(lifecycle=lifecycle)
    except AttendedPaperDayRuntimeError:
        raise
    except Exception as exc:
        # Any factory failure (including an internal TypeError) is a sanitized source
        # failure — never a signal to retry the call with a different signature.
        raise AttendedPaperDayRuntimeError("source", "source_failed") from exc
    if source_kind != "kis_live" and isinstance(source, ReplayMarketEventSource):
        return DiagnosticReplayMarketEventSource(
            source, lifecycle=lifecycle, clock=lifecycle.now
        )
    return source


def _run_live_startup_probe(
    *,
    source_factory: DiagnosticSourceFactory,
    lifecycle: DiagnosticLifecycle,
    timeout_seconds: float,
) -> None:
    async def probe() -> None:
        lifecycle.ready_event = asyncio.Event()
        source = _source_with_lifecycle(
            source_factory, lifecycle=lifecycle, source_kind="kis_live"
        )

        async def consume() -> None:
            async for _ in source.events():
                if lifecycle.all_subscribed or lifecycle.rejected:
                    return

        # Return as soon as ACK readiness (or rejection) fires — do not block on the
        # first market event. The consumer task is then cancelled and the source's
        # async generator is closed exactly once.
        consumer = asyncio.create_task(consume())
        ready = asyncio.create_task(lifecycle.ready_event.wait())
        try:
            await asyncio.wait({consumer, ready}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()
            with contextlib.suppress(BaseException):
                await ready

        # A source/consumer exception is the highest-priority startup outcome and
        # must never be downgraded to health_not_ready. Inspect the consumer task's
        # exception explicitly instead of suppressing it.
        if consumer.done():
            consumer_exc = consumer.exception()
            if consumer_exc is not None and not isinstance(
                consumer_exc, asyncio.CancelledError
            ):
                raise AttendedPaperDayRuntimeError("source", "source_failed") from consumer_exc
        else:
            consumer.cancel()
            with contextlib.suppress(BaseException):
                await consumer

        if lifecycle.rejected:
            raise AttendedPaperDayRuntimeError("transport", "subscription_rejected")
        if lifecycle.all_subscribed:
            return
        # The consumer reached normal exhaustion (or readiness fired without an ACK
        # state) before subscription readiness was observed.
        if consumer.done():
            raise AttendedPaperDayRuntimeError("transport", "transport_not_ready")

    try:
        asyncio.run(asyncio.wait_for(probe(), timeout=timeout_seconds))
    except asyncio.TimeoutError as exc:
        if lifecycle.all_subscribed and not lifecycle.rejected:
            return
        raise AttendedPaperDayRuntimeError("transport", "health_not_ready") from exc


def _normalize_critical_reason(reason: str) -> str:
    return "journal_uncertain" if reason == "uncertain" else reason


def _completion_verdict(
    counters: DiagnosticCounters, nonterminal_journal: int | None
) -> tuple[str, str]:
    """Mechanical PASS/NO_GO verdict for a completed market-loop run.

    Precedence is fixed: critical journal/reconcile -> resource close -> transport
    subscription -> trade/quote -> health -> trigger -> PASS. The first unmet
    criterion wins, yielding a stable NO_GO reason.
    """
    c = counters.values
    if nonterminal_journal:
        return RuntimeOutcome.NO_GO, "nonterminal_journal"
    if c.get("reconcile_required", 0) > 0:
        return RuntimeOutcome.NO_GO, "reconcile_required"
    if c.get("journal_uncertain", 0) > 0:
        return RuntimeOutcome.NO_GO, "journal_uncertain"
    if c.get("resource_close_failures", 0) > 0:
        return RuntimeOutcome.NO_GO, "resource_close_failure"
    if c.get("subscription_rejections", 0) > 0:
        return RuntimeOutcome.NO_GO, "subscription_rejected"
    if c.get("all_subscribed", 0) == 0:
        return RuntimeOutcome.NO_GO, "transport_not_ready"
    if c.get("normalized_trades", 0) == 0:
        return RuntimeOutcome.NO_GO, "trade_not_observed"
    if c.get("normalized_quotes", 0) == 0:
        return RuntimeOutcome.NO_GO, "quote_not_observed"
    if c.get("health_pass", 0) == 0:
        return RuntimeOutcome.NO_GO, "health_not_ready"
    if c.get("trigger_evaluations", 0) == 0:
        return RuntimeOutcome.NO_GO, "trigger_not_evaluated"
    return RuntimeOutcome.PASS, "completed"


def _critical_stop_from_exception(exc: BaseException) -> _CriticalStop | None:
    cursor: BaseException | None = exc
    while cursor is not None:
        if isinstance(cursor, _CriticalStop):
            return cursor
        cursor = cursor.__cause__
    return None


def _outcome_for_stop_reason(stop_reason: str) -> str:
    if stop_reason in {
        "health_not_ready",
        "transport_not_ready",
        "subscription_rejected",
        "trade_not_observed",
        "quote_not_observed",
        "trigger_not_evaluated",
        "journal_uncertain",
        "reconcile_required",
        "nonterminal_journal",
        "resource_close_failure",
        "runtime_lock_exists",
    }:
        return RuntimeOutcome.NO_GO
    return RuntimeOutcome.FAIL


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
    # connect_attempts / disconnects are owned solely by the lifecycle (one event per
    # connection attempt). The monitor still emits connect/drop evidence rows, but
    # counting them here too would double-count the same physical attempt.
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
    latest = stack.latest.peek(PILOT_MARKET, PILOT_SYMBOL, now=at) if stack is not None else None
    session = ExplicitMarketScheduleProvider(
        timezone=KST,
        schedule={
            at.astimezone(KST).date(): SessionWindow(
                pre_open=time(8, 30),
                open=time(9, 0),
                close=time(15, 30),
                post_close_end=time(16, 0),
            )
        },
    ).session_at(PILOT_MARKET, at)
    verdict = stack.tracker.evaluate(session=session, now=at) if stack is not None else None
    active = stack.active_store.read_active(PILOT_MARKET, PILOT_SYMBOL) if stack is not None else None
    return {
        "connected": counters.values.get("connected", 0) > 0,
        "subscriptions_ready": counters.values.get("subscription_acks", 0) >= 2,
        "last_trade_age_ms": _age_ms(latest.trade.trade_at, at) if latest and latest.trade else None,
        "last_quote_age_ms": _age_ms(latest.quote.quote_at, at) if latest and latest.quote else None,
        "transport_health": verdict.transport.value if verdict is not None else "UNKNOWN",
        "market_data_health": verdict.market_data.value if verdict is not None else "UNKNOWN",
        "session_state": verdict.session_state if verdict is not None else str(session.state),
        "active_decision_id": active.decision_id if active is not None else None,
        "trigger_evaluations": counters.values.get("trigger_evaluations", 0),
        "execution_requests": counters.values.get("execution_requests", 0),
        "committed_orders": counters.values.get("journal_committed", 0),
        "nonterminal_journal": nonterminal,
    }


def _age_ms(event_at: datetime, now: datetime) -> int:
    return max(0, int((now - event_at).total_seconds() * 1000))


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
