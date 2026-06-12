"""RTM-7a: paper-only 실행 조정 계층(execution coordinator).

TriggerEngine 의 발화 결과를 canonical paper 포트폴리오 컨텍스트와 안전하게 조립하여
TriggerOrderBridge.dispatch 까지 연결하는 *명시적 라이브러리 API* 다. 이 모듈은
engine/bridge/context-service 를 *호출*만 하며 그 어떤 것도 수정하지 않는다.

RTM-7a 핵심 변경(missed-execution 차단):
- 포지션/현금/포트폴리오/의존성 검증을 **TriggerEngine.evaluate 이전으로** 옮긴다.
  evaluate 는 TRIGGERED 경로에서만 fire budget 을 소비하므로, evaluate 이후에 의존성
  조회가 실패하면 *주문 없이 fire 만 소비*되는 누락이 생긴다. 이를 막기 위해 실행 가격
  구성·canonical context 계산·RiskFilterInput 구조 검증을 모두 evaluate 보다 먼저 수행하고,
  실패 시 engine/journal/broker 를 건드리지 않고 FAILED_CLOSED 로 반환한다.
- caller 가 RiskFilterContext 를 주입하지 않는다. NAV/cash/invested/weight 는 주입 불가능한
  ledger truth 이며, PaperPortfolioContextService 가 ledger + 최신 스냅샷에서 계산한다.

RTM 경계(중요):
- 라이브러리 callable 일 뿐, scheduler/daemon/monitor 에 자동 연결되지 않는다(RTM-7 범위).
- 실제 KIS/live order adapter 를 사용하지 않으며 network/LLM 을 호출하지 않는다.
- 실제 runtime/paper DB 경로를 하드코딩하지 않는다(의존성 주입).

조립 흐름(process):
    as-of/identity preflight → (plan 있을 때) 실행 MarketPrice 구성(BUY=ask, SELL=bid)
        → service.build_context(같은 frozen snapshot 을 current_snapshot 으로 전달 →
          실행가/슬리피지 기준/대상 종목 mark 가 단일 quote 에 정합) → RiskFilterInput 구조 검증
        → engine.replace_bundle(idempotent) → engine.evaluate
        → (비발화면 SUPPRESSED) → RiskFilterInput 재구성(correlation_id=idempotency_key)
        → bridge.dispatch(current_position_quantity=valuation.position_quantity)
        → typed CoordinatorResult
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from analysis.models import AnalysisAction
from allocator.models import AllocatorDecision
from domain._datetime import require_timezone_aware_datetime
from domain.market import MarketPrice
from domain.order import OrderResult
from market_data.indicators import IndicatorContext
from market_data.latest_state import LatestMarketStateSnapshot
from market_data.trigger_engine import (
    DecisionTriggerBundle,
    ReplaceStatus,
    TradingPermission,
    TriggerEngine,
    TriggerReason,
    TriggerSignal,
    TriggerStatus,
)
from risk.models import RiskFilterInput

from execution.paper_portfolio_context import (
    PaperPortfolioContextError,
    PaperPortfolioContextService,
    PaperPortfolioPolicy,
    PaperPortfolioValuation,
)
from execution.trigger_order_bridge import (
    BridgeOutcome,
    BridgeResult,
    TriggerOrderBridge,
)


# --- coordinator-local fail-closed reason codes (broker 호출 전 차단) ---
REASON_SNAPSHOT_AS_OF_MISMATCH = "snapshot_as_of_mismatch"
REASON_INDICATOR_AS_OF_MISMATCH = "indicator_as_of_mismatch"
REASON_SNAPSHOT_IDENTITY_MISMATCH = "snapshot_identity_mismatch"
REASON_DECISION_REPLACE_CONFLICT = "decision_replace_conflict"
REASON_DECISION_REPLACE_OLDER = "decision_replace_older"
REASON_QUOTE_UNAVAILABLE = "quote_unavailable"
REASON_PORTFOLIO_CONTEXT_BUILD_ERROR = "portfolio_context_build_error"
REASON_RISK_INPUT_BUILD_ERROR = "risk_input_build_error"


class CoordinatorStatus(StrEnum):
    """coordinator 한 번의 process 결과 분류."""

    SUPPRESSED = "suppressed"  # engine 비발화(조건 미충족/stale/권한/HOLD 등) — journal/broker 0
    FAILED_CLOSED = "failed_closed"  # coordinator preflight 차단 — journal/broker 0
    SKIPPED_TERMINAL = "skipped_terminal"  # 이미 종결된 발화(journal terminal) — 재처리 skip
    RECONCILE_REQUIRED = "reconcile_required"  # 미종결 영속 행 존재 — recover 필요
    TRIGGERED_ABORTED = "triggered_aborted"  # 발화했으나 risk/sizing 으로 안전 abort
    COMMITTED = "committed"  # ledger durable terminal 로 commit(FILLED/REJECTED)
    UNCERTAIN = "uncertain"  # 결과 불명확 — 자동 재시도 금지, operator 조정 대상


@dataclass(frozen=True, slots=True)
class CoordinatorResult:
    """process 한 건의 결과. trigger/bridge 의 typed 사유를 보존한다."""

    status: CoordinatorStatus
    trigger_status: TriggerStatus | None = None
    trigger_reason: TriggerReason | None = None
    reason_code: str | None = None
    signal: TriggerSignal | None = None
    bridge_result: BridgeResult | None = None
    order_result: OrderResult | None = None


# BridgeOutcome → CoordinatorStatus 매핑.
_BRIDGE_OUTCOME_MAP: dict[BridgeOutcome, CoordinatorStatus] = {
    BridgeOutcome.ABORTED: CoordinatorStatus.TRIGGERED_ABORTED,
    BridgeOutcome.COMMITTED: CoordinatorStatus.COMMITTED,
    BridgeOutcome.UNCERTAIN: CoordinatorStatus.UNCERTAIN,
    BridgeOutcome.RECONCILE_REQUIRED: CoordinatorStatus.RECONCILE_REQUIRED,
    BridgeOutcome.SKIPPED_TERMINAL: CoordinatorStatus.SKIPPED_TERMINAL,
}


class PaperExecutionCoordinator:
    """TriggerEngine 발화 → TriggerOrderBridge dispatch 를 조정하는 paper-only 라이브러리 API.

    engine 은 발화 상태기계를 in-memory 로 보유하므로 동일 인스턴스를 per-tick 으로 재사용한다.
    영속 멱등성/재시작 복구는 bridge 가 호출하는 SQLite 저널/원장이 책임진다(engine 이 재시작으로
    상태를 잃어도 journal/ledger 가 중복 실행을 막는다).

    canonical 포트폴리오 컨텍스트는 주입된 PaperPortfolioContextService 가 ledger truth +
    최신 스냅샷에서 *evaluate 이전에* 계산한다. 컨텍스트/의존성 실패는 fire budget 을 소비하지
    않고 FAILED_CLOSED 로 닫힌다.
    """

    def __init__(
        self,
        *,
        engine: TriggerEngine,
        bridge: TriggerOrderBridge,
        portfolio_context_service: PaperPortfolioContextService,
    ) -> None:
        self._engine = engine
        self._bridge = bridge
        self._context_service = portfolio_context_service

    # ------------------------------------------------------------------ process
    def process(
        self,
        *,
        bundle: DecisionTriggerBundle,
        snapshot: LatestMarketStateSnapshot,
        permission: TradingPermission | None,
        allocator_decision: AllocatorDecision,
        portfolio_policy: PaperPortfolioPolicy,
        now: datetime,
        indicators: IndicatorContext | None = None,
    ) -> CoordinatorResult:
        """단일 tick 평가→(발화 시) 주문 조립→dispatch. 비발화/차단은 journal/broker 미호출."""
        require_timezone_aware_datetime(now, field_name="now")

        # 1) as-of 정합(broker 호출 전 fail-closed). engine 도 STALE_SNAPSHOT/INDICATOR_
        #    CONTEXT_STALE 로 막지만, coordinator 가 명시적으로 먼저 차단해 진단을 분리한다.
        if snapshot.evaluated_at != now:
            return self._failed(REASON_SNAPSHOT_AS_OF_MISMATCH)
        if indicators is not None and indicators.evaluated_at != now:
            return self._failed(REASON_INDICATOR_AS_OF_MISMATCH)

        # 2) plan 이 없으면 HOLD 다 — 컨텍스트를 만들지 않고 replace+evaluate 만 수행한다.
        #    evaluate 는 HOLD 를 항상 비발화(SUPPRESSED)로 처리하므로 fire 소비가 없다.
        plan = bundle.plan
        market_price: MarketPrice | None = None
        valuation: PaperPortfolioValuation | None = None
        if plan is not None:
            # 2a) snapshot 식별자 정합(plan↔decision 정합은 bundle 검증기가 이미 강제).
            if snapshot.market != plan.market or snapshot.symbol != plan.symbol:
                return self._failed(REASON_SNAPSHOT_IDENTITY_MISMATCH)

            # 3) 실행 MarketPrice 를 발화 전에 구성한다(BUY=ask, SELL=bid). 임의 fallback 금지.
            #    quote 가 없으면 실행 차단(engine 이 보통 먼저 막지만 방어적으로 한 번 더).
            quote = snapshot.quote
            if quote is None:
                return self._failed(REASON_QUOTE_UNAVAILABLE)
            price = quote.ask_price if plan.action is AnalysisAction.BUY else quote.bid_price
            market_price = MarketPrice(
                symbol=snapshot.symbol,
                market=snapshot.market,
                currency=quote.currency,
                price=price,
                as_of=quote.quote_at,
            )

            # 4) canonical 컨텍스트를 발화 전에 계산한다(ledger + 최신 스냅샷). 모든 의존성 조회를
            #    여기서 끝내므로, 실패해도 evaluate 의 fire budget 을 소비하지 않는다.
            try:
                valuation = self._context_service.build_context(
                    symbol=plan.symbol,
                    market=plan.market,
                    proposed_price=market_price,
                    current_snapshot=snapshot,
                    policy=portfolio_policy,
                    now=now,
                )
            except PaperPortfolioContextError as exc:
                return self._failed(exc.reason_code)
            except Exception:
                return self._failed(REASON_PORTFOLIO_CONTEXT_BUILD_ERROR)

            # 5) RiskFilterInput 구조 검증을 발화 전에 1회 수행한다(correlation_id 는 아직 없음).
            #    스키마/타입 위반을 fire 소비 전에 닫는다. 실제 dispatch 용 input 은 발화 후 재구성.
            try:
                RiskFilterInput(
                    allocator_decision=allocator_decision,
                    analysis_decision=bundle.decision,
                    context=valuation.risk_filter_context,
                    correlation_id=None,
                )
            except Exception:
                return self._failed(REASON_RISK_INPUT_BUILD_ERROR)

        # 6) engine active bundle 동기화(idempotent). 동일 결정이면 UNCHANGED(상태 보존),
        #    더 새로운 결정이면 REPLACED(re-arm). 더 오래됐거나 충돌이면 fail-closed.
        replace = self._engine.replace_bundle(bundle, now=now)
        if replace.status is ReplaceStatus.REJECTED_CONFLICT:
            return self._failed(REASON_DECISION_REPLACE_CONFLICT)
        if replace.status is ReplaceStatus.REJECTED_OLDER:
            return self._failed(REASON_DECISION_REPLACE_OLDER)

        # 7) 결정론적 평가. 비발화는 예외가 아니라 typed SUPPRESSED 로 반환(journal/broker 0).
        evaluation = self._engine.evaluate(
            snapshot, permission, now=now, indicators=indicators
        )
        if evaluation.status is not TriggerStatus.TRIGGERED or evaluation.signal is None:
            return CoordinatorResult(
                status=CoordinatorStatus.SUPPRESSED,
                trigger_status=evaluation.status,
                trigger_reason=evaluation.reason,
            )
        signal = evaluation.signal

        # 발화했는데 plan/valuation 이 없다면 계약 위반이다(HOLD 는 발화하지 않는다). fail-closed.
        if valuation is None or market_price is None:
            return self._failed(
                REASON_PORTFOLIO_CONTEXT_BUILD_ERROR,
                signal=signal,
                trigger_status=evaluation.status,
            )

        # 8) 발화 idempotency_key 로 RiskFilterInput 을 재구성한다(correlation 추적).
        try:
            risk_input = RiskFilterInput(
                allocator_decision=allocator_decision,
                analysis_decision=bundle.decision,
                context=valuation.risk_filter_context,
                correlation_id=signal.idempotency_key,
            )
        except Exception:
            return self._failed(
                REASON_RISK_INPUT_BUILD_ERROR,
                signal=signal,
                trigger_status=evaluation.status,
            )

        # 9) 발화→주문 경계로 위임. 현재 포지션 수량은 canonical valuation 의 ledger truth 만
        #    전달한다(Decimal, 0 포함 — None/외부 scalar 금지).
        bridge_result = self._bridge.dispatch(
            signal=signal,
            bundle=bundle,
            risk_input=risk_input,
            market_price=market_price,
            current_position_quantity=valuation.position_quantity,
            now=now,
        )
        return self._from_bridge(signal, evaluation.status, bridge_result)

    # ------------------------------------------------------------------ recovery
    def recover(self, *, now: datetime) -> tuple[BridgeResult, ...]:
        """명시적 재시작 복구 API. 미종결 영속 행을 bridge 정책으로 종결한다(자동 재주문 없음).

        constructor side effect 가 아니라 명시 호출이어야 한다(DB 자동 변경 금지). RESERVED→
        ABORTED, DISPATCHING+durable terminal→COMMITTED, 그 외 DISPATCHING→UNCERTAIN.
        """
        require_timezone_aware_datetime(now, field_name="now")
        return self._bridge.reconcile_all(now=now)

    # ------------------------------------------------------------------ internals
    def _failed(
        self,
        reason_code: str,
        *,
        signal: TriggerSignal | None = None,
        trigger_status: TriggerStatus | None = None,
    ) -> CoordinatorResult:
        return CoordinatorResult(
            status=CoordinatorStatus.FAILED_CLOSED,
            trigger_status=trigger_status,
            reason_code=reason_code,
            signal=signal,
        )

    def _from_bridge(
        self,
        signal: TriggerSignal,
        trigger_status: TriggerStatus,
        bridge_result: BridgeResult,
    ) -> CoordinatorResult:
        status = _BRIDGE_OUTCOME_MAP[bridge_result.outcome]
        return CoordinatorResult(
            status=status,
            trigger_status=trigger_status,
            reason_code=bridge_result.reason_code,
            signal=signal,
            bridge_result=bridge_result,
            order_result=bridge_result.order_result,
        )


__all__ = [
    "CoordinatorResult",
    "CoordinatorStatus",
    "PaperExecutionCoordinator",
    "REASON_DECISION_REPLACE_CONFLICT",
    "REASON_DECISION_REPLACE_OLDER",
    "REASON_INDICATOR_AS_OF_MISMATCH",
    "REASON_PORTFOLIO_CONTEXT_BUILD_ERROR",
    "REASON_QUOTE_UNAVAILABLE",
    "REASON_RISK_INPUT_BUILD_ERROR",
    "REASON_SNAPSHOT_AS_OF_MISMATCH",
    "REASON_SNAPSHOT_IDENTITY_MISMATCH",
]
