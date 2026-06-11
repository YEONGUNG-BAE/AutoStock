"""F1b: 발화(fire)→주문(order) 경계 bridge.

F1a journal(영속 발화 저널)을 OrderIntentGenerator / QuantityResolver / broker /
ledger 와 묶어, 하나의 발화 신호(TriggerSignal)를 단발(idempotency_key 단위) 주문으로
변환한다. 이 모듈은 journal/engine/broker/ledger/risk/paper_loop 를 *호출*만 하며,
그 어떤 것도 수정하지 않는다.

핵심 불변식(GPT review v2 lock):
1. QuantityResolver 포함 — PaperBroker 는 target_weight_percent intent 를 거절하므로
   generate→submit 사이에 sizing 단계가 반드시 들어간다.
2. 입력에 TriggerPlan/DecisionTriggerBundle(plan+analysis)을 받는다 — TriggerSignal 만으로는
   max_fires_per_decision 과 plan↔analysis 정합을 확인할 수 없다.
3. RESERVED/DISPATCHING 영속 행의 재시작 자동 재개는 정상 경로에 없다 — reconcile 전용.
4. COMMITTED 판단은 broker 반환값이 아니라 ledger 의 durable order result 를 source of truth
   로 삼는다(broker 는 duplicate order_id 에 대해 row 없이 in-memory REJECTED 를 돌려준다).

상태 흐름(정상 경로):
    preflight → reserve(RESERVED) → generate → resolve(quantity)
        → ledger preflight(desync 방어) → mark_dispatching(DISPATCHING)
        → broker.submit_order → ledger truth → mark_committed | mark_uncertain

fail-closed 원칙: 의심스러우면 주문을 중복 실행하기보다 실행을 건너뛰고 UNCERTAIN 으로 멈춘다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from domain.enums import AccountRole, OrderSide, OrderStatus, OrderType
from domain.market import MarketPrice
from domain.order import OrderIntent, OrderResult
from paper_loop.models import QuantityResolutionResult, QuantityResolutionStatus
from risk.order_generation import OrderGenerationResult
from risk.models import OrderGenerationStatus

from execution.trigger_journal import (
    JournalResultStatus,
    JournalState,
    ReserveOutcome,
    TriggerFireSignal,
    TriggerJournal,
    TriggerJournalRecord,
)


# --- reason codes (journal reason_code 로 영속된다) ---
REASON_RISK_BLOCKED = "risk_blocked"
REASON_HOLD_NOOP = "hold_noop"
REASON_SIZING_FAILED = "sizing_failed"
REASON_NO_EXECUTABLE_QUANTITY = "no_executable_quantity"
REASON_COHERENCE_FAILED = "coherence_failed"
REASON_GENERATION_EXCEPTION = "generation_exception"
REASON_SIZING_EXCEPTION = "sizing_exception"
REASON_LEDGER_PREFLIGHT_EXCEPTION = "ledger_preflight_exception"
REASON_BROKER_EXCEPTION = "broker_exception"
REASON_DISPATCH_OUTCOME_MISSING = "dispatch_outcome_missing"
REASON_DISPATCH_OUTCOME_NONTERMINAL = "dispatch_outcome_nonterminal"
REASON_RESTART_BEFORE_DISPATCH = "restart_before_dispatch"
REASON_DISPATCH_OUTCOME_UNKNOWN = "dispatch_outcome_unknown"


# AnalysisAction(value) → 기대 OrderSide. signal.action 에서 파생되는 side 가 실제 실행 intent 의
# side 와 일치하는지 broker 호출 전에 검증하기 위한 매핑.
_ACTION_TO_SIDE: dict[str, OrderSide] = {
    "buy": OrderSide.BUY,
    "sell": OrderSide.SELL,
}


# OrderStatus → JournalResultStatus. 매핑에 없는 상태(PENDING/CANCELLED)는 terminal commit
# 대상이 아니며 UNCERTAIN 으로 처리한다(v1 paper 는 MARKET 만 생성하므로 정상적으로는 도달 X).
_TERMINAL_RESULT_MAP: dict[OrderStatus, JournalResultStatus] = {
    OrderStatus.FILLED: JournalResultStatus.FILLED,
    OrderStatus.REJECTED: JournalResultStatus.REJECTED,
}


class BridgeError(Exception):
    """bridge 공통 에러 base."""


class BridgePreflightError(BridgeError):
    """static preflight 실패 — journal/broker 를 호출하기 전에 차단된다(아무것도 reserve 안 됨)."""


class BridgeCoherenceError(BridgeError):
    """resolve 이후 실행 intent 가 기대 불변식을 어김(내부 논리 오류).

    reserve 행은 ABORTED 로 안전 종료한 뒤 raise 한다(주문은 전송되지 않았다).
    """


class BridgeDependencyError(BridgeError):
    """dispatch 전(generate/resolve/ledger preflight) 의존성 호출이 예외를 던짐.

    주문은 전송되지 않았으므로 reserve 행을 ABORTED 로 안전 종료한 뒤 raise 한다(고아 RESERVED 방지).
    """


class BridgeOutcome(StrEnum):
    """bridge dispatch/reconcile 결과 분류."""

    SKIPPED_TERMINAL = "skipped_terminal"
    RECONCILE_REQUIRED = "reconcile_required"
    ABORTED = "aborted"
    COMMITTED = "committed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class BridgeResult:
    """dispatch/reconcile 한 건의 결과."""

    outcome: BridgeOutcome
    record: TriggerJournalRecord
    reason_code: str | None = None
    order_result: OrderResult | None = None


@runtime_checkable
class FirePlan(Protocol):
    """발화 plan 의 최소 구조(market_data.TriggerPlan 이 구조적으로 만족)."""

    plan_id: str
    decision_id: object
    market: object
    symbol: str
    action: object
    max_fires_per_decision: int


@runtime_checkable
class FireDecision(Protocol):
    """발화 analysis decision 의 최소 구조(analysis.AnalysisDecision 이 만족)."""

    decision_id: object
    market: object
    symbol: str


@runtime_checkable
class FireBundle(Protocol):
    """DecisionTriggerBundle 의 최소 구조(plan+decision+action)."""

    plan: FirePlan | None
    decision: FireDecision

    @property
    def action(self) -> object: ...


class FireGenerator(Protocol):
    """OrderIntentGenerator 계약."""

    def generate(self, risk_input: object) -> OrderGenerationResult: ...


class FireResolver(Protocol):
    """QuantityResolver 계약."""

    def resolve(
        self,
        *,
        intent: OrderIntent,
        context: object,
        market_price: MarketPrice,
        current_position_quantity: Decimal | None = None,
    ) -> QuantityResolutionResult: ...


class FireBroker(Protocol):
    """broker 실행 계약(PaperBrokerAdapter.submit_order)."""

    def submit_order(self, intent: OrderIntent, market_price: MarketPrice) -> OrderResult: ...


class FireLedger(Protocol):
    """ledger durable 조회 계약(source of truth)."""

    def has_processed_order(self, order_id: str) -> bool: ...

    def get_order_result(self, order_id: str) -> OrderResult | None: ...


@runtime_checkable
class FireRiskInput(Protocol):
    """OrderIntentGenerator.generate 입력의 최소 구조(risk.models.RiskFilterInput)."""

    analysis_decision: FireDecision
    context: object
    correlation_id: str | None


def _scalar(value: object) -> str:
    """DecisionId/Market/AnalysisAction(.value) 또는 plain str 을 비교용 문자열로 정규화한다."""
    inner = getattr(value, "value", value)
    return str(inner)


class TriggerOrderBridge:
    """발화 신호를 단발 주문으로 변환하는 orchestrator."""

    def __init__(
        self,
        *,
        journal: TriggerJournal,
        generator: FireGenerator,
        resolver: FireResolver,
        broker: FireBroker,
        ledger: FireLedger,
    ) -> None:
        self._journal = journal
        self._generator = generator
        self._resolver = resolver
        self._broker = broker
        self._ledger = ledger

    # ------------------------------------------------------------------ dispatch
    def dispatch(
        self,
        *,
        signal: TriggerFireSignal,
        bundle: FireBundle,
        risk_input: FireRiskInput,
        market_price: MarketPrice,
        current_position_quantity: Decimal | None,
        now: datetime,
    ) -> BridgeResult:
        """하나의 발화 신호를 reserve→generate→resolve→dispatch→commit 으로 처리한다."""
        self._preflight(
            signal=signal, bundle=bundle, risk_input=risk_input, market_price=market_price
        )

        # 1) reserve — idempotency_key 단위 단발 선점.
        reserve = self._journal.reserve(signal, now)
        if reserve.outcome is ReserveOutcome.EXISTING_TERMINAL:
            # 이미 종결된 발화: 재처리 skip.
            return BridgeResult(BridgeOutcome.SKIPPED_TERMINAL, reserve.record)
        if reserve.outcome is ReserveOutcome.EXISTING_PENDING:
            # 미종결 행이 남아 있다: 정상 경로 자동 재개 금지, reconcile 대상.
            return BridgeResult(BridgeOutcome.RECONCILE_REQUIRED, reserve.record)

        key = signal.idempotency_key
        order_id = f"order-{_scalar(signal.decision_id)}"

        # 2) order intent 생성(RiskFilter 평가 포함). 예외는 dispatch 전이므로 ABORTED 안전 종료.
        try:
            gen = self._generator.generate(risk_input)
        except Exception as exc:  # noqa: BLE001 — 고아 RESERVED 방지: 안전 abort 후 surfacing
            self._journal.mark_aborted(key, REASON_GENERATION_EXCEPTION, now)
            raise BridgeDependencyError(
                f"OrderIntentGenerator.generate raised: {exc!r}"
            ) from exc
        if gen.status is OrderGenerationStatus.BLOCKED:
            return self._abort(key, REASON_RISK_BLOCKED, now)
        if gen.status is OrderGenerationStatus.NOOP:
            return self._abort(key, REASON_HOLD_NOOP, now)
        if gen.order_intent is None:  # GENERATED 인데 intent 없음 — 방어
            self._journal.mark_aborted(key, REASON_COHERENCE_FAILED, now)
            raise BridgeCoherenceError("GENERATED status without order_intent.")

        # 3) target_weight → quantity sizing. PaperBroker 는 target_weight intent 를 거절한다.
        try:
            resolution = self._resolver.resolve(
                intent=gen.order_intent,
                context=risk_input.context,
                market_price=market_price,
                current_position_quantity=current_position_quantity,
            )
        except Exception as exc:  # noqa: BLE001 — 고아 RESERVED 방지: 안전 abort 후 surfacing
            self._journal.mark_aborted(key, REASON_SIZING_EXCEPTION, now)
            raise BridgeDependencyError(
                f"QuantityResolver.resolve raised: {exc!r}"
            ) from exc
        if resolution.status is QuantityResolutionStatus.FAILED:
            return self._abort(key, REASON_SIZING_FAILED, now)
        if resolution.status is QuantityResolutionStatus.NOOP:
            return self._abort(key, REASON_NO_EXECUTABLE_QUANTITY, now)

        executable = resolution.order_intent
        # 4) 실행 intent 불변식 검증(order_id/correlation/quantity + symbol/market/side/source/type/account).
        self._assert_executable_coherence(
            executable, signal=signal, key=key, order_id=order_id, now=now
        )
        assert executable is not None  # _assert_executable_coherence 가 보장

        # 5) ledger preflight(desync 방어): 같은 order_id 의 durable result 가 이미 있으면
        #    broker 를 호출하지 않고 그 durable 상태로 종결한다. 예외는 dispatch 전 → ABORTED.
        try:
            durable_pre = self._ledger.get_order_result(order_id)
        except Exception as exc:  # noqa: BLE001 — 고아 RESERVED 방지: 안전 abort 후 surfacing
            self._journal.mark_aborted(key, REASON_LEDGER_PREFLIGHT_EXCEPTION, now)
            raise BridgeDependencyError(
                f"ledger.get_order_result (preflight) raised: {exc!r}"
            ) from exc
        if durable_pre is not None:
            self._journal.mark_dispatching(key, order_id, now)
            return self._finalize_from_durable(key, durable_pre, now)

        # 6) DISPATCHING 영속화(order_id 전역 UNIQUE 점유) — broker 호출 직전.
        self._journal.mark_dispatching(key, order_id, now)

        # 7) broker 제출. 예외가 나도 ledger 에 durable 이 commit 됐을 수 있으므로(반환 직전 예외)
        #    ledger 를 다시 읽어 진실원천으로 삼는다 — 그래야 'broker 반환값 불신' 원칙과 일관된다.
        try:
            self._broker.submit_order(executable, market_price)
        except Exception:  # noqa: BLE001 — 결과 불명확은 ledger 재조회 후 fail-closed
            return self._reconcile_after_broker_exception(key, order_id, now)

        # 8) ledger 가 source of truth. broker 반환값을 신뢰하지 않는다.
        durable_post = self._ledger.get_order_result(order_id)
        return self._finalize_from_durable(key, durable_post, now)

    # ------------------------------------------------------------------ reconcile
    def reconcile_record(
        self, record: TriggerJournalRecord, *, now: datetime
    ) -> BridgeResult:
        """재시작 복원: 미종결 행 1건을 안전하게 종결한다(자동 재실행/재제출 없음).

        - RESERVED: 주문 미전송 확정 → ABORTED.
        - DISPATCHING: ledger 에 order 가 기록됐으면 durable 상태로 COMMIT, 아니면 UNCERTAIN.
        """
        key = record.idempotency_key
        if record.state is JournalState.RESERVED:
            aborted = self._journal.mark_aborted(key, REASON_RESTART_BEFORE_DISPATCH, now)
            return BridgeResult(
                BridgeOutcome.ABORTED, aborted, REASON_RESTART_BEFORE_DISPATCH, None
            )
        if record.state is JournalState.DISPATCHING:
            order_id = record.order_id
            if order_id is not None and self._ledger.has_processed_order(order_id):
                durable = self._ledger.get_order_result(order_id)
                result_status = (
                    _TERMINAL_RESULT_MAP.get(durable.status)
                    if durable is not None
                    else None
                )
                if result_status is not None:
                    committed = self._journal.mark_committed(key, result_status, now)
                    return BridgeResult(
                        BridgeOutcome.COMMITTED, committed, None, durable
                    )
            # 결과 불명확 → 자동 재제출 금지, UNCERTAIN 정지.
            uncertain = self._journal.mark_uncertain(
                key, REASON_DISPATCH_OUTCOME_UNKNOWN, now
            )
            return BridgeResult(
                BridgeOutcome.UNCERTAIN, uncertain, REASON_DISPATCH_OUTCOME_UNKNOWN, None
            )
        raise BridgeError(
            f"reconcile_record requires a nonterminal record, got {record.state.value}."
        )

    def reconcile_all(self, *, now: datetime) -> tuple[BridgeResult, ...]:
        """list_nonterminal() 의 모든 미종결 행을 reconcile 한다."""
        return tuple(
            self.reconcile_record(record, now=now)
            for record in self._journal.list_nonterminal()
        )

    # ------------------------------------------------------------------ internals
    def _preflight(
        self,
        *,
        signal: TriggerFireSignal,
        bundle: FireBundle,
        risk_input: FireRiskInput,
        market_price: MarketPrice,
    ) -> None:
        """주문/저널 호출 전 정합성 검증. 실패 시 아무것도 reserve 하지 않고 raise(fail-closed)."""
        plan = bundle.plan
        if plan is None:
            raise BridgePreflightError(
                "Fire dispatch requires a BUY/SELL bundle with a TriggerPlan (HOLD has no fire)."
            )

        # signal ↔ plan: 모든 식별 필드 일치.
        if signal.plan_id != plan.plan_id:
            raise BridgePreflightError("signal.plan_id != plan.plan_id.")
        if _scalar(signal.decision_id) != _scalar(plan.decision_id):
            raise BridgePreflightError("signal.decision_id != plan.decision_id.")
        if _scalar(signal.market) != _scalar(plan.market):
            raise BridgePreflightError("signal.market != plan.market.")
        if signal.symbol != plan.symbol:
            raise BridgePreflightError("signal.symbol != plan.symbol.")
        if _scalar(signal.action) != _scalar(plan.action):
            raise BridgePreflightError("signal.action != plan.action.")

        # signal ↔ analysis decision.
        decision = bundle.decision
        if _scalar(signal.decision_id) != _scalar(decision.decision_id):
            raise BridgePreflightError("signal.decision_id != decision.decision_id.")
        if _scalar(signal.market) != _scalar(decision.market):
            raise BridgePreflightError("signal.market != decision.market.")
        if signal.symbol != decision.symbol:
            raise BridgePreflightError("signal.symbol != decision.symbol.")
        if _scalar(signal.action) != _scalar(bundle.action):
            raise BridgePreflightError("signal.action != bundle.action.")

        # max_fires_per_decision == 1 — engine in-memory budget 만으로는 재시작을 못 넘기므로
        # bridge 경계에서 단발을 강제한다.
        if plan.max_fires_per_decision != 1:
            raise BridgePreflightError(
                "plan.max_fires_per_decision must be 1 at the fire→order bridge."
            )

        # correlation_id == idempotency_key — generate() 가 None 일 때 decision_id 로 fallback
        # 하므로, 실행 intent.correlation_id 를 idempotency_key 로 보장하려면 명시적으로 일치해야 한다.
        if risk_input.correlation_id != signal.idempotency_key:
            raise BridgePreflightError(
                "risk_input.correlation_id must equal signal.idempotency_key."
            )

        # 주문은 bundle.decision 이 아니라 risk_input.analysis_decision 에서 파생되므로
        # (symbol/market/side/target weight 전부) 그 둘이 동일해야 한다. decision_id 만 같고
        # symbol/action 이 다른 risk_input 이 들어오면 엉뚱한 종목·방향 주문이 나갈 수 있다.
        if risk_input.analysis_decision != decision:
            raise BridgePreflightError(
                "risk_input.analysis_decision must equal bundle.decision "
                "(order identity is derived from risk_input)."
            )

        # market_price 도 발화 신호와 같은 종목/시장이어야 한다(PaperBroker 는 intent↔price 만 보고
        # signal 과의 일치는 보지 않으므로, 여기서 발화 신호 기준으로 한 번 더 잠근다).
        if _scalar(market_price.market) != _scalar(signal.market):
            raise BridgePreflightError("market_price.market != signal.market.")
        if market_price.symbol != signal.symbol:
            raise BridgePreflightError("market_price.symbol != signal.symbol.")

    def _assert_executable_coherence(
        self,
        executable: OrderIntent | None,
        *,
        signal: TriggerFireSignal,
        key: str,
        order_id: str,
        now: datetime,
    ) -> None:
        problems: list[str] = []
        if executable is None:
            problems.append("RESOLVED status without order_intent")
        else:
            if executable.order_id != order_id:
                problems.append(
                    f"order_id {executable.order_id!r} != expected {order_id!r}"
                )
            if executable.correlation_id != key:
                problems.append(
                    f"correlation_id {executable.correlation_id!r} != idempotency_key {key!r}"
                )
            if executable.quantity is None:
                problems.append("executable intent missing quantity")
            if executable.target_weight_percent is not None:
                problems.append("executable intent still carries target_weight_percent")
            # 종목/시장/방향/원천 decision 이 발화 신호와 일치하는지 broker 호출 전 강제.
            if executable.symbol != signal.symbol:
                problems.append(
                    f"symbol {executable.symbol!r} != signal.symbol {signal.symbol!r}"
                )
            if _scalar(executable.market) != _scalar(signal.market):
                problems.append(
                    f"market {_scalar(executable.market)!r} != signal.market {_scalar(signal.market)!r}"
                )
            expected_side = _ACTION_TO_SIDE.get(_scalar(signal.action))
            if expected_side is None:
                problems.append(f"unmappable signal.action {_scalar(signal.action)!r}")
            elif executable.side != expected_side:
                problems.append(
                    f"side {executable.side.value!r} != expected {expected_side.value!r} "
                    f"for action {_scalar(signal.action)!r}"
                )
            if executable.source_decision_id != _scalar(signal.decision_id):
                problems.append(
                    f"source_decision_id {executable.source_decision_id!r} "
                    f"!= signal.decision_id {_scalar(signal.decision_id)!r}"
                )
            if executable.order_type != OrderType.MARKET:
                problems.append(
                    f"order_type {executable.order_type.value!r} != MARKET (v1 paper)"
                )
            if executable.account_role != AccountRole.PAPER:
                problems.append(
                    f"account_role {executable.account_role.value!r} != PAPER (v1 paper lane)"
                )
        if problems:
            # 주문 미전송 상태이므로 ABORTED 안전 종료 후 raise(버그 surfacing).
            self._journal.mark_aborted(key, REASON_COHERENCE_FAILED, now)
            raise BridgeCoherenceError("; ".join(problems))

    def _reconcile_after_broker_exception(
        self, key: str, order_id: str, now: datetime
    ) -> BridgeResult:
        """broker.submit_order 예외 후 ledger 를 진실원천으로 재조회한다.

        broker 가 ledger commit 직후 반환 과정에서 예외를 던질 수 있으므로, ledger 에 durable
        terminal 결과가 있으면 그것으로 COMMIT 한다. 없거나(또는 재조회 자체가 실패하면)
        nonterminal 이면 UNCERTAIN 으로 멈춘다(자동 재제출 금지).
        """
        durable: OrderResult | None
        try:
            durable = self._ledger.get_order_result(order_id)
        except Exception:  # noqa: BLE001 — ledger 재조회 실패도 결과 불명확으로 처리
            durable = None
        result_status = (
            _TERMINAL_RESULT_MAP.get(durable.status) if durable is not None else None
        )
        if result_status is not None:
            record = self._journal.mark_committed(key, result_status, now)
            return BridgeResult(BridgeOutcome.COMMITTED, record, None, durable)
        record = self._journal.mark_uncertain(key, REASON_BROKER_EXCEPTION, now)
        return BridgeResult(BridgeOutcome.UNCERTAIN, record, REASON_BROKER_EXCEPTION, None)

    def _abort(self, key: str, reason_code: str, now: datetime) -> BridgeResult:
        record = self._journal.mark_aborted(key, reason_code, now)
        return BridgeResult(BridgeOutcome.ABORTED, record, reason_code, None)

    def _finalize_from_durable(
        self, key: str, durable: OrderResult | None, now: datetime
    ) -> BridgeResult:
        """ledger durable result 를 기준으로 DISPATCHING 행을 종결한다."""
        if durable is None:
            record = self._journal.mark_uncertain(key, REASON_DISPATCH_OUTCOME_MISSING, now)
            return BridgeResult(
                BridgeOutcome.UNCERTAIN, record, REASON_DISPATCH_OUTCOME_MISSING, None
            )
        result_status = _TERMINAL_RESULT_MAP.get(durable.status)
        if result_status is None:
            record = self._journal.mark_uncertain(
                key, REASON_DISPATCH_OUTCOME_NONTERMINAL, now
            )
            return BridgeResult(
                BridgeOutcome.UNCERTAIN,
                record,
                REASON_DISPATCH_OUTCOME_NONTERMINAL,
                durable,
            )
        record = self._journal.mark_committed(key, result_status, now)
        return BridgeResult(BridgeOutcome.COMMITTED, record, None, durable)
