"""Offline paper fast-loop composition root (RTM-7c.4a).

Three operator capabilities, all offline:

* ``build_paper_fast_loop_plan`` — validate-only. Loads + validates the on-disk
  execution-inputs snapshot and runs single-symbol (KR / 6-digit / PAPER / KRW)
  preflight against any existing ledger positions. No execution, no DB writes,
  no network, no runtime directory creation.
* ``inspect_paper_fast_loop`` — read-only inspection of the configured ledger /
  journal / active-decision-store via ``composition.sqlite_inspector``.
* ``replay_offline`` — deterministic offline replay of the fast-loop execution
  stack against normalized-event fixtures, using caller-provided temp paths
  (never the configured ``runtime/`` paths). No KIS frames, no network.

This module is the *only* place allowed to wire broker / ledger / coordinator
together; ``src/orchestration`` purity is preserved. It reads no credentials and
never starts a live runtime. See ``docs/PAPER_FAST_LOOP_COMPOSITION_CONTRACT.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

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
from domain.enums import AccountRole, Currency, Market
from domain.position import CashSnapshot
from domain.validation import ValidationResult

from execution.paper_execution_coordinator import PaperExecutionCoordinator
from execution.paper_portfolio_context import PaperPortfolioContextService, PaperPortfolioPolicy
from execution.sqlite_trigger_journal import SqliteTriggerJournal
from execution.trigger_order_bridge import TriggerOrderBridge

from ledger.sqlite_ledger import SQLiteLedger

from market_data.conditions import Comparator, ConditionClause, Metric
from market_data.health_policy import HealthThresholds, MarketHealthTracker
from market_data.latest_state import LatestMarketStateStore
from market_data.market_session import SessionWindow, build_explicit_schedule
from market_data.models import (
    MarketEventType,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)
from market_data.monitor import AppliedMarketUpdate
from market_data.rolling_window import RollingRetentionPolicy, RollingTradeHistoryStore
from market_data.trigger_engine import TriggerPlan, TriggerEngine

from paper_loop import QuantityResolver
from risk import OrderIntentGenerator
from risk.models import RiskMode

from config.settings import RuntimePaperFastLoopSettings
from composition import sqlite_inspector
from orchestration.active_decision_store import ActiveDecisionStore, DecisionPublicationCandidate
from orchestration.execution_gate import SessionHealthExecutionGate
from orchestration.execution_inputs_snapshot import (
    ExecutionInputsSnapshotError,
    ValidatedExecutionInputsProvider,
    ValidatedExecutionInputsSnapshot,
    compute_snapshot_payload_hash,
    load_execution_inputs_snapshot,
)
from orchestration.fast_loop_execution import (
    FastLoopExecutionOrchestrator,
    FastLoopExecutionStatus,
    StaticExecutionInputsProvider,
)

__all__ = [
    "PaperFastLoopPaths",
    "PaperFastLoopOutcome",
    "InspectionOutcome",
    "MachineCheckOutcome",
    "PaperFastLoopPlan",
    "PaperFastLoopInspection",
    "ExecutionInputsInspection",
    "ActiveDecisionInspection",
    "RuntimePrecheckResult",
    "OfflineReplayResult",
    "PaperFastLoopStack",
    "build_offline_paper_fast_loop_stack",
    "AVAILABLE_REPLAY_FIXTURES",
    "build_paper_fast_loop_plan",
    "inspect_paper_fast_loop",
    "precheck_runtime",
    "replay_offline",
]


class PaperFastLoopOutcome(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"


class InspectionOutcome(StrEnum):
    OK = "ok"
    NO_GO = "no_go"


class MachineCheckOutcome(StrEnum):
    """Mechanically-verifiable precheck verdict. NEVER a runtime-activation authorization."""

    PASS = "pass"
    NO_GO = "no_go"


@dataclass(frozen=True)
class PaperFastLoopPaths:
    """Resolved on-disk paths for the configured fast-loop runtime.

    Path resolution is pure: it joins the validated relative paths under ``base_dir``
    and never touches the filesystem.
    """

    snapshot_path: Path
    active_decision_store_path: Path
    ledger_path: Path
    trigger_journal_path: Path

    @classmethod
    def from_settings(
        cls, settings: RuntimePaperFastLoopSettings, *, base_dir: Path | str = Path(".")
    ) -> "PaperFastLoopPaths":
        base = Path(base_dir)
        return cls(
            snapshot_path=base / settings.snapshot_path,
            active_decision_store_path=base / settings.active_decision_store_path,
            ledger_path=base / settings.ledger_path,
            trigger_journal_path=base / settings.trigger_journal_path,
        )


@dataclass(frozen=True)
class PaperFastLoopPlan:
    """Validate-only outcome. ``reasons`` carries typed, sanitized reason codes."""

    outcome: PaperFastLoopOutcome
    market: str
    symbol: str
    snapshot_source_id: str | None
    snapshot_universe: str | None
    snapshot_expires_at: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionInputsInspection:
    """Sanitized readiness view of the on-disk execution-inputs snapshot.

    ``hash_valid`` is true only when the snapshot loaded (the loader fail-closes on a
    hash mismatch). ``currently_valid`` is true when ``created_at <= now <= expires_at``.
    No raw snapshot JSON is carried — only non-secret identifiers and parsed timestamps.
    """

    present: bool
    source_id: str | None
    universe: str | None
    created_at: str | None
    expires_at: str | None
    hash_valid: bool
    currently_valid: bool


@dataclass(frozen=True)
class ActiveDecisionInspection:
    """Sanitized readiness view of the configured (market, symbol) active decision.

    ``integrity_valid`` mirrors the publish-time invariants (hash / publication-id /
    validity columns / internal identity) reconciled read-only. No raw bundle JSON is
    carried — only non-secret identifiers, the action label, and parsed timestamps.
    """

    present: bool
    integrity_valid: bool
    decision_id: str | None
    plan_id: str | None
    market: str | None
    symbol: str | None
    universe: str | None
    action: str | None
    valid_from: str | None
    expires_at: str | None
    has_plan: bool
    currently_valid: bool


@dataclass(frozen=True)
class PaperFastLoopInspection:
    """Read-only inspection of the configured fast-loop databases.

    ``outcome`` is ``NO_GO`` whenever any database is missing, not quiescent (a live
    WAL/shm/journal sidecar is present), has an invalid schema, carries a dangling active
    pointer, holds a non-terminal (in-flight/crashed) journal entry, fails a single-symbol
    position preflight, or the execution-inputs snapshot / active decision is missing,
    corrupt, out of its validity window, or universe-mismatched. ``reasons`` carries the
    typed, sanitized codes behind that verdict.
    """

    outcome: InspectionOutcome
    market: str
    symbol: str
    ledger: sqlite_inspector.LedgerSummary | None
    journal: sqlite_inspector.JournalSummary | None
    active_store: sqlite_inspector.ActiveStoreSummary | None
    execution_inputs: ExecutionInputsInspection | None
    active_decision: ActiveDecisionInspection | None
    missing_databases: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RuntimePrecheckResult:
    """Attended bounded fast-loop runtime precheck verdict (read-only; runs no runtime).

    ``machine_outcome`` is the *mechanical* verdict: ``PASS`` only when the reused
    ``inspect_paper_fast_loop`` is OK AND every artifact fingerprint is byte-identical
    before and after the inspection (proving the precheck mutated nothing). It is **never**
    an authorization to activate the runtime — the activation fields below are constants that
    always hold:

    * ``activation_authorized`` is always ``False``;
    * ``runtime_activation_outcome`` is always ``"no_go"``;
    * ``explicit_operator_approval_required`` is always ``True``;
    * ``writers_stopped_manual_confirmation_required`` is always ``True`` — sidecar absence
      proves only a *momentary* quiescence, not that every writer process is stopped (no
      process scan / PID inspection / OS lock is in scope), so writer-stop stays a
      machine-unverified MANUAL requirement.

    ``reasons`` is the reused inspection's reasons followed by any precheck-specific reason
    (``precheck_artifact_changed:<artifact>`` / ``precheck_artifact_not_regular_file:<artifact>``).
    Missing artifacts are reported by the reused inspection layer (``missing_database:<db>`` /
    ``missing_execution_inputs_snapshot``); precheck does not re-report them (drift avoidance)."""

    machine_outcome: MachineCheckOutcome
    activation_authorized: bool
    runtime_activation_outcome: str
    explicit_operator_approval_required: bool
    writers_stopped_manual_confirmation_required: bool
    market: str
    symbol: str
    inspection: PaperFastLoopInspection
    fingerprints_before: tuple[sqlite_inspector.ArtifactFingerprint, ...]
    fingerprints_after: tuple[sqlite_inspector.ArtifactFingerprint, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class OfflineReplayResult:
    """Deterministic offline replay summary built from temp-dir databases.

    Replay runs three phases on the *same* on-disk databases to prove idempotency:
    a first event, a repeat event on the same orchestrator (within-arming duplicate),
    and a third event after rebuilding the whole stack (composition restart). The
    execution inputs are supplied by the real ``ValidatedExecutionInputsProvider`` loaded
    from a snapshot file, so a tampered/stale/universe-mismatch snapshot yields zero fills.
    """

    fixture: str
    market: str
    symbol: str
    snapshot_loaded: bool
    snapshot_reason: str | None
    event_count: int
    statuses: tuple[str, ...]
    first_status: str | None
    repeat_status: str | None
    restart_status: str | None
    committed_count: int
    order_result_count: int
    filled_result_count: int
    fill_count: int
    journal_state_counts: tuple[tuple[str, int], ...]
    journal_terminal_count: int
    final_position_quantity: str | None
    final_cash_amount: str | None


# --- single-symbol preflight ---

_SUPPORTED_MARKET = "KR"
_SUPPORTED_ACCOUNT_ROLE = AccountRole.PAPER.value
_SUPPORTED_CURRENCY = Currency.KRW.value


def _position_preflight_reasons(
    positions: Sequence[sqlite_inspector.PositionRow], *, symbol: str
) -> tuple[str, ...]:
    reasons: list[str] = []
    for row in positions:
        if row.market != _SUPPORTED_MARKET:
            reasons.append("unsupported_market")
        if row.account_role != _SUPPORTED_ACCOUNT_ROLE:
            reasons.append("unsupported_account_role")
        if row.currency != _SUPPORTED_CURRENCY:
            reasons.append("unsupported_currency")
        if row.symbol != symbol:
            reasons.append("foreign_position_present")
    # 안정적 순서로 중복 제거.
    seen: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.append(reason)
    return tuple(seen)


def build_paper_fast_loop_plan(
    *,
    settings: RuntimePaperFastLoopSettings,
    now: datetime,
    base_dir: Path | str = Path("."),
    snapshot_loader: Callable[[Path], ValidatedExecutionInputsSnapshot] = load_execution_inputs_snapshot,
) -> PaperFastLoopPlan:
    """Validate-only: load+validate the on-disk snapshot and check validity window.

    Pure config + snapshot only. Opens **no** database (no ledger/journal/active-store
    access); single-symbol position preflight against existing databases lives in
    ``inspect_paper_fast_loop`` so validate-only stays side-effect free.
    """

    paths = PaperFastLoopPaths.from_settings(settings, base_dir=base_dir)
    market = settings.market
    symbol = settings.symbol
    reasons: list[str] = []

    snapshot: ValidatedExecutionInputsSnapshot | None = None
    try:
        snapshot = snapshot_loader(paths.snapshot_path)
    except Exception as exc:  # ExecutionInputsSnapshotError 등 — sanitized reason만 surface.
        reason_code = getattr(exc, "reason_code", None)
        reasons.append(str(reason_code) if reason_code else "snapshot_load_failed")

    if snapshot is not None:
        if now < snapshot.created_at:
            reasons.append("snapshot_not_yet_valid")
        elif now > snapshot.expires_at:
            reasons.append("snapshot_expired")

    outcome = PaperFastLoopOutcome.READY if not reasons else PaperFastLoopOutcome.NOT_READY
    return PaperFastLoopPlan(
        outcome=outcome,
        market=market,
        symbol=symbol,
        snapshot_source_id=snapshot.source_id if snapshot else None,
        snapshot_universe=snapshot.universe if snapshot else None,
        snapshot_expires_at=snapshot.expires_at.isoformat() if snapshot else None,
        reasons=tuple(reasons),
    )


def inspect_paper_fast_loop(
    *, settings: RuntimePaperFastLoopSettings, now: datetime, base_dir: Path | str = Path(".")
) -> PaperFastLoopInspection:
    """Read-only, fail-closed startup-readiness inspection of the configured stack.

    Checks (all read-only; constructs no store, creates/migrates no schema, reconciles
    nothing) and the sanitized ``reasons`` they emit when ``NO_GO``:

    * execution-inputs snapshot — ``missing_execution_inputs_snapshot``,
      ``execution_inputs_hash_mismatch``, ``execution_inputs_universe_mismatch``,
      ``execution_inputs_invalid``, ``execution_inputs_not_yet_valid``,
      ``execution_inputs_expired``;
    * quiescence — ``database_not_quiescent:<db>`` (a live WAL/shm/journal sidecar);
    * databases — ``missing_database:<db>``, ``<db>_missing_table/column``,
      ``<db>_unreadable:<code>``, ``dangling_active_pointer``,
      ``nonterminal_journal_entries``;
    * single-symbol position preflight — ``unsupported_*`` / ``foreign_position_present``;
    * active decision — ``missing_active_decision``, ``active_pointer_identity_mismatch``,
      ``active_bundle_corrupt``, ``active_decision_not_yet_valid``,
      ``active_decision_expired``, ``active_execution_universe_mismatch``,
      ``active_plan_consistency_mismatch``.

    ``now`` must be timezone-aware (the CLI reads it once and passes it). All sqlite
    failures are surfaced as sanitized reason codes — never raw exception text.
    """

    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("inspect_paper_fast_loop requires a timezone-aware 'now'.")

    paths = PaperFastLoopPaths.from_settings(settings, base_dir=base_dir)
    market = settings.market
    symbol = settings.symbol
    missing: list[str] = []
    reasons: list[str] = []

    execution_inputs, snapshot_universe = _inspect_execution_inputs(
        paths.snapshot_path, now=now, reasons=reasons
    )
    ledger_summary = _inspect_ledger(paths.ledger_path, symbol=symbol, market=market,
                                     missing=missing, reasons=reasons)
    journal_summary = _inspect_journal(paths.trigger_journal_path, missing=missing, reasons=reasons)
    active_summary, active_decision = _inspect_active_store(
        paths.active_decision_store_path, symbol=symbol, market=market, now=now,
        snapshot_universe=snapshot_universe, missing=missing, reasons=reasons,
    )

    outcome = InspectionOutcome.OK if not reasons else InspectionOutcome.NO_GO
    return PaperFastLoopInspection(
        outcome=outcome,
        market=market,
        symbol=symbol,
        ledger=ledger_summary,
        journal=journal_summary,
        active_store=active_summary,
        execution_inputs=execution_inputs,
        active_decision=active_decision,
        missing_databases=tuple(missing),
        reasons=tuple(reasons),
    )


# Artifacts fingerprinted before/after the read-only precheck. ``is_sqlite`` controls
# whether ``user_version`` is parsed from the file header. The snapshot is JSON (not SQLite).
_PRECHECK_ARTIFACTS: tuple[tuple[str, str, bool], ...] = (
    ("execution_inputs_snapshot", "snapshot_path", False),
    ("ledger", "ledger_path", True),
    ("trigger_journal", "trigger_journal_path", True),
    ("active_decision_store", "active_decision_store_path", True),
)

# Inspection reasons a *present-but-irregular* artifact (directory/socket/fifo) produces,
# keyed by precheck artifact name. When the precheck ``not_regular_file`` reason fires for an
# artifact, these are dropped from the AGGREGATE reasons (kept in ``inspection.reasons``) so a
# single root cause yields a single canonical reason. Note the inspection reason prefixes differ
# from the artifact names (``trigger_journal`` → ``journal_unreadable``,
# ``active_decision_store`` → ``active_store_unreadable``). An irregular SQLite path fails
# ``open_read_only`` with ``sqlite_not_a_file``; an irregular JSON snapshot fails to load and the
# inspect layer maps the unexpected error to the generic ``execution_inputs_invalid``.
_IRREGULAR_ARTIFACT_OWNED_INSPECTION_REASONS: dict[str, tuple[str, ...]] = {
    "execution_inputs_snapshot": ("execution_inputs_invalid",),
    "ledger": ("ledger_unreadable:sqlite_not_a_file",),
    "trigger_journal": ("journal_unreadable:sqlite_not_a_file",),
    "active_decision_store": ("active_store_unreadable:sqlite_not_a_file",),
}


def _fingerprint_artifacts(paths: PaperFastLoopPaths) -> tuple[sqlite_inspector.ArtifactFingerprint, ...]:
    return tuple(
        sqlite_inspector.fingerprint_artifact(
            getattr(paths, attr), name=name, is_sqlite=is_sqlite
        )
        for name, attr, is_sqlite in _PRECHECK_ARTIFACTS
    )


def precheck_runtime(
    *, settings: RuntimePaperFastLoopSettings, now: datetime, base_dir: Path | str = Path(".")
) -> RuntimePrecheckResult:
    """Attended bounded fast-loop runtime precheck — read-only, runs no runtime.

    Fingerprints every configured artifact (execution-inputs snapshot + ledger / journal /
    active-decision SQLite DBs), reuses ``inspect_paper_fast_loop`` for the machine-check
    body (config + snapshot + DB readiness; no store construction, no schema create/migrate,
    no reconcile), then re-fingerprints. The mechanical ``machine_outcome`` is ``PASS`` only
    when the inspection is OK and no fingerprint changed; otherwise ``NO_GO``.

    This NEVER authorizes runtime activation: the result always carries
    ``activation_authorized=False`` / ``runtime_activation_outcome="no_go"`` and the manual
    requirements (explicit Operator approval; writer-stop confirmation). No network, no
    credential read, no broker call, no order, no operational DB write, no schema work, no
    process/thread/daemon, no runtime file creation. ``now`` must be timezone-aware."""

    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("precheck_runtime requires a timezone-aware 'now'.")

    paths = PaperFastLoopPaths.from_settings(settings, base_dir=base_dir)
    before = _fingerprint_artifacts(paths)
    inspection = inspect_paper_fast_loop(settings=settings, now=now, base_dir=base_dir)
    after = _fingerprint_artifacts(paths)

    precheck_reasons: list[str] = []
    irregular_names: set[str] = set()
    # A present-but-irregular artifact (directory/socket/fifo) cannot be trusted read-only.
    # Inspect's open_read_only catches this for the DBs it opens (``sqlite_not_a_file``), but
    # not for the JSON snapshot, so the fingerprint covers all four uniformly, fail-closed.
    for fb in before:
        if fb.present and not fb.is_regular_file:
            precheck_reasons.append(f"precheck_artifact_not_regular_file:{fb.name}")
            irregular_names.add(fb.name)
    # Read-only proof: every artifact must be byte-identical (and same sidecar set) across
    # the inspection. Any difference means the precheck mutated operator state → NO_GO.
    for fb, fa in zip(before, after):
        if fb != fa:
            precheck_reasons.append(f"precheck_artifact_changed:{fb.name}")

    # Single canonical reason per root cause: when an artifact is present-but-irregular, the
    # precheck ``not_regular_file`` reason OWNS that condition; drop the inspection layer's
    # generic unreadable/invalid reason for the SAME artifact from the AGGREGATE (it is kept
    # verbatim in ``inspection.reasons`` for diagnostics), so an irregular artifact surfaces
    # as exactly one reason — matching the missing/dangling/identity single-reason precedent.
    owned_to_drop: set[str] = set()
    for name in irregular_names:
        owned_to_drop.update(_IRREGULAR_ARTIFACT_OWNED_INSPECTION_REASONS.get(name, ()))
    aggregate_inspection_reasons = tuple(r for r in inspection.reasons if r not in owned_to_drop)

    reasons = aggregate_inspection_reasons + tuple(precheck_reasons)
    machine_no_go = inspection.outcome is InspectionOutcome.NO_GO or bool(precheck_reasons)
    machine_outcome = MachineCheckOutcome.NO_GO if machine_no_go else MachineCheckOutcome.PASS
    return RuntimePrecheckResult(
        machine_outcome=machine_outcome,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        explicit_operator_approval_required=True,
        writers_stopped_manual_confirmation_required=True,
        market=settings.market,
        symbol=settings.symbol,
        inspection=inspection,
        fingerprints_before=before,
        fingerprints_after=after,
        reasons=reasons,
    )


def _check_quiescent(path: Path, db_name: str, reasons: list[str]) -> None:
    """Flag a live SQLite sidecar (WAL/shm/journal) for an existing DB as non-quiescent."""

    if sqlite_inspector.sidecar_files(path):
        reasons.append(f"database_not_quiescent:{db_name}")


def _inspect_execution_inputs(
    snapshot_path: Path, *, now: datetime, reasons: list[str]
) -> tuple[ExecutionInputsInspection | None, str | None]:
    """Read-only execution-inputs snapshot readiness. Returns ``(inspection, universe)``.

    ``universe`` (the snapshot's declared universe) is returned even when the validity
    window fails, so the active-decision universe-match check can still run."""

    if not snapshot_path.exists():
        reasons.append("missing_execution_inputs_snapshot")
        return None, None
    try:
        snapshot = load_execution_inputs_snapshot(snapshot_path)
    except ExecutionInputsSnapshotError as exc:
        code = getattr(exc, "reason_code", None)
        if code == "snapshot_file_missing":
            reasons.append("missing_execution_inputs_snapshot")
        elif code == "snapshot_hash_mismatch":
            reasons.append("execution_inputs_hash_mismatch")
        elif code == "snapshot_universe_mismatch":
            reasons.append("execution_inputs_universe_mismatch")
        else:
            reasons.append("execution_inputs_invalid")
        return None, None

    currently_valid = True
    if now < snapshot.created_at:
        reasons.append("execution_inputs_not_yet_valid")
        currently_valid = False
    elif now > snapshot.expires_at:
        reasons.append("execution_inputs_expired")
        currently_valid = False
    inspection = ExecutionInputsInspection(
        present=True,
        source_id=snapshot.source_id,
        universe=snapshot.universe,
        created_at=snapshot.created_at.isoformat(),
        expires_at=snapshot.expires_at.isoformat(),
        hash_valid=True,
        currently_valid=currently_valid,
    )
    return inspection, snapshot.universe


def _inspect_ledger(
    path: Path, *, symbol: str, market: str, missing: list[str], reasons: list[str]
) -> sqlite_inspector.LedgerSummary | None:
    if not path.exists():
        missing.append("ledger")
        reasons.append("missing_database:ledger")
        return None
    _check_quiescent(path, "ledger", reasons)
    try:
        schema = sqlite_inspector.schema_issues(path, sqlite_inspector.LEDGER_REQUIRED_SCHEMA)
        if schema:
            reasons.extend(f"ledger_{code}" for code in schema)
            return None
        positions = sqlite_inspector.scan_positions(path)
        reasons.extend(_position_preflight_reasons(positions, symbol=symbol))
        return sqlite_inspector.summarize_ledger(path, symbol=symbol, market=market)
    except sqlite_inspector.SqliteInspectionError as exc:
        reasons.append(f"ledger_unreadable:{exc.reason_code}")
        return None


def _inspect_journal(
    path: Path, *, missing: list[str], reasons: list[str]
) -> sqlite_inspector.JournalSummary | None:
    if not path.exists():
        missing.append("trigger_journal")
        reasons.append("missing_database:trigger_journal")
        return None
    _check_quiescent(path, "trigger_journal", reasons)
    try:
        schema = sqlite_inspector.schema_issues(path, sqlite_inspector.JOURNAL_REQUIRED_SCHEMA)
        if schema:
            reasons.extend(f"journal_{code}" for code in schema)
            return None
        summary = sqlite_inspector.summarize_journal(path)
        if summary.nonterminal_count > 0:
            reasons.append("nonterminal_journal_entries")
        return summary
    except sqlite_inspector.SqliteInspectionError as exc:
        reasons.append(f"journal_unreadable:{exc.reason_code}")
        return None


def _inspect_active_store(
    path: Path, *, symbol: str, market: str, now: datetime, snapshot_universe: str | None,
    missing: list[str], reasons: list[str]
) -> tuple[sqlite_inspector.ActiveStoreSummary | None, ActiveDecisionInspection | None]:
    if not path.exists():
        missing.append("active_decision_store")
        reasons.append("missing_database:active_decision_store")
        return None, None
    _check_quiescent(path, "active_decision_store", reasons)
    try:
        schema = sqlite_inspector.schema_issues(path, sqlite_inspector.ACTIVE_STORE_REQUIRED_SCHEMA)
        if schema:
            reasons.extend(f"active_store_{code}" for code in schema)
            return None, None
        summary = sqlite_inspector.summarize_active_store(path, symbol=symbol, market=market)
        if summary.dangling_pointer_count > 0:
            reasons.append("dangling_active_pointer")
        inspection = _inspect_active_decision(
            path, symbol=symbol, market=market, now=now,
            snapshot_universe=snapshot_universe, reasons=reasons,
        )
        return summary, inspection
    except sqlite_inspector.SqliteInspectionError as exc:
        reasons.append(f"active_store_unreadable:{exc.reason_code}")
        return None, None


def _inspect_active_decision(
    path: Path, *, symbol: str, market: str, now: datetime, snapshot_universe: str | None,
    reasons: list[str]
) -> ActiveDecisionInspection | None:
    """Read-only active-decision readiness via ``sqlite_inspector.inspect_active_decision``.

    Never constructs ``ActiveDecisionStore`` (its ``__init__`` creates/migrates schema)."""

    integrity = sqlite_inspector.inspect_active_decision(path, symbol=symbol, market=market)
    if not integrity.present:
        reasons.append("missing_active_decision")
        return ActiveDecisionInspection(
            present=False, integrity_valid=False, decision_id=None, plan_id=None,
            market=None, symbol=None, universe=None, action=None, valid_from=None,
            expires_at=None, has_plan=False, currently_valid=False,
        )
    if not integrity.integrity_ok:
        if integrity.integrity_reason == "identity_mismatch":
            reasons.append("active_pointer_identity_mismatch")
        elif integrity.integrity_reason == "plan_consistency_mismatch":
            # 인식된 action인데 plan 유무가 어긋남(BUY/SELL without plan 또는 HOLD with plan).
            # 안정적·구별 가능한 운영 분류이므로 generic corrupt가 아닌 전용 reason으로 표면화한다.
            reasons.append("active_plan_consistency_mismatch")
        elif integrity.integrity_reason == "dangling":
            # 손상 종류가 "pointer는 있으나 가리키는 version 행이 없음"인 경우, 이미
            # ``_inspect_active_store``가 summary.dangling_pointer_count>0를 보고
            # ``dangling_active_pointer``를 한 번 기록한다(LEFT JOIN으로 같은 pointer를 셈).
            # 여기서 ``active_bundle_corrupt``를 더하면 단일 근본원인에 두 reason이 붙어
            # 안정-단일-reason 계약을 깨므로, dangling은 corrupt로 중복 보고하지 않는다.
            pass
        else:  # "corrupt": JSON/hash/publication_id/model/validity 손상
            reasons.append("active_bundle_corrupt")
        return ActiveDecisionInspection(
            present=True, integrity_valid=False, decision_id=integrity.decision_id,
            plan_id=integrity.plan_id, market=integrity.market, symbol=integrity.symbol,
            universe=integrity.universe, action=integrity.action,
            valid_from=integrity.valid_from, expires_at=integrity.expires_at,
            has_plan=integrity.has_plan, currently_valid=False,
        )

    currently_valid = True
    valid_from = _parse_optional_iso(integrity.valid_from)
    expires_at = _parse_optional_iso(integrity.expires_at)
    if valid_from is not None and now < valid_from:
        reasons.append("active_decision_not_yet_valid")
        currently_valid = False
    elif expires_at is not None and now > expires_at:
        reasons.append("active_decision_expired")
        currently_valid = False
    if snapshot_universe is not None and integrity.universe != snapshot_universe:
        reasons.append("active_execution_universe_mismatch")
    # plan-consistency(BUY/SELL↔plan 유무)는 integrity 단계에서 이미 fail-closed로 검증된다:
    # integrity_ok=True에 도달했다는 것은 모델 복원(DecisionTriggerBundle 검증)을 통과했다는
    # 뜻이므로 여기서는 항상 일치한다. 중복(dead) 재검사를 두지 않고, 어긋남은 integrity의
    # plan_consistency_mismatch → active_plan_consistency_mismatch 경로로만 보고한다.
    return ActiveDecisionInspection(
        present=True, integrity_valid=True, decision_id=integrity.decision_id,
        plan_id=integrity.plan_id, market=integrity.market, symbol=integrity.symbol,
        universe=integrity.universe, action=integrity.action,
        valid_from=integrity.valid_from, expires_at=integrity.expires_at,
        has_plan=integrity.has_plan, currently_valid=currently_valid,
    )


def _parse_optional_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:  # pragma: no cover - integrity_ok rows always carry parseable ISO
        return None


# --- deterministic offline replay ---

_KST = ZoneInfo("Asia/Seoul")
_REPLAY_DAY = date(2026, 6, 16)
_REPLAY_WINDOW = SessionWindow(
    pre_open=time(8, 30), open=time(9, 0), close=time(15, 30), post_close_end=time(16, 0)
)
_REPLAY_DECISION_AT = datetime(2026, 6, 16, 9, 0, tzinfo=_KST)
_REPLAY_EVENT_AT = datetime(2026, 6, 16, 9, 30, tzinfo=_KST)
_REPLAY_PRICE = Decimal("70000")
_REPLAY_THRESHOLD = "70000"
_REPLAY_UNIVERSE = "KR_LARGE"
_DAY_DELTA = timedelta(days=1)

AVAILABLE_REPLAY_FIXTURES: tuple[str, ...] = ("buy_fill", "hold_noop")


def _replay_thresholds() -> HealthThresholds:
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


def _reason(date_id: str = "260616-1") -> AnalysisReason:
    return AnalysisReason(reason="근거", date_id=DateId(date_id))


def _analysis_decision(*, action: AnalysisAction, symbol: str, decision_id: str) -> AnalysisDecision:
    return AnalysisDecision(
        decision_id=DecisionId(decision_id),
        created_at=_REPLAY_DECISION_AT,
        universe=_REPLAY_UNIVERSE,
        symbol=symbol,
        market=_SUPPORTED_MARKET,
        summary_one_liner="요약",
        bear=BearPerspective(summary="하방", risks=("리스크",), reasons=(_reason(),)),
        bull=BullPerspective(summary="상방", catalysts=("촉매",), reasons=(_reason("260616-2"),)),
        risk_manager=RiskManagerEvaluation(summary="중립", reasons=(_reason("260616-3"),)),
        fund_manager=FundManagerDecision(
            action=action,
            target_weight_percent=Percent("4"),
            rationale="근거",
            reasons=(_reason("260616-4"),),
        ),
        reasons=(_reason("260616-5"),),
    )


def _snapshot(decision: AnalysisDecision) -> DecisionSnapshot:
    return DecisionSnapshot.create(
        decision_id=decision.decision_id,
        created_at=decision.created_at,
        schema_name=ANALYSIS_DECISION_SCHEMA,
        raw_payload=decision.model_dump(mode="json"),
        validation_result=ValidationResult(passed=True, issues=(), schema_name=ANALYSIS_DECISION_SCHEMA),
    )


def _buy_plan(*, symbol: str, decision_id: DecisionId) -> TriggerPlan:
    return TriggerPlan(
        plan_id="replay-plan",
        decision_id=decision_id,
        created_at=_REPLAY_DECISION_AT,
        valid_from=_REPLAY_DECISION_AT,
        expires_at=_REPLAY_DECISION_AT + _DAY_DELTA,
        universe=_REPLAY_UNIVERSE,
        market=Market.KR,
        symbol=symbol,
        action=AnalysisAction.BUY,
        rules=(
            ConditionClause(
                metric=Metric.LAST_TRADE_PRICE,
                comparator=Comparator.LTE,
                threshold=_REPLAY_THRESHOLD,
            ),
        ),
    )


def _quote_tick(*, symbol: str, sequence: int) -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider="replay",
        symbol=symbol,
        market=Market.KR,
        currency=Currency.KRW,
        bid_price=_REPLAY_PRICE,
        ask_price=_REPLAY_PRICE,
        bid_quantity=Decimal("10"),
        ask_quantity=Decimal("10"),
        quote_at=_REPLAY_EVENT_AT,
        received_at=_REPLAY_EVENT_AT,
        provider_sequence=ProviderSequence(
            provider="replay", channel="replay-quote", sequence=sequence, received_at=_REPLAY_EVENT_AT
        ),
    )


def _trade_tick(*, symbol: str, sequence: int) -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="replay",
        symbol=symbol,
        market=Market.KR,
        currency=Currency.KRW,
        price=_REPLAY_PRICE,
        quantity=Decimal("10"),
        trade_at=_REPLAY_EVENT_AT,
        received_at=_REPLAY_EVENT_AT,
        provider_sequence=ProviderSequence(
            provider="replay", channel="replay-trade", sequence=sequence, received_at=_REPLAY_EVENT_AT
        ),
    )


def build_replay_snapshot_payload(
    *,
    symbol_universe: str = _REPLAY_UNIVERSE,
    created_at: datetime = _REPLAY_DECISION_AT,
    expires_at: datetime = _REPLAY_DECISION_AT + _DAY_DELTA,
) -> dict:
    """Build a canonical, hash-stamped execution-inputs snapshot payload for replay.

    Exposed so operator tests can write tampered/stale/universe-mismatch variants and
    prove the ``ValidatedExecutionInputsProvider`` path yields zero fills.
    """

    payload: dict = {
        "schema_version": 1,
        "source_id": "replay-fixture",
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "universe": symbol_universe,
        "allocator_decision": _build_allocator(symbol_universe=symbol_universe).model_dump(
            mode="json"
        ),
        "portfolio_policy": {
            "mode": RiskMode.REBALANCING.value,
            "allocator_symbol_target_weight": "4",
        },
    }
    payload["payload_sha256"] = compute_snapshot_payload_hash(payload)
    return payload


def _seed_initial_cash() -> CashSnapshot:
    return CashSnapshot(
        currency=Currency.KRW,
        amount=Decimal("100000000"),
        account_role=AccountRole.PAPER,
        as_of=_REPLAY_DECISION_AT,
    )


@dataclass
class PaperFastLoopStack:
    """Owns the durable + in-memory resources of one composed offline fast-loop stack.

    The ledger / journal / active-store SQLite connections are the only resources that
    hold OS handles; ``close()`` releases them exactly once (idempotent) and attempts
    every handle even if one raises, so a temp dir can be deleted with zero pending
    handles (Windows-safe). The in-memory stores need no teardown.
    """

    orchestrator: FastLoopExecutionOrchestrator
    latest_store: LatestMarketStateStore
    ledger: SQLiteLedger
    journal: SqliteTriggerJournal
    active_store: ActiveDecisionStore
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        first_error: Exception | None = None
        # 생성 역순(active_store → journal → ledger)으로 닫아 부분-생성 정리 경로
        # (``_build_stack`` except 절)와 동일한 teardown 순서를 보장한다.
        for resource in (self.active_store, self.journal, self.ledger):
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - close every handle, re-raise first
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def __enter__(self) -> "PaperFastLoopStack":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@contextmanager
def build_offline_paper_fast_loop_stack(
    temp_root: Path, *, provider: ValidatedExecutionInputsProvider
) -> Iterator[PaperFastLoopStack]:
    """Context-managed ``PaperFastLoopStack`` that always closes its handles on exit."""

    stack = _build_stack(temp_root, provider=provider)
    try:
        yield stack
    finally:
        stack.close()


def _build_stack(
    temp_root: Path, *, provider: ValidatedExecutionInputsProvider
) -> PaperFastLoopStack:
    """(Re)build the full fast-loop stack against the on-disk DBs in ``temp_root``.

    In-memory state (latest/rolling stores, health tracker, engine) is fresh each call;
    the ledger / journal / active-store SQLite files persist, so calling this twice models
    a composition restart against the same durable state. The caller owns ``close()``.

    Construction is fail-closed: if *any* step after the first SQLite resource is opened
    raises (a later resource constructor, or an in-memory dependency), every
    already-opened SQLite handle is closed in reverse order and the **original** exception
    is re-raised, so a partial construction never leaks a handle.
    """

    ledger: SQLiteLedger | None = None
    journal: SqliteTriggerJournal | None = None
    active_store: ActiveDecisionStore | None = None
    try:
        ledger = SQLiteLedger(temp_root / "ledger.sqlite3")
        broker = PaperBrokerAdapter(ledger, initial_cash=_seed_initial_cash())
        journal = SqliteTriggerJournal(temp_root / "journal.sqlite3")
        active_store = ActiveDecisionStore(temp_root / "active.sqlite3")

        latest = LatestMarketStateStore()
        rolling = RollingTradeHistoryStore(
            retention=RollingRetentionPolicy(hard_max_events=1000, hard_max_age_seconds=Decimal("86400"))
        )
        calendar = build_explicit_schedule(
            timezone=_KST, trading_days=[_REPLAY_DAY], window=_REPLAY_WINDOW
        )
        tracker = MarketHealthTracker(_replay_thresholds())
        # connect/subscribe는 decision 시점(09:00)에 기록해 평가 시점(09:30)까지 안정 uptime을 확보한다.
        tracker.record_transport_event(kind="connected", at=_REPLAY_DECISION_AT, now=_REPLAY_DECISION_AT)
        tracker.record_transport_event(
            kind="all_subscribed", at=_REPLAY_DECISION_AT, now=_REPLAY_DECISION_AT
        )
        # market-data HEALTHY는 최근 quote를 요구한다(trade만으로는 quote starvation).
        tracker.record_market_event(event_type="best_bid_ask", at=_REPLAY_EVENT_AT, now=_REPLAY_EVENT_AT)

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
            execution_inputs_provider=provider,
            coordinator=coordinator,
        )
    except BaseException:
        # 부분 생성된 SQLite handle을 역순으로 정리하되, cleanup 오류가 원래 예외를 가리지
        # 않게 한다(원래 construction 예외를 그대로 전파).
        for resource in (active_store, journal, ledger):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - cleanup must not mask the original error
                    pass
        raise
    return PaperFastLoopStack(
        orchestrator=orchestrator,
        latest_store=latest,
        ledger=ledger,
        journal=journal,
        active_store=active_store,
    )


def _drive_one_event(
    orchestrator: FastLoopExecutionOrchestrator, latest: LatestMarketStateStore, *, symbol: str,
    sequence: int,
) -> str:
    """Seed a quote + trade into ``latest`` and route one applied trade update."""

    latest.apply(_quote_tick(symbol=symbol, sequence=sequence), now=_REPLAY_EVENT_AT)
    latest.apply(_trade_tick(symbol=symbol, sequence=sequence + 1), now=_REPLAY_EVENT_AT)
    update = AppliedMarketUpdate(
        market=Market.KR,
        symbol=symbol,
        event_type=MarketEventType.TRADE,
        provider="replay",
        channel="replay-trade",
        sequence=sequence,
        applied_at=_REPLAY_EVENT_AT,
    )
    return orchestrator.handle_applied_update(update).status.value


def replay_offline(
    *,
    settings: RuntimePaperFastLoopSettings,
    temp_dir: Path | str,
    fixture: str,
    snapshot_path: Path | str | None = None,
) -> OfflineReplayResult:
    """Run a deterministic offline replay in ``temp_dir`` (never the runtime paths).

    Execution inputs flow through the real ``ValidatedExecutionInputsProvider`` loaded
    from ``snapshot_path`` (or a canonical valid snapshot written into ``temp_dir`` when
    omitted). Three phases prove idempotency: first event, repeat event (same stack),
    restart event (rebuilt stack, same DBs). A snapshot that fails to load yields a
    zero-execution result with a sanitized ``snapshot_reason``.

    Raises ``ValueError`` for an unknown fixture or a missing ``temp_dir``.
    """

    if fixture not in AVAILABLE_REPLAY_FIXTURES:
        raise ValueError(f"unknown replay fixture: {fixture!r}")

    temp_root = Path(temp_dir)
    if not temp_root.exists():
        raise ValueError(f"replay temp_dir does not exist: {temp_root}")

    symbol = settings.symbol
    is_buy = fixture == "buy_fill"
    action = AnalysisAction.BUY if is_buy else AnalysisAction.HOLD
    decision = _analysis_decision(action=action, symbol=symbol, decision_id=f"replay-{fixture}")
    plan = _buy_plan(symbol=symbol, decision_id=decision.decision_id) if is_buy else None

    # 실행 입력 snapshot을 디스크에서 fail-closed 로드 → ValidatedExecutionInputsProvider.
    if snapshot_path is None:
        snapshot_file = temp_root / "execution_inputs_snapshot.json"
        snapshot_file.write_text(_json_dumps(build_replay_snapshot_payload()), encoding="utf-8")
    else:
        snapshot_file = Path(snapshot_path)
    try:
        snapshot = load_execution_inputs_snapshot(snapshot_file)
    except ExecutionInputsSnapshotError as exc:
        return _empty_replay_result(fixture, symbol, snapshot_reason=exc.reason_code)
    provider = ValidatedExecutionInputsProvider(snapshot=snapshot)

    # active decision은 한 번만 publish하고 모든 phase가 같은 파일을 공유한다. publish 전용
    # store는 즉시 닫아 handle을 남기지 않는다(이후 phase가 같은 파일을 다시 연다).
    publish_store = ActiveDecisionStore(temp_root / "active.sqlite3")
    try:
        publish_store.publish(
            DecisionPublicationCandidate(
                snapshot=_snapshot(decision),
                plan=plan,
                valid_from=_REPLAY_DECISION_AT,
                expires_at=_REPLAY_DECISION_AT + _DAY_DELTA,
            ),
            now=_REPLAY_DECISION_AT,
        )
    finally:
        publish_store.close()

    statuses: list[str] = []
    # Phase 1 + 2: 동일 스택에서 첫 이벤트와 반복 이벤트(arming 내 중복). 끝나면 close.
    with build_offline_paper_fast_loop_stack(temp_root, provider=provider) as stack:
        first_status = _drive_one_event(stack.orchestrator, stack.latest_store, symbol=symbol, sequence=10)
        repeat_status = _drive_one_event(stack.orchestrator, stack.latest_store, symbol=symbol, sequence=20)
    statuses.extend((first_status, repeat_status))

    # Phase 3: 같은 DB 파일로 스택을 재구성(컴포지션 재시작) 후 한 이벤트 더. 끝나면 close.
    with build_offline_paper_fast_loop_stack(temp_root, provider=provider) as restart_stack:
        restart_status = _drive_one_event(
            restart_stack.orchestrator, restart_stack.latest_store, symbol=symbol, sequence=30
        )
    statuses.append(restart_status)

    committed = sum(1 for status in statuses if status == FastLoopExecutionStatus.COMMITTED.value)
    journal_summary = sqlite_inspector.summarize_journal(temp_root / "journal.sqlite3")
    ledger_summary = sqlite_inspector.summarize_ledger(
        temp_root / "ledger.sqlite3", symbol=symbol, market=_SUPPORTED_MARKET
    )
    cash_amount = _read_cash_amount(temp_root / "ledger.sqlite3")
    return OfflineReplayResult(
        fixture=fixture,
        market=_SUPPORTED_MARKET,
        symbol=symbol,
        snapshot_loaded=True,
        snapshot_reason=None,
        event_count=len(statuses),
        statuses=tuple(statuses),
        first_status=first_status,
        repeat_status=repeat_status,
        restart_status=restart_status,
        committed_count=committed,
        order_result_count=ledger_summary.order_result_count,
        filled_result_count=ledger_summary.filled_result_count,
        fill_count=ledger_summary.fill_count,
        journal_state_counts=tuple(
            (item.state, item.count) for item in journal_summary.state_counts
        ),
        journal_terminal_count=journal_summary.terminal_count,
        final_position_quantity=ledger_summary.position_quantity,
        final_cash_amount=cash_amount,
    )


def _empty_replay_result(fixture: str, symbol: str, *, snapshot_reason: str) -> OfflineReplayResult:
    return OfflineReplayResult(
        fixture=fixture,
        market=_SUPPORTED_MARKET,
        symbol=symbol,
        snapshot_loaded=False,
        snapshot_reason=snapshot_reason,
        event_count=0,
        statuses=(),
        first_status=None,
        repeat_status=None,
        restart_status=None,
        committed_count=0,
        order_result_count=0,
        filled_result_count=0,
        fill_count=0,
        journal_state_counts=(),
        journal_terminal_count=0,
        final_position_quantity=None,
        final_cash_amount=None,
    )


def _read_cash_amount(ledger_path: Path) -> str | None:
    with sqlite_inspector.open_read_only(ledger_path) as conn:
        row = conn.execute(
            "SELECT amount FROM current_cash WHERE currency = ? AND account_role = ?",
            (Currency.KRW.value, AccountRole.PAPER.value),
        ).fetchone()
    return None if row is None else str(row["amount"])


def _json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload)


class _LatestStateAdapter:
    """LatestMarketStateStore를 PortfolioMarketStateSource로 노출하는 얇은 어댑터."""

    def __init__(self, store: LatestMarketStateStore) -> None:
        self._store = store

    def get_snapshot(self, symbol: str, market: Market, *, now: datetime):
        return self._store.peek(market, symbol, now=now)


def _build_allocator(*, symbol_universe: str):
    from allocator import (
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

    reasons = (AllocatorReason(reason="근거", date_id=DateId("260616-1")),)
    weights = TargetWeights(kr=Percent("50"), us=Percent("30"), gold=Percent("20"))
    cash = Percent("20")
    return AllocatorDecision(
        decision_id=DecisionId("replay-allocator-001"),
        created_at=_REPLAY_DECISION_AT,
        universe=symbol_universe,
        summary_one_liner="배분 유지",
        gold_policy_mode=GoldPolicyMode.NORMAL,
        signal_summary=SignalSummary(summary="신호", reasons=reasons),
        cash_manager=CashManagerView(summary="현금", recommended_cash_percent=cash, reasons=reasons),
        asset_allocator=AssetAllocatorView(summary="배분", target_weights=weights, reasons=reasons),
        consistency_checker=ConsistencyCheckerView(passed=True, summary="확인", reasons=reasons),
        cash_policy=CashPolicy(cash_target_percent=cash, rationale="유동성", reasons=reasons),
        target_weights=weights,
        reasons=reasons,
    )
