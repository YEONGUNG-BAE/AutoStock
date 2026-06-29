"""RTM-7c.5a/5b attended single-symbol paper-day diagnostic runtime.

This is a bounded, attended, paper-only composition root. It wires normalized market
events into the existing fast-loop paper execution stack and records diagnostic
evidence. It is not runtime activation, not an unattended daemon, and not a live
order path.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import secrets
import sqlite3
import stat
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
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
    MarketSessionState,
    SessionWindow,
)
from market_data.models import (
    MarketEvent,
    MarketEventType,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
)
from market_data.monitor import (
    AppliedMarketUpdate,
    MarketMonitor,
    MonitorEvidence,
    MonitorExhaustedError,
)
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
# Upper bound on awaiting the startup-probe consumer's cancellation/cleanup. This
# bounds the *probe await* for a source that delays its response to cancellation. It
# does NOT make an arbitrary source bounded: a source that ignores *every*
# CancelledError (including the one asyncio.run delivers during loop shutdown) cannot
# be terminated in-process and would require process isolation to bound. See the
# startup cancellation contract in the runtime contract doc — only cancellation-
# compliant sources (the real KisWsMarketEventSource is one) are bounded here.
PROBE_CLEANUP_TIMEOUT_SECONDS = 5.0
PILOT_DB_FILES = frozenset({"active.sqlite3", "ledger.sqlite3", "trigger_journal.sqlite3"})
PILOT_DB_SIDECAR_SUFFIXES = (".sqlite3-wal", ".sqlite3-shm", ".sqlite3-journal")


class RuntimeOutcome:
    PASS = "PASS"
    NO_GO = "NO_GO"
    FAIL = "FAIL"


class CleanupOutcome:
    CLEAN = "CLEAN"
    INCOMPLETE = "INCOMPLETE"
    FATAL = "FATAL"


class SummaryPublicationOutcome:
    WRITTEN = "WRITTEN"
    NOT_WRITTEN = "NOT_WRITTEN"
    PUBLISHED_INCOMPLETE = "PUBLISHED_INCOMPLETE"
    PUBLICATION_UNCERTAIN = "PUBLICATION_UNCERTAIN"


@dataclass(frozen=True)
class RuntimeLockReleaseResult:
    fd_closed: bool
    lock_unlinked: bool
    lock_absent_confirmed: bool
    identity_matched: bool | None = None
    reason_code: str | None = None
    fatal: BaseException | None = None


@dataclass(frozen=True)
class RuntimeLockAcquireCleanupResult:
    fd_closed: bool
    lock_unlinked: bool
    lock_absent_confirmed: bool
    identity_matched: bool | None = None
    reason_code: str | None = None
    fatal: BaseException | None = None


@dataclass(frozen=True)
class PartialCleanupResult:
    ordinary_failures: int
    fatal: BaseException | None


@dataclass(frozen=True)
class SummaryPublishResult:
    outcome: str
    reason_codes: tuple[str, ...] = ()
    # Publisher-internal operation/cleanup fatal (operation > cleanup precedence).
    # Carried in the structured result so publication state is not erased by propagation.
    fatal: BaseException | None = None


class AttendedPaperDayInputError(Exception):
    """Sanitized CLI/configuration input error."""


class AttendedPaperDayRuntimeError(Exception):
    """Sanitized runtime failure with a diagnostic stage/reason."""

    def __init__(self, stage: str, reason_code: str) -> None:
        super().__init__(reason_code)
        self.stage = stage
        self.reason_code = reason_code


class LiveSourceConfigGateError(Exception):
    """Live-source config/env gate failure (enabled, credential env names, symbol,
    settings load, URL config). Carries no secret/credential value — the source-open
    path maps it to the sanitized reason ``source_config_gate_failed``."""


class LiveSourceApprovalError(Exception):
    """Live-source approval-key issuance failure. Carries no app key/secret/approval
    key or raw HTTP response — mapped to the sanitized reason ``source_approval_failed``."""


class LiveSourceConnectError(Exception):
    """Live-source websocket connect failure (open/handshake/timeout). Carries no raw
    frame or credentialed URL — mapped to the sanitized reason ``source_connect_failed``."""


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

    def close(self) -> tuple[int, BaseException | None]:
        """모든 리소스 close를 시도하고, 첫 cleanup fatal을 보존한다."""
        failures = 0
        pending_fatal: BaseException | None = None
        # Reverse construction order (active_store, ledger, journal): the journal is
        # closed first and the active_store last.
        for resource in (self.journal, self.ledger, self.active_store):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
                    if pending_fatal is None:
                        pending_fatal = exc
                except Exception:
                    failures += 1
        self.close_failures += failures
        return failures, pending_fatal


class PilotRuntimeLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None
        # dev/ino of the inode we created; recorded only on a fully successful
        # acquire so release can refuse to unlink a replaced/foreign lock.
        self._identity: tuple[int, int] | None = None

    def acquire(self) -> None:
        """Partial-side-effect-safe 취득.

        ``os.open``(O_EXCL) 이후의 어떤 실패(fstat/write)도 우리가 만든 inode를
        close하고, 그 inode가 여전히 우리 것일 때만 unlink하여 stale lock/fd leak을
        남기지 않는다. fd와 identity는 완전 성공에서만 보존한다.
        """
        # Lock-parent creation is part of the stable admission boundary: any mkdir
        # failure is mapped to a sanitized lock reason (or, for a fatal, preserved by
        # identity) so a raw OSError/RuntimeError never escapes the taxonomy. No lock
        # fd is opened on this path, so there is no partial side effect to roll back.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except PermissionError as exc:
            raise AttendedPaperDayRuntimeError("lock", "runtime_lock_parent_unreadable") from exc
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EIO):
                raise AttendedPaperDayRuntimeError(
                    "lock", "runtime_lock_parent_unreadable"
                ) from exc
            raise AttendedPaperDayRuntimeError("lock", "runtime_lock_acquire_failed") from exc
        except Exception as exc:
            raise AttendedPaperDayRuntimeError("lock", "runtime_lock_acquire_uncertain") from exc

        try:
            fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise AttendedPaperDayRuntimeError("lock", "runtime_lock_exists") from exc
        except OSError as exc:
            raise AttendedPaperDayRuntimeError("lock", "runtime_lock_failed") from exc

        identity: tuple[int, int] | None = None
        try:
            st = os.fstat(fd)
            identity = (st.st_dev, st.st_ino)
            os.write(fd, b"locked\n")
        except (MemoryError, KeyboardInterrupt, SystemExit):
            # Operation fatal outranks any rollback cleanup fatal: roll back on a
            # best-effort basis (its own fatal is captured, not raised) and re-raise
            # the original operation fatal with its identity intact.
            self._abort_partial_acquire(fd, identity)
            raise
        except BaseException as exc:
            cleanup = self._abort_partial_acquire(fd, identity)
            if cleanup.fatal is not None:
                # Rollback cleanup fatal outranks an ordinary acquire error.
                raise cleanup.fatal
            clean_rollback = (
                cleanup.fd_closed
                and cleanup.lock_absent_confirmed
                and cleanup.reason_code is None
            )
            reason = (
                "runtime_lock_acquire_failed"
                if clean_rollback
                else "runtime_lock_acquire_uncertain"
            )
            cause = exc if isinstance(exc, Exception) else None
            raise AttendedPaperDayRuntimeError("lock", reason) from cause

        self._fd = fd
        self._identity = identity

    def _abort_partial_acquire(
        self, fd: int, identity: tuple[int, int] | None
    ) -> RuntimeLockAcquireCleanupResult:
        """부분 취득 롤백을 structured result로 관찰한다.

        fd close 실패는 더 이상 숨기지 않는다: close가 실패하면(또는 미확정이면)
        unlink가 성공해도 rollback은 ``runtime_lock_acquire_uncertain``으로 본다(fd
        leak 가능성). 우리가 만든 inode임을 확인할 때만 unlink하고, replaced/foreign
        lock은 건드리지 않는다. fatal은 raise하지 않고 결과에 담아 호출자가 operation
        fatal precedence를 적용하게 한다.
        """
        fd_closed = False
        lock_unlinked = False
        lock_absent_confirmed = False
        identity_matched: bool | None = None
        reason_code: str | None = None
        fatal: BaseException | None = None

        try:
            os.close(fd)
            fd_closed = True
        except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
            fatal = exc
        except OSError:
            reason_code = "runtime_lock_acquire_uncertain"

        try:
            st = os.lstat(self._path)
        except FileNotFoundError:
            lock_unlinked = True
            lock_absent_confirmed = True
        except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
            fatal = fatal or exc
        except OSError:
            reason_code = reason_code or "runtime_lock_acquire_uncertain"
        else:
            if identity is None or (st.st_dev, st.st_ino) != identity:
                identity_matched = False
                reason_code = reason_code or "runtime_lock_acquire_uncertain"
            else:
                identity_matched = True
                try:
                    os.unlink(self._path)
                    lock_unlinked = True
                    lock_absent_confirmed = True
                except FileNotFoundError:
                    lock_unlinked = True
                    lock_absent_confirmed = True
                except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
                    fatal = fatal or exc
                except OSError:
                    reason_code = reason_code or "runtime_lock_acquire_uncertain"

        if not fd_closed and reason_code is None and fatal is None:
            reason_code = "runtime_lock_acquire_uncertain"
        return RuntimeLockAcquireCleanupResult(
            fd_closed=fd_closed,
            lock_unlinked=lock_unlinked,
            lock_absent_confirmed=lock_absent_confirmed,
            identity_matched=identity_matched,
            reason_code=reason_code,
            fatal=fatal,
        )

    def release(self) -> RuntimeLockReleaseResult:
        """fd close와 identity-safe unlink를 분리 관찰한다.

        fd close가 실패해도 unlink identity 검사를 계속 시도한다. 취득한 inode와 다른
        lock(replaced/foreign)은 unlink하지 않고 ``runtime_lock_identity_mismatch``로
        보고한다. fatal은 raise하지 않고 결과에 담아 outer owner가 처리하게 한다.
        """
        fd_closed = self._fd is None
        lock_unlinked = False
        lock_absent_confirmed = False
        identity_matched: bool | None = None
        reason_code: str | None = None
        fatal: BaseException | None = None

        if self._fd is not None:
            fd = self._fd
            self._fd = None
            try:
                os.close(fd)
                fd_closed = True
            except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
                # An fd-close fatal is captured (not raised) so identity/unlink still
                # run and the outer finalizer owns fatal precedence; release() must
                # never escape the outer cleanup.
                fd_closed = False
                fatal = exc
            except OSError:
                fd_closed = False
                reason_code = "runtime_lock_release_failed"

        try:
            st = os.lstat(self._path)
        except FileNotFoundError:
            lock_unlinked = True
            lock_absent_confirmed = True
        except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
            fatal = fatal or exc
        except PermissionError:
            reason_code = reason_code or "runtime_lock_release_uncertain"
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EIO):
                reason_code = reason_code or "runtime_lock_release_uncertain"
            else:
                reason_code = reason_code or "runtime_lock_release_failed"
        else:
            if self._identity is not None and (st.st_dev, st.st_ino) != self._identity:
                identity_matched = False
                reason_code = reason_code or "runtime_lock_identity_mismatch"
            else:
                identity_matched = self._identity is not None or None
                try:
                    os.unlink(self._path)
                    lock_unlinked = True
                    lock_absent_confirmed = True
                except FileNotFoundError:
                    lock_unlinked = True
                    lock_absent_confirmed = True
                except PermissionError:
                    reason_code = reason_code or "runtime_lock_release_uncertain"
                except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
                    fatal = fatal or exc
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EIO):
                        reason_code = reason_code or "runtime_lock_release_uncertain"
                    else:
                        reason_code = reason_code or "runtime_lock_release_failed"

        if not lock_absent_confirmed and reason_code is None and fatal is None:
            reason_code = "runtime_lock_release_failed"
        return RuntimeLockReleaseResult(
            fd_closed=fd_closed,
            lock_unlinked=lock_unlinked,
            lock_absent_confirmed=lock_absent_confirmed,
            identity_matched=identity_matched,
            reason_code=reason_code,
            fatal=fatal,
        )


class _CriticalStop(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


SOURCE_EXHAUSTED_AFTER_RECONNECTS = "source_exhausted_after_reconnects"


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
            if tr_id == TR_TRADE:
                self._counters.inc("trade_subscription_acks")
            elif tr_id == TR_QUOTE:
                self._counters.inc("quote_subscription_acks")
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
    except BaseException as operation_exc:
        cleanup = _close_partial_resources(opened)
        if isinstance(operation_exc, (MemoryError, KeyboardInterrupt, SystemExit)):
            raise operation_exc
        if cleanup.fatal is not None:
            raise cleanup.fatal
        raise operation_exc
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


def _close_partial_resources(resources: list[object]) -> PartialCleanupResult:
    """역순 close를 시도하고, operation fatal과 cleanup fatal 우선순위를 보존한다."""
    ordinary_failures = 0
    pending_fatal: BaseException | None = None
    for resource in reversed(resources):
        close = getattr(resource, "close", None)
        if callable(close):
            try:
                close()
            except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
                if pending_fatal is None:
                    pending_fatal = exc
            except Exception:
                ordinary_failures += 1
    return PartialCleanupResult(ordinary_failures=ordinary_failures, fatal=pending_fatal)


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
    last_source_error_subcode: str | None = None
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
        except (MemoryError, KeyboardInterrupt, SystemExit):
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
        except (MemoryError, KeyboardInterrupt, SystemExit):
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
            if config.source_kind == "kis_live":
                session_now = now_fn()
                session = _live_pilot_session(config, now=session_now)
                record(
                    session_now,
                    "preflight",
                    "session_window_check",
                    reason=None if session.state is MarketSessionState.OPEN else "invalid_session_window",
                    snapshot={
                        "session_state": session.state.value,
                        "required_session_state": MarketSessionState.OPEN.value,
                        "calendar_reason": (
                            session.calendar_reason.value
                            if session.calendar_reason is not None
                            else None
                        ),
                    },
                )
                if session.state is not MarketSessionState.OPEN:
                    raise AttendedPaperDayRuntimeError("preflight", "invalid_session_window")

            publisher = DeterministicPaperDecisionPublisher(
                store=stack.active_store,
                session_date=config.session_date,
                symbol=config.symbol,
                evidence=lambda at, slot, status: _record_publication(
                    record, counters, at, slot, status
                ),
            )

            def on_monitor_evidence(evidence: MonitorEvidence) -> None:
                nonlocal last_source_error_subcode
                if evidence.reason_code == "source_error" and evidence.reason_subcode:
                    last_source_error_subcode = evidence.reason_subcode
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
                    session=_live_pilot_session(config, now=update.applied_at),
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
                elif isinstance(exc, MonitorExhaustedError):
                    stop_reason = SOURCE_EXHAUSTED_AFTER_RECONNECTS
                    outcome = RuntimeOutcome.FAIL
                    counters.reason(stop_reason)
                    snapshot: dict[str, object | None] = {
                        "reason_subcode": last_source_error_subcode,
                        "connection_attempts": exc.summary.connection_attempts,
                        "consecutive_failures": exc.summary.consecutive_failures,
                    }
                    with contextlib.suppress(Exception):
                        record(
                            now_fn(),
                            "runtime",
                            "failed_closed",
                            reason=stop_reason,
                            snapshot=snapshot,
                        )
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
        pending_fatal=body_fatal,
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


def is_clean_pass(result: Mapping[str, object]) -> bool:
    """Single owner of the clean exit-0 predicate (CLI and runtime share it).

    Exit 0 requires every clause: mechanical PASS, summary WRITTEN, lock fd closed,
    lock absence confirmed, no lock-release reason, and a CLEAN cleanup outcome.
    """
    return (
        result.get("outcome") == RuntimeOutcome.PASS
        and result.get("summary_publication_outcome") == SummaryPublicationOutcome.WRITTEN
        and result.get("runtime_lock_fd_closed") is True
        and result.get("runtime_lock_absent_confirmed") is True
        and result.get("runtime_lock_release_reason_code") is None
        and result.get("cleanup_outcome") == CleanupOutcome.CLEAN
    )


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
    pending_fatal: BaseException | None = None,
) -> tuple[dict[str, object], BaseException | None]:
    """단일 outer lifecycle owner: cleanup 판정 → summary publish → lock release.

    cleanup fatal이 있으면 PASS summary publish를 하지 않는다(Choice A).
    persisted/returned byte equality는 ``WRITTEN``에서만 주장한다.
    """
    nonterminal_journal: int | None = None
    cleanup_fatal: BaseException | None = None
    # Any ordinary (non-fatal) cleanup failure — stack/recorder/lock — marks the
    # cleanup INCOMPLETE and forbids a clean PASS/exit 0.
    ordinary_cleanup_failure = False

    # 1. Stack reverse-close — 모든 리소스 close 시도, 첫 fatal 보존.
    if stack is not None:
        with contextlib.suppress(Exception):
            nonterminal_journal = len(stack.journal.list_nonterminal())
        try:
            failures, stack_fatal = stack.close()
        except BaseException as exc:  # noqa: BLE001
            cleanup_fatal = exc
            failures = 0
        else:
            if stack_fatal is not None:
                cleanup_fatal = stack_fatal
        if failures:
            counters.inc("resource_close_failures", failures)
            stop_reason = "resource_close_failure"
            outcome = RuntimeOutcome.NO_GO
            ordinary_cleanup_failure = True
    # Stamp the actual resource-close completion here (exactly the point at which the
    # stack has been closed), not a "shutdown_completed" claim recorded before the
    # summary publish and lock release have run. The name now matches the observation.
    counters.stamp("resource_close_completed_at", now_fn())

    # 2. Completion verdict — cleanup fatal 전 mechanical outcome만 정제.
    if ran_market_loop and outcome == RuntimeOutcome.PASS and cleanup_fatal is None:
        outcome, stop_reason = _completion_verdict(counters, nonterminal_journal)

    # 3. Final evidence record + 4. recorder close.
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
            ordinary_cleanup_failure = True
    try:
        recorder.close()
    except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
        cleanup_fatal = cleanup_fatal or exc
    except Exception:
        ordinary_cleanup_failure = True
        if outcome != RuntimeOutcome.FAIL:
            outcome = RuntimeOutcome.FAIL
            stop_reason = "evidence_failed"

    # 5. Mechanical summary — cleanup fatal 판정 후 publish 여부 결정.
    summary = _build_summary(
        config=config,
        run_id=run_id,
        counters=counters,
        nonterminal_journal=nonterminal_journal,
        stop_reason=stop_reason,
        outcome=outcome,
    )
    publication_outcome: str | None = None
    publication_reason_codes: tuple[str, ...] = ()
    # The exact object serialized to the summary file, set only when the publish
    # outcome is WRITTEN. This is the persisted mechanical observation; it is
    # distinct from the final operator envelope returned to the caller.
    persisted_summary: dict[str, object] | None = None

    if summary_path_owned:
        publish_blocked = cleanup_fatal is not None or pending_fatal is not None
        if not publish_blocked:
            # Independent publication lifecycle boundary: the publisher returns a
            # structured SummaryPublishResult (outcome + optional fatal). Fatal
            # propagation does not erase confirmed publication state. Control always
            # falls through to lock release — no publisher exception may skip it.
            persisted_candidate = summary
            publish = _publish_summary_create_new(
                config.summary_out,
                json.dumps(persisted_candidate, sort_keys=True, indent=2),
            )
            publication_outcome = publish.outcome
            publication_reason_codes = publish.reason_codes
            if publish.fatal is not None:
                cleanup_fatal = cleanup_fatal or publish.fatal
            if publish.outcome == SummaryPublicationOutcome.WRITTEN:
                persisted_summary = persisted_candidate
            elif publish.outcome == SummaryPublicationOutcome.PUBLISHED_INCOMPLETE:
                summary = _build_summary(
                    config=config,
                    run_id=run_id,
                    counters=counters,
                    nonterminal_journal=nonterminal_journal,
                    stop_reason="summary_published_incomplete",
                    outcome=RuntimeOutcome.FAIL,
                )
            elif publish.outcome == SummaryPublicationOutcome.PUBLICATION_UNCERTAIN:
                summary = _build_summary(
                    config=config,
                    run_id=run_id,
                    counters=counters,
                    nonterminal_journal=nonterminal_journal,
                    stop_reason="summary_publication_uncertain",
                    outcome=RuntimeOutcome.FAIL,
                )
            else:
                summary = _build_summary(
                    config=config,
                    run_id=run_id,
                    counters=counters,
                    nonterminal_journal=nonterminal_journal,
                    stop_reason="summary_failed",
                    outcome=RuntimeOutcome.FAIL,
                )
        else:
            # Choice A: operation/cleanup fatal — summary 파일 미작성.
            publication_outcome = SummaryPublicationOutcome.NOT_WRITTEN
            publication_reason_codes = (
                ("cleanup_fatal",) if cleanup_fatal is not None else ("operation_fatal",)
            )

    # 6. Runtime lock release — 마지막 bounded cleanup. Always reached.
    lock_result = lock.release()
    if lock_result.fatal is not None:
        cleanup_fatal = cleanup_fatal or lock_result.fatal
    if lock_result.reason_code is not None or not lock_result.fd_closed:
        ordinary_cleanup_failure = True

    cleanup_outcome = (
        CleanupOutcome.FATAL
        if cleanup_fatal is not None or pending_fatal is not None
        else CleanupOutcome.INCOMPLETE
        if ordinary_cleanup_failure
        else CleanupOutcome.CLEAN
    )

    # 6b. Final verdict — a mechanical PASS survives only if the whole clean-exit
    #     predicate holds (publication WRITTEN, fd closed, lock absent confirmed,
    #     no release reason, cleanup CLEAN). The persisted file may still read PASS.
    if summary.get("outcome") == RuntimeOutcome.PASS:
        block_reason: str | None = None
        if publication_outcome != SummaryPublicationOutcome.WRITTEN:
            block_reason = "summary_not_written"
        elif not lock_result.fd_closed:
            block_reason = lock_result.reason_code or "runtime_lock_release_failed"
        elif not lock_result.lock_absent_confirmed:
            block_reason = lock_result.reason_code or "runtime_lock_release_failed"
        elif lock_result.reason_code is not None:
            block_reason = lock_result.reason_code
        elif cleanup_outcome != CleanupOutcome.CLEAN:
            block_reason = stop_reason if stop_reason != "completed" else "resource_close_failure"
        if block_reason is not None:
            summary = _build_summary(
                config=config,
                run_id=run_id,
                counters=counters,
                nonterminal_journal=nonterminal_journal,
                stop_reason=block_reason,
                outcome=RuntimeOutcome.FAIL,
            )

    result = {
        **summary,
        "persisted_summary": persisted_summary,
        "summary_publication_outcome": publication_outcome,
        "summary_publication_reason_codes": list(publication_reason_codes),
        "runtime_lock_fd_closed": lock_result.fd_closed,
        "runtime_lock_unlinked": lock_result.lock_unlinked,
        "runtime_lock_absent_confirmed": lock_result.lock_absent_confirmed,
        "runtime_lock_identity_matched": lock_result.identity_matched,
        "runtime_lock_release_reason_code": lock_result.reason_code,
        "cleanup_outcome": cleanup_outcome,
    }
    return result, cleanup_fatal


# Summary publication reason codes (stable, no raw errno/path).
_REASON_TEMP_CLOSE_FAILED = "summary_temp_close_failed"
_REASON_TEMP_CLEANUP_FAILED = "summary_temp_cleanup_failed"
_REASON_SYNC_FAILED = "summary_parent_sync_failed"
_REASON_PUBLISH_FAILED = "summary_publish_failed"
_REASON_WRITE_FAILED = "summary_write_failed"
_SUMMARY_TEMP_PREFIX = ".paper_day_summary."


def _summary_open_flags() -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


@dataclass(frozen=True)
class DirectorySyncResult:
    opened: bool
    synced: bool
    closed: bool
    reason_code: str | None = None
    fatal: BaseException | None = None


def _fsync_directory(directory: Path) -> DirectorySyncResult:
    """parent dir fsync을 structured result로 변환한다.

    open/fsync/close의 어떤 ``OSError``도 호출자로 escape하지 않는다(이전 구현은
    ``os.open``/``finally``의 ``os.close``가 lock release를 우회시켰다). fatal은
    raise하지 않고 결과에 담는다.
    """
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
        return DirectorySyncResult(
            opened=False, synced=False, closed=True, reason_code=_REASON_SYNC_FAILED, fatal=exc
        )
    except OSError:
        return DirectorySyncResult(
            opened=False, synced=False, closed=True, reason_code=_REASON_SYNC_FAILED
        )
    synced = False
    closed = False
    reason: str | None = None
    fatal: BaseException | None = None
    try:
        os.fsync(dir_fd)
        synced = True
    except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
        fatal = exc
    except OSError:
        reason = _REASON_SYNC_FAILED
    try:
        os.close(dir_fd)
        closed = True
    except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
        fatal = fatal or exc
    except OSError:
        reason = reason or _REASON_SYNC_FAILED
    if not (synced and closed) and reason is None and fatal is None:
        reason = _REASON_SYNC_FAILED
    return DirectorySyncResult(
        opened=True, synced=synced, closed=closed, reason_code=reason, fatal=fatal
    )



def _publish_summary_create_new(path: Path, text: str) -> SummaryPublishResult:
    """Atomic create-new summary publish; RTM-7c.4x artifact-file 패턴 재사용."""
    payload = text.encode("utf-8")
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
        return SummaryPublishResult(
            outcome=SummaryPublicationOutcome.NOT_WRITTEN,
            reason_codes=(_REASON_WRITE_FAILED,),
            fatal=exc,
        )
    except Exception:
        return SummaryPublishResult(
            outcome=SummaryPublicationOutcome.NOT_WRITTEN,
            reason_codes=(_REASON_WRITE_FAILED,),
        )
    temp_path = parent / f"{_SUMMARY_TEMP_PREFIX}{secrets.token_hex(16)}"
    fd: int | None = None
    temp_created = False
    destination_published = False
    publication_uncertain = False
    temp_close_complete = False
    temp_cleanup_complete = False
    parent_sync_confirmed = False
    parent_sync_attempted = False
    temp_stat: os.stat_result | None = None
    primary_reasons: list[str] = []
    # Fatal ownership inside the publisher: an operation (body) fatal outranks any
    # cleanup (temp close/unlink) fatal. Neither is allowed to collapse an actually
    # published destination into NOT_WRITTEN — the structured _finalize() result owns
    # the publication state.
    operation_fatal: BaseException | None = None
    cleanup_fatal: BaseException | None = None

    def _finalize(*, selected_fatal: BaseException | None) -> SummaryPublishResult:
        if destination_published:
            reason_codes = _build_publish_reason_codes(
                primary_reasons=primary_reasons,
                temp_close_complete=temp_close_complete,
                temp_cleanup_complete=temp_cleanup_complete,
                parent_sync_attempted=parent_sync_attempted,
                parent_sync_confirmed=parent_sync_confirmed,
                destination_published=True,
                publication_uncertain=False,
            )
            if (
                temp_close_complete
                and temp_cleanup_complete
                and parent_sync_confirmed
                and not primary_reasons
            ):
                return SummaryPublishResult(
                    outcome=SummaryPublicationOutcome.WRITTEN,
                    reason_codes=(),
                    fatal=selected_fatal,
                )
            return SummaryPublishResult(
                outcome=SummaryPublicationOutcome.PUBLISHED_INCOMPLETE,
                reason_codes=reason_codes,
                fatal=selected_fatal,
            )
        if publication_uncertain:
            return SummaryPublishResult(
                outcome=SummaryPublicationOutcome.PUBLICATION_UNCERTAIN,
                reason_codes=_build_publish_reason_codes(
                    primary_reasons=primary_reasons,
                    temp_close_complete=temp_close_complete,
                    temp_cleanup_complete=temp_cleanup_complete,
                    parent_sync_attempted=parent_sync_attempted,
                    parent_sync_confirmed=parent_sync_confirmed,
                    destination_published=False,
                    publication_uncertain=True,
                ),
                fatal=selected_fatal,
            )
        return SummaryPublishResult(
            outcome=SummaryPublicationOutcome.NOT_WRITTEN,
            reason_codes=_build_publish_reason_codes(
                primary_reasons=primary_reasons or [_REASON_WRITE_FAILED],
                temp_close_complete=temp_close_complete,
                temp_cleanup_complete=temp_cleanup_complete,
                parent_sync_attempted=parent_sync_attempted,
                parent_sync_confirmed=parent_sync_confirmed,
                destination_published=False,
                publication_uncertain=False,
            ),
            fatal=selected_fatal,
        )

    try:
        try:
            fd = os.open(str(temp_path), _summary_open_flags(), 0o600)
        except OSError:
            primary_reasons.append(_REASON_WRITE_FAILED)
        else:
            temp_created = True
            view = memoryview(payload)
            offset = 0
            while offset < len(view) and not primary_reasons:
                try:
                    written = os.write(fd, view[offset:])
                except OSError:
                    primary_reasons.append(_REASON_WRITE_FAILED)
                    break
                if written <= 0:
                    primary_reasons.append(_REASON_WRITE_FAILED)
                    break
                offset += written
            if not primary_reasons:
                try:
                    os.fsync(fd)
                    temp_stat = os.fstat(fd)
                except OSError:
                    primary_reasons.append(_REASON_WRITE_FAILED)
            if not primary_reasons:
                try:
                    os.link(temp_path, path)
                    destination_published = True
                except FileExistsError:
                    primary_reasons.append(_REASON_WRITE_FAILED)
                except OSError:
                    try:
                        dest_stat = os.lstat(path)
                    except FileNotFoundError:
                        primary_reasons.append(_REASON_WRITE_FAILED)
                    except OSError:
                        publication_uncertain = True
                        primary_reasons.append(_REASON_PUBLISH_FAILED)
                    else:
                        if (
                            stat.S_ISREG(dest_stat.st_mode)
                            and temp_stat is not None
                            and dest_stat.st_dev == temp_stat.st_dev
                            and dest_stat.st_ino == temp_stat.st_ino
                        ):
                            destination_published = True
                        else:
                            publication_uncertain = True
                            primary_reasons.append(_REASON_PUBLISH_FAILED)
            if destination_published and not primary_reasons and not publication_uncertain:
                try:
                    dest_stat = os.lstat(path)
                except OSError:
                    publication_uncertain = True
                    destination_published = False
                    primary_reasons.append(_REASON_PUBLISH_FAILED)
                else:
                    if (
                        not stat.S_ISREG(dest_stat.st_mode)
                        or temp_stat is None
                        or dest_stat.st_dev != temp_stat.st_dev
                        or dest_stat.st_ino != temp_stat.st_ino
                    ):
                        publication_uncertain = True
                        destination_published = False
                        primary_reasons.append(_REASON_PUBLISH_FAILED)
            if destination_published and not primary_reasons and not publication_uncertain:
                parent_sync_attempted = True
                sync = _fsync_directory(parent)
                if sync.fatal is not None:
                    # Capture as the operation fatal; the temp close/unlink finally
                    # still runs, then operation>cleanup precedence is applied below.
                    operation_fatal = sync.fatal
                else:
                    parent_sync_confirmed = sync.synced and sync.closed
                    if not parent_sync_confirmed:
                        primary_reasons.append(_REASON_SYNC_FAILED)
    except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
        operation_fatal = operation_fatal or exc
    except Exception:
        # An ordinary (non-OSError) exception escaped an inner step. It must never
        # collapse a published destination into NOT_WRITTEN: keep whatever state was
        # already established and let _finalize() recover it (PUBLISHED_INCOMPLETE if
        # the link landed, PUBLICATION_UNCERTAIN otherwise).
        if not destination_published:
            publication_uncertain = True
            if _REASON_PUBLISH_FAILED not in primary_reasons:
                primary_reasons.append(_REASON_PUBLISH_FAILED)
    finally:
        if fd is not None:
            try:
                os.close(fd)
                temp_close_complete = True
            except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
                cleanup_fatal = cleanup_fatal or exc
            except Exception:
                temp_close_complete = False
        if temp_created:
            try:
                os.unlink(temp_path)
                temp_cleanup_complete = True
            except FileNotFoundError:
                temp_cleanup_complete = True
            except (MemoryError, KeyboardInterrupt, SystemExit) as exc:
                cleanup_fatal = cleanup_fatal or exc
            except Exception:
                temp_cleanup_complete = False
    # Operation fatal outranks cleanup fatal. Neither may collapse a confirmed
    # publication state — the structured result carries both outcome and fatal for
    # the outer finalizer (lock release still runs before re-raise).
    selected_fatal = operation_fatal if operation_fatal is not None else cleanup_fatal
    return _finalize(selected_fatal=selected_fatal)


def _build_publish_reason_codes(
    *,
    primary_reasons: list[str],
    temp_close_complete: bool,
    temp_cleanup_complete: bool,
    parent_sync_attempted: bool,
    parent_sync_confirmed: bool,
    destination_published: bool,
    publication_uncertain: bool,
) -> tuple[str, ...]:
    reasons: list[str] = list(primary_reasons)
    if not temp_close_complete:
        reasons.append(_REASON_TEMP_CLOSE_FAILED)
    if not temp_cleanup_complete:
        reasons.append(_REASON_TEMP_CLEANUP_FAILED)
    if (
        (destination_published or publication_uncertain)
        and parent_sync_attempted
        and not parent_sync_confirmed
    ):
        reasons.append(_REASON_SYNC_FAILED)
    seen: set[str] = set()
    deduped: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            deduped.append(reason)
    return tuple(deduped)


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
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except LiveSourceConfigGateError as exc:
        raise AttendedPaperDayRuntimeError("source", "source_config_gate_failed") from exc
    except LiveSourceApprovalError as exc:
        raise AttendedPaperDayRuntimeError("source", "source_approval_failed") from exc
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

        if consumer.done() and not consumer.cancelled():
            consumer_exc = consumer.exception()
            if consumer_exc is not None:
                if isinstance(consumer_exc, (MemoryError, KeyboardInterrupt, SystemExit)):
                    raise consumer_exc
                if isinstance(consumer_exc, LiveSourceConnectError):
                    raise AttendedPaperDayRuntimeError(
                        "source", "source_connect_failed"
                    ) from consumer_exc
                raise AttendedPaperDayRuntimeError("source", "source_failed") from consumer_exc
        else:
            consumer.cancel()
            # Bound the *probe await* on cancellation/cleanup so a source that delays
            # its response to cancellation cannot stall the probe past the timeout.
            # ``shield`` keeps the timeout from re-cancelling (and thus re-blocking on)
            # the same task. NOTE: this bounds only the await — a source that ignores
            # every CancelledError (including the one asyncio.run delivers at loop
            # shutdown) cannot be terminated in-process; bounding that pathological
            # case requires process isolation. The supported contract is therefore
            # "cancellation-compliant sources are bounded".
            try:
                await asyncio.wait_for(
                    asyncio.shield(consumer), timeout=PROBE_CLEANUP_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError as exc:
                raise AttendedPaperDayRuntimeError(
                    "source", "source_close_timeout"
                ) from exc
            except (MemoryError, KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                pass
            # Python 3.11+: ``Task.exception()`` on a cancelled task re-raises
            # ``CancelledError`` into the caller; check ``cancelled()`` first.
            if consumer.done() and not consumer.cancelled():
                cancel_exc = consumer.exception()
                if cancel_exc is not None:
                    if isinstance(cancel_exc, (MemoryError, KeyboardInterrupt, SystemExit)):
                        raise cancel_exc
                    raise AttendedPaperDayRuntimeError(
                        "source", "source_close_failed"
                    ) from cancel_exc

        if lifecycle.rejected:
            raise AttendedPaperDayRuntimeError("transport", "subscription_rejected")
        if lifecycle.all_subscribed:
            return
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


def _diagnostic_session_provider(day: date) -> ExplicitMarketScheduleProvider:
    return ExplicitMarketScheduleProvider(
        timezone=KST,
        schedule={
            day: SessionWindow(
                pre_open=time(8, 30),
                open=time(9, 0),
                close=time(15, 30),
                post_close_end=time(16, 0),
            )
        },
    )


def _live_pilot_session(config: AttendedPaperDayConfig, *, now: datetime):
    return _diagnostic_session_provider(config.session_date).session_at(PILOT_MARKET, now)


def _outcome_for_stop_reason(stop_reason: str) -> str:
    if stop_reason in {
        "health_not_ready",
        "transport_not_ready",
        "invalid_session_window",
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
    snapshot: dict[str, object | None] = {
        "event_type": evidence.event_type,
        "apply_status": evidence.apply_status,
        "sequence": evidence.sequence,
        "reason_subcode": evidence.reason_subcode,
    }
    if evidence.parser_metadata is not None:
        snapshot["parser_metadata"] = evidence.parser_metadata
    record(
        evidence.timestamp,
        "market_data",
        evidence.kind,
        reason=evidence.reason_code,
        snapshot=snapshot,
    )


def _heartbeat_snapshot(
    stack: DiagnosticStack | None, counters: DiagnosticCounters, at: datetime
) -> dict[str, object]:
    nonterminal = len(stack.journal.list_nonterminal()) if stack is not None else 0
    latest = stack.latest.peek(PILOT_MARKET, PILOT_SYMBOL, now=at) if stack is not None else None
    session = _diagnostic_session_provider(at.astimezone(KST).date()).session_at(PILOT_MARKET, at)
    verdict = stack.tracker.evaluate(session=session, now=at) if stack is not None else None
    active = stack.active_store.read_active(PILOT_MARKET, PILOT_SYMBOL) if stack is not None else None
    return {
        "connected": counters.values.get("connected", 0) > 0,
        "subscriptions_ready": counters.values.get("subscription_acks", 0) >= 2,
        "trade_subscription_ready": counters.values.get("trade_subscription_acks", 0) > 0,
        "quote_subscription_ready": counters.values.get("quote_subscription_acks", 0) > 0,
        "quote_frames": counters.values.get("quote_frames", 0),
        "normalized_quotes": counters.values.get("normalized_quotes", 0),
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
    "CleanupOutcome",
    "DirectorySyncResult",
    "DiagnosticCounters",
    "DeterministicPaperDecisionPublisher",
    "EvidenceRecorder",
    "LiveSourceApprovalError",
    "LiveSourceConfigGateError",
    "LiveSourceConnectError",
    "PartialCleanupResult",
    "RuntimeLockAcquireCleanupResult",
    "RuntimeLockReleaseResult",
    "SummaryPublicationOutcome",
    "SummaryPublishResult",
    "build_diagnostic_stack",
    "is_clean_pass",
    "journal_state_counts",
    "run_attended_paper_day",
    "validate_attended_paper_day_inputs",
]
