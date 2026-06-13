"""RTM-7c.2 — fast-loop paper execution orchestration (offline library wiring).

MarketMonitor의 neutral post-apply hook → active decision atomic read → frozen snapshot/
indicator → session/health gate → PaperExecutionCoordinator.process() 순으로 동기 직렬
평가한다. queue/coalescing 없음; exact same-now 계약; KIS/network/runtime DB/scheduler
미연결.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from allocator.models import AllocatorDecision
from domain._datetime import require_timezone_aware_datetime
from domain.enums import Market
from market_data.conditions import rule_required_windows
from market_data.indicators import IndicatorContext, build_indicator_context
from market_data.latest_state import LatestMarketStateStore
from market_data.models import MarketEventType
from market_data.monitor import AppliedMarketUpdate
from market_data.rolling_window import RollingTradeHistoryStore
from market_data.trigger_engine import TradingPermission

from execution.paper_execution_coordinator import (
    CoordinatorResult,
    CoordinatorStatus,
    PaperExecutionCoordinator,
)
from execution.paper_portfolio_context import PaperPortfolioPolicy

from orchestration.active_decision_store import ActiveBundle, PublicationError
from orchestration.execution_gate import (
    REASON_GATE_PROVIDER_ERROR,
    ExecutionGateProvider,
    evaluate_gate_safe,
    gate_execution_reason,
)

__all__ = [
    "ActiveDecisionReader",
    "ExecutionGateProvider",
    "ExecutionInputsProvider",
    "FastLoopExecutionEvidence",
    "FastLoopExecutionOrchestrator",
    "FastLoopExecutionResult",
    "FastLoopExecutionStatus",
    "StaticExecutionInputsProvider",
]


class FastLoopExecutionStatus(StrEnum):
    """orchestrator 한 update의 typed 결과 분류."""

    COMMITTED = "committed"
    SUPPRESSED = "suppressed"
    FAILED_CLOSED = "failed_closed"
    SKIPPED_TERMINAL = "skipped_terminal"
    TRIGGERED_ABORTED = "triggered_aborted"
    UNCERTAIN = "uncertain"
    RECONCILE_REQUIRED = "reconcile_required"
    HELD_SESSION = "held_session"
    HELD_HEALTH = "held_health"
    GATE_AS_OF_MISMATCH = "gate_as_of_mismatch"
    GATE_MARKET_MISMATCH = "gate_market_mismatch"
    GATE_PROVIDER_ERROR = "gate_provider_error"
    MISSING_ACTIVE_DECISION = "missing_active_decision"
    ACTIVE_DECISION_CORRUPT = "active_decision_corrupt"
    ACTIVE_DECISION_IDENTITY_MISMATCH = "active_decision_identity_mismatch"
    ACTIVE_DECISION_NOT_YET_VALID = "active_decision_not_yet_valid"
    ACTIVE_DECISION_EXPIRED = "active_decision_expired"
    SNAPSHOT_AS_OF_MISMATCH = "snapshot_as_of_mismatch"
    SNAPSHOT_IDENTITY_MISMATCH = "snapshot_identity_mismatch"
    EXECUTION_INPUTS_UNAVAILABLE = "execution_inputs_unavailable"
    HALTED_RECONCILE_REQUIRED = "halted_reconciliation_required"
    MALFORMED_UPDATE = "malformed_update"
    COORDINATOR_INTERNAL_ERROR = "coordinator_internal_error"
    GLOBAL_TERMINAL_FAIL_CLOSED = "global_terminal_fail_closed"
    EVIDENCE_SINK_ERROR = "evidence_sink_error"


_COORDINATOR_STATUS_MAP: dict[CoordinatorStatus, FastLoopExecutionStatus] = {
    CoordinatorStatus.SUPPRESSED: FastLoopExecutionStatus.SUPPRESSED,
    CoordinatorStatus.FAILED_CLOSED: FastLoopExecutionStatus.FAILED_CLOSED,
    CoordinatorStatus.SKIPPED_TERMINAL: FastLoopExecutionStatus.SKIPPED_TERMINAL,
    CoordinatorStatus.RECONCILE_REQUIRED: FastLoopExecutionStatus.RECONCILE_REQUIRED,
    CoordinatorStatus.TRIGGERED_ABORTED: FastLoopExecutionStatus.TRIGGERED_ABORTED,
    CoordinatorStatus.COMMITTED: FastLoopExecutionStatus.COMMITTED,
    CoordinatorStatus.UNCERTAIN: FastLoopExecutionStatus.UNCERTAIN,
}


class ActiveDecisionReader(Protocol):
    def read_active(self, market: Market | str, symbol: str) -> ActiveBundle | None: ...


@dataclass(frozen=True)
class ExecutionInputs:
    allocator_decision: AllocatorDecision
    portfolio_policy: PaperPortfolioPolicy


class ExecutionInputsProvider(Protocol):
    def resolve(self, *, active: ActiveBundle, now: datetime) -> ExecutionInputs: ...


@dataclass(frozen=True)
class StaticExecutionInputsProvider:
    """테스트/오프라인 wiring용 고정 execution inputs."""

    allocator_decision: AllocatorDecision
    portfolio_policy: PaperPortfolioPolicy

    def resolve(self, *, active: ActiveBundle, now: datetime) -> ExecutionInputs:
        require_timezone_aware_datetime(now, field_name="now")
        decision = active.bundle.decision
        if decision.symbol != active.symbol:
            raise ValueError("active symbol mismatch")
        if decision.market != active.market:
            raise ValueError("active market mismatch")
        alloc = self.allocator_decision
        require_timezone_aware_datetime(alloc.created_at, field_name="allocator_decision.created_at")
        if alloc.created_at > now:
            raise ValueError("allocator created_at in future")
        if alloc.universe != decision.universe:
            raise ValueError("allocator universe mismatch")
        if active.bundle.plan is not None:
            plan = active.bundle.plan
            if plan.symbol != active.symbol or plan.market.value != active.market:
                raise ValueError("plan identity mismatch")
            if plan.universe != decision.universe:
                raise ValueError("universe mismatch")
        return ExecutionInputs(
            allocator_decision=self.allocator_decision,
            portfolio_policy=self.portfolio_policy,
        )


@dataclass(frozen=True)
class FastLoopExecutionEvidence:
    """append-only orchestration evidence. raw frame/credential/exception repr 금지."""

    timestamp: datetime
    market: str
    symbol: str
    event_type: str
    provider: str
    channel: str
    sequence: int
    status: str
    reason_code: str | None
    publication_id: str | None
    decision_id: str | None
    plan_id: str | None
    trigger_id: str | None
    idempotency_key: str | None
    coordinator_status: str | None


@dataclass(frozen=True)
class FastLoopExecutionResult:
    status: FastLoopExecutionStatus
    reason_code: str | None = None
    publication_id: str | None = None
    decision_id: str | None = None
    plan_id: str | None = None
    trigger_id: str | None = None
    idempotency_key: str | None = None
    coordinator_status: str | None = None


class FastLoopExecutionOrchestrator:
    """APPLIED trade/quote마다 동기 직렬로 paper execution을 평가한다."""

    def __init__(
        self,
        *,
        active_reader: ActiveDecisionReader,
        latest_store: LatestMarketStateStore,
        execution_gate: ExecutionGateProvider,
        execution_inputs_provider: ExecutionInputsProvider,
        coordinator: PaperExecutionCoordinator,
        rolling_store: RollingTradeHistoryStore | None = None,
        on_evidence: Callable[[FastLoopExecutionEvidence], None] | None = None,
    ) -> None:
        self._active_reader = active_reader
        self._latest_store = latest_store
        self._rolling_store = rolling_store
        self._execution_gate = execution_gate
        self._execution_inputs_provider = execution_inputs_provider
        self._coordinator = coordinator
        self._on_evidence = on_evidence
        self._halted_symbols: set[tuple[str, str]] = set()
        self._global_terminal = False

    def handle_applied_update(self, update: AppliedMarketUpdate) -> FastLoopExecutionResult:
        if self._global_terminal:
            return self._result(
                FastLoopExecutionStatus.GLOBAL_TERMINAL_FAIL_CLOSED,
                reason_code=FastLoopExecutionStatus.GLOBAL_TERMINAL_FAIL_CLOSED.value,
            )

        preflight = _preflight_update(update)
        if preflight is not None:
            return preflight

        key = (update.market.value, update.symbol)
        if key in self._halted_symbols:
            return self._emit_and_return(
                update,
                FastLoopExecutionStatus.HALTED_RECONCILE_REQUIRED,
                reason_code=FastLoopExecutionStatus.HALTED_RECONCILE_REQUIRED.value,
            )

        gate, gate_error = evaluate_gate_safe(
            self._execution_gate, market=update.market, now=update.applied_at
        )
        if gate_error is not None:
            status = (
                FastLoopExecutionStatus.GATE_PROVIDER_ERROR
                if gate_error == REASON_GATE_PROVIDER_ERROR
                else FastLoopExecutionStatus(gate_error)
            )
            return self._emit_and_return(update, status, reason_code=gate_error)
        assert gate is not None
        held = gate_execution_reason(
            gate, update_market=update.market, update_applied_at=update.applied_at
        )
        if held is not None:
            return self._emit_and_return(
                update, FastLoopExecutionStatus(held), reason_code=held
            )

        try:
            active = self._active_reader.read_active(update.market, update.symbol)
        except PublicationError:
            self._global_terminal = True
            return self._emit_and_return(
                update,
                FastLoopExecutionStatus.ACTIVE_DECISION_CORRUPT,
                reason_code=FastLoopExecutionStatus.ACTIVE_DECISION_CORRUPT.value,
            )

        if active is None:
            return self._emit_and_return(
                update,
                FastLoopExecutionStatus.MISSING_ACTIVE_DECISION,
                reason_code=FastLoopExecutionStatus.MISSING_ACTIVE_DECISION.value,
            )

        identity_reason = _active_identity_reason(active, update)
        if identity_reason is not None:
            return self._emit_and_return(
                update,
                FastLoopExecutionStatus.ACTIVE_DECISION_IDENTITY_MISMATCH,
                reason_code=identity_reason,
                publication_id=active.publication_id,
                decision_id=active.decision_id,
                plan_id=active.plan_id,
            )

        validity_reason = _active_validity_reason(active, update.applied_at)
        if validity_reason is not None:
            status = (
                FastLoopExecutionStatus.ACTIVE_DECISION_NOT_YET_VALID
                if validity_reason == "not_yet_valid"
                else FastLoopExecutionStatus.ACTIVE_DECISION_EXPIRED
            )
            return self._emit_and_return(
                update,
                status,
                reason_code=status.value,
                publication_id=active.publication_id,
                decision_id=active.decision_id,
                plan_id=active.plan_id,
            )

        snapshot = self._latest_store.peek(
            update.market, update.symbol, now=update.applied_at
        )
        if snapshot.evaluated_at != update.applied_at:
            return self._emit_and_return(
                update,
                FastLoopExecutionStatus.SNAPSHOT_AS_OF_MISMATCH,
                reason_code=FastLoopExecutionStatus.SNAPSHOT_AS_OF_MISMATCH.value,
                publication_id=active.publication_id,
                decision_id=active.decision_id,
                plan_id=active.plan_id,
            )
        if snapshot.market != update.market or snapshot.symbol != update.symbol:
            return self._emit_and_return(
                update,
                FastLoopExecutionStatus.SNAPSHOT_IDENTITY_MISMATCH,
                reason_code=FastLoopExecutionStatus.SNAPSHOT_IDENTITY_MISMATCH.value,
                publication_id=active.publication_id,
                decision_id=active.decision_id,
                plan_id=active.plan_id,
            )

        indicators: IndicatorContext | None = None
        plan = active.bundle.plan
        if plan is not None and self._rolling_store is not None:
            specs = rule_required_windows(plan.rules)
            if specs:
                history = self._rolling_store.peek_history(
                    update.market, update.symbol, now=update.applied_at
                )
                indicators = build_indicator_context(
                    history, specs, now=update.applied_at
                )

        permission = TradingPermission(
            market=update.market,
            allowed=True,
            checked_at=update.applied_at,
            valid_until=update.applied_at,
            reason_code="session_and_health_ready",
        )

        try:
            inputs = self._execution_inputs_provider.resolve(
                active=active, now=update.applied_at
            )
        except Exception:
            return self._emit_and_return(
                update,
                FastLoopExecutionStatus.EXECUTION_INPUTS_UNAVAILABLE,
                reason_code=FastLoopExecutionStatus.EXECUTION_INPUTS_UNAVAILABLE.value,
                publication_id=active.publication_id,
                decision_id=active.decision_id,
                plan_id=active.plan_id,
            )

        try:
            coord_result = self._coordinator.process(
                bundle=active.bundle,
                snapshot=snapshot,
                permission=permission,
                allocator_decision=inputs.allocator_decision,
                portfolio_policy=inputs.portfolio_policy,
                now=update.applied_at,
                indicators=indicators,
            )
        except Exception:
            self._global_terminal = True
            return self._emit_and_return(
                update,
                FastLoopExecutionStatus.COORDINATOR_INTERNAL_ERROR,
                reason_code=FastLoopExecutionStatus.COORDINATOR_INTERNAL_ERROR.value,
                publication_id=active.publication_id,
                decision_id=active.decision_id,
                plan_id=active.plan_id,
            )

        mapped = _map_coordinator_result(coord_result)
        if mapped.status in (
            FastLoopExecutionStatus.UNCERTAIN,
            FastLoopExecutionStatus.RECONCILE_REQUIRED,
        ):
            self._halted_symbols.add(key)

        return self._emit_and_return(
            update,
            mapped.status,
            reason_code=mapped.reason_code,
            publication_id=active.publication_id,
            decision_id=active.decision_id,
            plan_id=active.plan_id,
            trigger_id=mapped.trigger_id,
            idempotency_key=mapped.idempotency_key,
            coordinator_status=mapped.coordinator_status,
        )

    def _emit_and_return(
        self,
        update: AppliedMarketUpdate,
        status: FastLoopExecutionStatus,
        *,
        reason_code: str | None = None,
        publication_id: str | None = None,
        decision_id: str | None = None,
        plan_id: str | None = None,
        trigger_id: str | None = None,
        idempotency_key: str | None = None,
        coordinator_status: str | None = None,
    ) -> FastLoopExecutionResult:
        result = FastLoopExecutionResult(
            status=status,
            reason_code=reason_code,
            publication_id=publication_id,
            decision_id=decision_id,
            plan_id=plan_id,
            trigger_id=trigger_id,
            idempotency_key=idempotency_key,
            coordinator_status=coordinator_status,
        )
        if self._on_evidence is None:
            return result
        evidence = FastLoopExecutionEvidence(
            timestamp=update.applied_at,
            market=update.market.value,
            symbol=update.symbol,
            event_type=update.event_type.value,
            provider=update.provider,
            channel=update.channel,
            sequence=update.sequence,
            status=status.value,
            reason_code=reason_code,
            publication_id=publication_id,
            decision_id=decision_id,
            plan_id=plan_id,
            trigger_id=trigger_id,
            idempotency_key=idempotency_key,
            coordinator_status=coordinator_status,
        )
        try:
            self._on_evidence(evidence)
        except Exception:
            self._global_terminal = True
            if status is FastLoopExecutionStatus.COMMITTED:
                return result
            return FastLoopExecutionResult(
                status=FastLoopExecutionStatus.EVIDENCE_SINK_ERROR,
                reason_code=FastLoopExecutionStatus.EVIDENCE_SINK_ERROR.value,
            )
        return result

    def _result(
        self, status: FastLoopExecutionStatus, *, reason_code: str | None = None
    ) -> FastLoopExecutionResult:
        return FastLoopExecutionResult(status=status, reason_code=reason_code)


def _malformed_result() -> FastLoopExecutionResult:
    return FastLoopExecutionResult(
        status=FastLoopExecutionStatus.MALFORMED_UPDATE,
        reason_code=FastLoopExecutionStatus.MALFORMED_UPDATE.value,
    )


def _preflight_update(update: object) -> FastLoopExecutionResult | None:
    """public boundary 방어 검증. malformed 입력에서 예외를 발생시키지 않는다."""
    if not isinstance(update, AppliedMarketUpdate):
        return _malformed_result()
    if not isinstance(update.market, Market):
        return _malformed_result()
    if not isinstance(update.event_type, MarketEventType):
        return _malformed_result()
    if update.event_type not in (MarketEventType.TRADE, MarketEventType.BEST_BID_ASK):
        return _malformed_result()
    try:
        require_timezone_aware_datetime(update.applied_at, field_name="applied_at")
    except Exception:
        return _malformed_result()
    if not isinstance(update.symbol, str) or not update.symbol.strip():
        return _malformed_result()
    if not isinstance(update.provider, str) or not update.provider.strip():
        return _malformed_result()
    if not isinstance(update.channel, str) or not update.channel.strip():
        return _malformed_result()
    seq = update.sequence
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return _malformed_result()
    return None


def _active_identity_reason(
    active: ActiveBundle, update: AppliedMarketUpdate
) -> str | None:
    if active.market != update.market.value or active.symbol != update.symbol:
        return FastLoopExecutionStatus.ACTIVE_DECISION_IDENTITY_MISMATCH.value
    decision = active.bundle.decision
    if decision.symbol != update.symbol or decision.market != update.market.value:
        return FastLoopExecutionStatus.ACTIVE_DECISION_IDENTITY_MISMATCH.value
    plan = active.bundle.plan
    if plan is not None and (plan.symbol != update.symbol or plan.market != update.market):
        return FastLoopExecutionStatus.ACTIVE_DECISION_IDENTITY_MISMATCH.value
    return None


def _active_validity_reason(active: ActiveBundle, now: datetime) -> str | None:
    valid_from = active.valid_from
    expires_at = active.expires_at
    plan = active.bundle.plan
    if plan is not None:
        valid_from = plan.valid_from
        expires_at = plan.expires_at
    if now < valid_from:
        return "not_yet_valid"
    if now > expires_at:
        return "expired"
    return None


def _map_coordinator_result(result: CoordinatorResult) -> FastLoopExecutionResult:
    status = _COORDINATOR_STATUS_MAP[result.status]
    trigger_id = result.signal.trigger_id if result.signal is not None else None
    idempotency_key = result.signal.idempotency_key if result.signal is not None else None
    reason_code = result.reason_code
    if reason_code is None and result.trigger_reason is not None:
        reason_code = result.trigger_reason.value
    return FastLoopExecutionResult(
        status=status,
        reason_code=reason_code,
        trigger_id=trigger_id,
        idempotency_key=idempotency_key,
        coordinator_status=result.status.value,
    )
