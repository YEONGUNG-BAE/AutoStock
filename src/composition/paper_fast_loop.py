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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
    ValidatedExecutionInputsSnapshot,
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
    "PaperFastLoopPlan",
    "PaperFastLoopInspection",
    "OfflineReplayResult",
    "AVAILABLE_REPLAY_FIXTURES",
    "build_paper_fast_loop_plan",
    "inspect_paper_fast_loop",
    "replay_offline",
]


class PaperFastLoopOutcome(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"


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
class PaperFastLoopInspection:
    """Read-only inspection of the configured fast-loop databases."""

    market: str
    symbol: str
    ledger: sqlite_inspector.LedgerSummary | None
    journal: sqlite_inspector.JournalSummary | None
    active_store: sqlite_inspector.ActiveStoreSummary | None
    missing_databases: tuple[str, ...]


@dataclass(frozen=True)
class OfflineReplayResult:
    """Deterministic offline replay summary built from temp-dir databases."""

    fixture: str
    market: str
    symbol: str
    event_count: int
    statuses: tuple[str, ...]
    committed_count: int
    final_position_quantity: str | None
    journal_terminal_count: int


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
    """Validate-only: load+validate snapshot, run single-symbol preflight. No execution."""

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

    # 기존 ledger가 있으면 단일 종목 preflight 수행(없으면 통과).
    if paths.ledger_path.exists():
        try:
            positions = sqlite_inspector.scan_positions(paths.ledger_path)
        except sqlite_inspector.SqliteInspectionError:
            reasons.append("ledger_unreadable")
        else:
            reasons.extend(_position_preflight_reasons(positions, symbol=symbol))

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
    *, settings: RuntimePaperFastLoopSettings, base_dir: Path | str = Path(".")
) -> PaperFastLoopInspection:
    """Read-only inspection of the configured ledger / journal / active store."""

    paths = PaperFastLoopPaths.from_settings(settings, base_dir=base_dir)
    market = settings.market
    symbol = settings.symbol
    missing: list[str] = []

    ledger_summary: sqlite_inspector.LedgerSummary | None = None
    if paths.ledger_path.exists():
        ledger_summary = sqlite_inspector.summarize_ledger(
            paths.ledger_path, symbol=symbol, market=market
        )
    else:
        missing.append("ledger")

    journal_summary: sqlite_inspector.JournalSummary | None = None
    if paths.trigger_journal_path.exists():
        journal_summary = sqlite_inspector.summarize_journal(paths.trigger_journal_path)
    else:
        missing.append("trigger_journal")

    active_summary: sqlite_inspector.ActiveStoreSummary | None = None
    if paths.active_decision_store_path.exists():
        active_summary = sqlite_inspector.summarize_active_store(
            paths.active_decision_store_path, symbol=symbol, market=market
        )
    else:
        missing.append("active_decision_store")

    return PaperFastLoopInspection(
        market=market,
        symbol=symbol,
        ledger=ledger_summary,
        journal=journal_summary,
        active_store=active_summary,
        missing_databases=tuple(missing),
    )


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


def replay_offline(
    *, settings: RuntimePaperFastLoopSettings, temp_dir: Path | str, fixture: str
) -> OfflineReplayResult:
    """Run a deterministic offline replay in ``temp_dir`` (never the runtime paths).

    Raises ``ValueError`` for an unknown fixture name.
    """

    if fixture not in AVAILABLE_REPLAY_FIXTURES:
        raise ValueError(f"unknown replay fixture: {fixture!r}")

    temp_root = Path(temp_dir)
    if not temp_root.exists():
        raise ValueError(f"replay temp_dir does not exist: {temp_root}")

    symbol = settings.symbol
    is_buy = fixture == "buy_fill"
    action = AnalysisAction.BUY if is_buy else AnalysisAction.HOLD
    decision = _analysis_decision(
        action=action, symbol=symbol, decision_id=f"replay-{fixture}"
    )
    plan = _buy_plan(symbol=symbol, decision_id=decision.decision_id) if is_buy else None

    # 모든 상태는 caller temp_dir에만 쓴다(runtime/ 경로 미사용).
    ledger = SQLiteLedger(temp_root / "ledger.sqlite3")
    broker = PaperBrokerAdapter(
        ledger,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=Decimal("100000000"),
            account_role=AccountRole.PAPER,
            as_of=_REPLAY_DECISION_AT,
        ),
    )
    journal = SqliteTriggerJournal(temp_root / "journal.sqlite3")
    active_store = ActiveDecisionStore(temp_root / "active.sqlite3")

    active_store.publish(
        DecisionPublicationCandidate(
            snapshot=_snapshot(decision),
            plan=plan,
            valid_from=_REPLAY_DECISION_AT,
            expires_at=_REPLAY_DECISION_AT + _DAY_DELTA,
        ),
        now=_REPLAY_DECISION_AT,
    )

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
    tracker.record_market_event(
        event_type="best_bid_ask", at=_REPLAY_EVENT_AT, now=_REPLAY_EVENT_AT
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
    provider = StaticExecutionInputsProvider(
        allocator_decision=_build_allocator(symbol_universe=_REPLAY_UNIVERSE),
        portfolio_policy=PaperPortfolioPolicy(
            mode=RiskMode.REBALANCING, allocator_symbol_target_weight=Percent("4")
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

    # 가격 산출을 위해 quote를 먼저 latest store에 seed한다(applied update로 라우팅하지 않음).
    latest.apply(_quote_tick(symbol=symbol, sequence=1), now=_REPLAY_EVENT_AT)

    statuses: list[str] = []
    events = [_trade_tick(symbol=symbol, sequence=2)]
    for index, event in enumerate(events, start=1):
        latest.apply(event, now=_REPLAY_EVENT_AT)
        rolling.observe(event, now=_REPLAY_EVENT_AT)
        update = AppliedMarketUpdate(
            market=Market.KR,
            symbol=symbol,
            event_type=MarketEventType.TRADE,
            provider="replay",
            channel="replay-trade",
            sequence=index,
            applied_at=_REPLAY_EVENT_AT,
        )
        result = orchestrator.handle_applied_update(update)
        statuses.append(result.status.value)

    committed = sum(1 for status in statuses if status == FastLoopExecutionStatus.COMMITTED.value)
    journal_summary = sqlite_inspector.summarize_journal(temp_root / "journal.sqlite3")
    ledger_summary = sqlite_inspector.summarize_ledger(
        temp_root / "ledger.sqlite3", symbol=symbol, market=_SUPPORTED_MARKET
    )
    return OfflineReplayResult(
        fixture=fixture,
        market=_SUPPORTED_MARKET,
        symbol=symbol,
        event_count=len(events),
        statuses=tuple(statuses),
        committed_count=committed,
        final_position_quantity=ledger_summary.position_quantity,
        journal_terminal_count=journal_summary.terminal_count,
    )


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
