"""RTM-5: paper-only 실행 조정 계층(execution coordinator).

TriggerEngine 의 발화 결과를 현재 paper 포트폴리오 컨텍스트와 안전하게 조립하여
TriggerOrderBridge.dispatch 까지 연결하는 *명시적 라이브러리 API* 다. 이 모듈은
engine/bridge/broker(position 조회)를 *호출*만 하며 그 어떤 것도 수정하지 않는다.

RTM-5 경계(중요):
- 이 coordinator 는 라이브러리 callable 일 뿐, scheduler/daemon/monitor 에 자동 연결되지 않는다.
  무인 per-tick 연결은 RTM-7 범위다.
- 실제 KIS/live order adapter 를 사용하지 않으며 network/LLM 을 호출하지 않는다.
- 실제 runtime/paper DB 경로를 하드코딩하지 않는다(의존성 주입).

fail-closed 설계: 의심스러우면 주문을 내기보다 멈춘다. coordinator 단계의 식별자/as-of/
포지션 정합 위반은 engine/journal/broker 를 호출하기 *전에* FAILED_CLOSED 로 반환한다.

조립 흐름(process):
    as-of/identity preflight → engine.replace_bundle(idempotent) → engine.evaluate
        → (비발화면 SUPPRESSED 반환)
        → ledger/broker 에서 현재 PAPER 포지션 수량 조회(외부 scalar 주입 금지)
        → 동일 frozen snapshot 의 quote 로 MarketPrice 생성(BUY=ask, SELL=bid)
        → RiskFilterInput 신규 구성(analysis_decision=bundle.decision,
          correlation_id=signal.idempotency_key)
        → bridge.dispatch → typed CoordinatorResult
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from analysis.models import AnalysisAction
from allocator.models import AllocatorDecision
from domain._datetime import require_timezone_aware_datetime
from domain.enums import AccountRole, Market
from domain.market import MarketPrice
from domain.order import OrderResult
from domain.position import Position
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
from risk.models import RiskFilterContext, RiskFilterInput

from execution.trigger_order_bridge import (
    BridgeOutcome,
    BridgeResult,
    TriggerOrderBridge,
)


# --- coordinator-local fail-closed reason codes (broker 호출 전 차단) ---
REASON_SNAPSHOT_AS_OF_MISMATCH = "snapshot_as_of_mismatch"
REASON_INDICATOR_AS_OF_MISMATCH = "indicator_as_of_mismatch"
REASON_SNAPSHOT_IDENTITY_MISMATCH = "snapshot_identity_mismatch"
REASON_CONTEXT_MARKET_MISMATCH = "context_market_mismatch"
REASON_DECISION_REPLACE_CONFLICT = "decision_replace_conflict"
REASON_DECISION_REPLACE_OLDER = "decision_replace_older"
REASON_POSITION_IDENTITY_MISMATCH = "position_identity_mismatch"
REASON_QUOTE_UNAVAILABLE = "quote_unavailable"
REASON_CONTEXT_AS_OF_MISMATCH = "context_as_of_mismatch"
REASON_CONTEXT_POSITION_VALUE_MISMATCH = "context_position_value_mismatch"
REASON_POSITION_SOURCE_ERROR = "position_source_error"
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


@runtime_checkable
class PositionSource(Protocol):
    """현재 PAPER 포지션 조회 계약(PaperBrokerAdapter / SQLiteLedger 가 만족)."""

    def get_position(
        self, symbol: str, market: Market, account_role: AccountRole
    ) -> Position | None: ...


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
    """

    def __init__(
        self,
        *,
        engine: TriggerEngine,
        bridge: TriggerOrderBridge,
        position_source: PositionSource,
    ) -> None:
        self._engine = engine
        self._bridge = bridge
        self._position_source = position_source

    # ------------------------------------------------------------------ process
    def process(
        self,
        *,
        bundle: DecisionTriggerBundle,
        snapshot: LatestMarketStateSnapshot,
        permission: TradingPermission | None,
        allocator_decision: AllocatorDecision,
        risk_context: RiskFilterContext,
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

        # 2) snapshot / context 식별자 정합(plan 이 있을 때만 — HOLD 는 engine 이 SUPPRESS).
        #    bundle 내부(plan↔decision) 정합은 DecisionTriggerBundle 검증기가 이미 강제한다.
        plan = bundle.plan
        if plan is not None:
            if snapshot.market != plan.market or snapshot.symbol != plan.symbol:
                return self._failed(REASON_SNAPSHOT_IDENTITY_MISMATCH)
            if risk_context.market is not None and risk_context.market != plan.market:
                return self._failed(REASON_CONTEXT_MARKET_MISMATCH)

        # 3) engine active bundle 동기화(idempotent). 동일 결정이면 UNCHANGED(상태 보존),
        #    더 새로운 결정이면 REPLACED(re-arm). 더 오래됐거나 충돌이면 fail-closed.
        replace = self._engine.replace_bundle(bundle, now=now)
        if replace.status is ReplaceStatus.REJECTED_CONFLICT:
            return self._failed(REASON_DECISION_REPLACE_CONFLICT)
        if replace.status is ReplaceStatus.REJECTED_OLDER:
            return self._failed(REASON_DECISION_REPLACE_OLDER)

        # 4) 결정론적 평가. 비발화는 예외가 아니라 typed SUPPRESSED 로 반환(journal/broker 0).
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

        # 5) 동일 frozen snapshot 의 quote 로 MarketPrice 생성. BUY=ask, SELL=bid.
        #    임의 fallback(LAST trade) 금지 — quote 가 없으면 실행 차단(engine 이 보통
        #    먼저 막지만 방어적으로 한 번 더). as_of 는 사용한 quote 의 quote_at.
        #    가격은 포지션 정합 검증에도 필요하므로 ledger 조회보다 먼저 만든다.
        quote = snapshot.quote
        if quote is None:
            return self._failed(
                REASON_QUOTE_UNAVAILABLE, signal=signal, trigger_status=evaluation.status
            )
        price = quote.ask_price if signal.action is AnalysisAction.BUY else quote.bid_price
        market_price = MarketPrice(
            symbol=snapshot.symbol,
            market=snapshot.market,
            currency=quote.currency,
            price=price,
            as_of=quote.quote_at,
        )

        # 6) 현재 PAPER 포지션 수량을 ledger/broker 에서 조회한다(외부 scalar 주입 금지).
        #    조회 자체가 던지면 typed FAILED_CLOSED 로 변환(예외 전파 금지).
        #    반드시 signal 의 종목/시장/PAPER 계좌 포지션이어야 한다 — 다른 종목 수량이
        #    sizing 에 흘러들면 fail-closed. 포지션이 없으면 ledger truth 는 0 이다
        #    (None 을 흘려보내 caller context value 로 fallback 되게 두지 않는다).
        try:
            position = self._position_source.get_position(
                signal.symbol, signal.market, AccountRole.PAPER
            )
        except Exception:
            return self._failed(
                REASON_POSITION_SOURCE_ERROR,
                signal=signal,
                trigger_status=evaluation.status,
            )
        if position is not None and (
            position.symbol != signal.symbol
            or position.market != signal.market
            or position.account_role is not AccountRole.PAPER
        ):
            return self._failed(
                REASON_POSITION_IDENTITY_MISMATCH,
                signal=signal,
                trigger_status=evaluation.status,
            )
        ledger_quantity: Decimal = (
            position.quantity if position is not None else Decimal("0")
        )

        # 7) caller 가 넘긴 RiskFilterContext 가 ledger truth 와 정합하는지 강제한다.
        #    coordinator 는 단일 종목 snapshot 으로 전체 NAV/cash/invested 를 재구성할 수
        #    없으므로(추정 금지) 검증 가능한 차원만 fail-closed 로 닫는다:
        #    (a) as-of 동일성(created_at == now),
        #    (b) current_symbol_market_value == ledger_quantity × reference price
        #        (통화 일치 포함). 불일치면 stale/위조 context 이므로 실행 차단.
        if risk_context.created_at != now:
            return self._failed(
                REASON_CONTEXT_AS_OF_MISMATCH,
                signal=signal,
                trigger_status=evaluation.status,
            )
        expected_symbol_value = ledger_quantity * price
        symbol_value = risk_context.current_symbol_market_value
        if symbol_value is None:
            actual_symbol_value = Decimal("0")
        else:
            if symbol_value.currency != quote.currency:
                return self._failed(
                    REASON_CONTEXT_POSITION_VALUE_MISMATCH,
                    signal=signal,
                    trigger_status=evaluation.status,
                )
            actual_symbol_value = symbol_value.amount
        if actual_symbol_value != expected_symbol_value:
            return self._failed(
                REASON_CONTEXT_POSITION_VALUE_MISMATCH,
                signal=signal,
                trigger_status=evaluation.status,
            )

        # 8) RiskFilterInput 을 신규 구성한다(caller 가 만든 임의 risk input 을 신뢰하지 않는다).
        #    analysis_decision 은 bundle.decision, correlation_id 는 발화 idempotency_key.
        #    구성 자체가 던지면(타입/검증 위반) typed FAILED_CLOSED 로 변환.
        try:
            risk_input = RiskFilterInput(
                allocator_decision=allocator_decision,
                analysis_decision=bundle.decision,
                context=risk_context,
                correlation_id=signal.idempotency_key,
            )
        except Exception:
            return self._failed(
                REASON_RISK_INPUT_BUILD_ERROR,
                signal=signal,
                trigger_status=evaluation.status,
            )

        # 9) 발화→주문 경계로 위임. bridge 가 reserve/generate/resolve/ledger truth 를 책임진다.
        #    현재 포지션 수량은 ledger truth(Decimal, 0 포함) 만 전달한다 — None 금지.
        bridge_result = self._bridge.dispatch(
            signal=signal,
            bundle=bundle,
            risk_input=risk_input,
            market_price=market_price,
            current_position_quantity=ledger_quantity,
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
    "PositionSource",
    "REASON_CONTEXT_AS_OF_MISMATCH",
    "REASON_CONTEXT_MARKET_MISMATCH",
    "REASON_CONTEXT_POSITION_VALUE_MISMATCH",
    "REASON_DECISION_REPLACE_CONFLICT",
    "REASON_DECISION_REPLACE_OLDER",
    "REASON_INDICATOR_AS_OF_MISMATCH",
    "REASON_POSITION_IDENTITY_MISMATCH",
    "REASON_POSITION_SOURCE_ERROR",
    "REASON_QUOTE_UNAVAILABLE",
    "REASON_RISK_INPUT_BUILD_ERROR",
    "REASON_SNAPSHOT_AS_OF_MISMATCH",
    "REASON_SNAPSHOT_IDENTITY_MISMATCH",
]
