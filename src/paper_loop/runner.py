from __future__ import annotations

from decimal import Decimal

from allocator.models import ALLOCATOR_DECISION_SCHEMA
from allocator.validator import ALLOCATOR_VALIDATOR_VERSION
from analysis.models import ANALYSIS_DECISION_SCHEMA
from analysis.validator import ANALYSIS_VALIDATOR_VERSION
from broker.paper_broker import PaperBrokerAdapter
from decision.sqlite_decision_store import DuplicateDecisionIdError, SQLiteDecisionStore
from domain.decision import DecisionSnapshot
from domain.enums import AccountRole, Currency, OrderStatus
from domain.identifiers import DecisionId
from domain.portfolio import NavSnapshot
from ledger.sqlite_ledger import SQLiteLedger
from risk.models import RiskFilterInput
from risk.order_generation import OrderGenerationStatus, OrderIntentGenerator
from risk.filter import RiskFilter

from paper_loop.models import (
    PAPER_LOOP_DUPLICATE_SNAPSHOT,
    PaperLoopInput,
    PaperLoopResult,
    PaperLoopStatus,
    QuantityResolutionStatus,
    build_paper_loop_snapshot,
    failed_validation_result,
    passed_validation_result,
)
from paper_loop.quantity_resolver import QuantityResolver


class PaperLoopRunner:
    """검증된 decision bundle을 paper broker에 연결하는 E2E runner."""

    def __init__(
        self,
        *,
        ledger: SQLiteLedger,
        decision_store: SQLiteDecisionStore,
        broker: PaperBrokerAdapter | None = None,
        risk_filter: RiskFilter | None = None,
        order_generator: OrderIntentGenerator | None = None,
        quantity_resolver: QuantityResolver | None = None,
    ) -> None:
        self._ledger = ledger
        self._decision_store = decision_store
        self._broker = broker or PaperBrokerAdapter(ledger)
        self._order_generator = order_generator or OrderIntentGenerator(
            risk_filter=risk_filter or RiskFilter()
        )
        self._quantity_resolver = quantity_resolver or QuantityResolver()

    def run(self, loop_input: PaperLoopInput) -> PaperLoopResult:
        """validated decision bundle → RiskFilter → OrderIntent → quantity → PaperBroker."""
        correlation_id = (
            loop_input.correlation_id
            or loop_input.analysis_decision.decision_id.value
        )

        # run_id duplicate는 write/broker 호출 전에 fail-closed 한다.
        if self._decision_store.get_decision_snapshot(loop_input.normalized_run_id) is not None:
            return _validation_failed_result(
                loop_input=loop_input,
                correlation_id=correlation_id,
                message=(
                    f"decision_id already exists: {loop_input.normalized_run_id.value}"
                ),
                code=PAPER_LOOP_DUPLICATE_SNAPSHOT,
                snapshot_ids=(),
            )

        risk_input = RiskFilterInput(
            allocator_decision=loop_input.allocator_decision,
            analysis_decision=loop_input.analysis_decision,
            context=loop_input.risk_context,
            correlation_id=correlation_id,
        )

        snapshot_ids: list[DecisionId] = []
        try:
            snapshot_ids.extend(
                _save_input_decision_snapshots(
                    self._decision_store,
                    loop_input=loop_input,
                )
            )
        except DuplicateDecisionIdError as exc:
            return _validation_failed_result(
                loop_input=loop_input,
                correlation_id=correlation_id,
                message=str(exc),
                code=PAPER_LOOP_DUPLICATE_SNAPSHOT,
                snapshot_ids=tuple(snapshot_ids),
            )

        generation = self._order_generator.generate(risk_input)
        risk_result = generation.validation_result
        generated_intent = generation.order_intent

        if generation.status == OrderGenerationStatus.BLOCKED:
            return _finalize_loop_result(
                self._decision_store,
                loop_input=loop_input,
                correlation_id=correlation_id,
                snapshot_ids=snapshot_ids,
                result=PaperLoopResult(
                    status=PaperLoopStatus.RISK_BLOCKED,
                    validation_result=risk_result,
                    risk_result=risk_result,
                    order_generation_result=generation,
                    generated_order_intent=None,
                    correlation_id=correlation_id,
                    decision_snapshot_ids=tuple(snapshot_ids),
                ),
            )

        if generation.status == OrderGenerationStatus.NOOP:
            return _finalize_loop_result(
                self._decision_store,
                loop_input=loop_input,
                correlation_id=correlation_id,
                snapshot_ids=snapshot_ids,
                result=PaperLoopResult(
                    status=PaperLoopStatus.NOOP,
                    validation_result=risk_result,
                    risk_result=risk_result,
                    order_generation_result=generation,
                    generated_order_intent=None,
                    correlation_id=correlation_id,
                    decision_snapshot_ids=tuple(snapshot_ids),
                ),
            )

        assert generated_intent is not None

        quantity_result = self._quantity_resolver.resolve(
            intent=generated_intent,
            context=loop_input.risk_context,
            market_price=loop_input.market_price,
            current_position_quantity=_current_position_quantity(
                self._broker,
                loop_input,
            ),
        )

        if quantity_result.status == QuantityResolutionStatus.FAILED:
            return _finalize_loop_result(
                self._decision_store,
                loop_input=loop_input,
                correlation_id=correlation_id,
                snapshot_ids=snapshot_ids,
                result=PaperLoopResult(
                    status=PaperLoopStatus.QUANTITY_FAILED,
                    validation_result=quantity_result.validation_result,
                    risk_result=risk_result,
                    order_generation_result=generation,
                    quantity_resolution_result=quantity_result,
                    generated_order_intent=generated_intent,
                    correlation_id=correlation_id,
                    decision_snapshot_ids=tuple(snapshot_ids),
                ),
            )

        if quantity_result.status == QuantityResolutionStatus.NOOP:
            return _finalize_loop_result(
                self._decision_store,
                loop_input=loop_input,
                correlation_id=correlation_id,
                snapshot_ids=snapshot_ids,
                result=PaperLoopResult(
                    status=PaperLoopStatus.NOOP,
                    validation_result=quantity_result.validation_result,
                    risk_result=risk_result,
                    order_generation_result=generation,
                    quantity_resolution_result=quantity_result,
                    generated_order_intent=generated_intent,
                    correlation_id=correlation_id,
                    decision_snapshot_ids=tuple(snapshot_ids),
                ),
            )

        assert quantity_result.order_intent is not None
        broker_result = self._broker.submit_order(
            quantity_result.order_intent,
            loop_input.market_price,
        )

        fill = self._ledger.get_fill_by_order_id(broker_result.order_id)
        nav_snapshot: NavSnapshot | None = None

        if broker_result.status == OrderStatus.FILLED:
            nav_snapshot = _compute_and_save_nav_snapshot(
                self._ledger,
                broker=self._broker,
                loop_input=loop_input,
                order_id=broker_result.order_id,
            )
            loop_status = PaperLoopStatus.FILLED
            loop_validation = quantity_result.validation_result
        else:
            loop_status = PaperLoopStatus.BROKER_REJECTED
            loop_validation = quantity_result.validation_result

        return _finalize_loop_result(
            self._decision_store,
            loop_input=loop_input,
            correlation_id=correlation_id,
            snapshot_ids=snapshot_ids,
            result=PaperLoopResult(
                status=loop_status,
                validation_result=loop_validation,
                risk_result=risk_result,
                order_generation_result=generation,
                quantity_resolution_result=quantity_result,
                generated_order_intent=generated_intent,
                executable_order_intent=quantity_result.order_intent,
                broker_order_result=broker_result,
                fill=fill,
                nav_snapshot=nav_snapshot,
                correlation_id=correlation_id,
                decision_snapshot_ids=tuple(snapshot_ids),
            ),
        )


def _finalize_loop_result(
    store: SQLiteDecisionStore,
    *,
    loop_input: PaperLoopInput,
    correlation_id: str,
    snapshot_ids: list[DecisionId],
    result: PaperLoopResult,
) -> PaperLoopResult:
    """paper_loop snapshot 저장 후 최종 PaperLoopResult를 반환한다."""
    try:
        snapshot_ids.extend(_save_paper_loop_snapshot(store, loop_input, result))
    except DuplicateDecisionIdError as exc:
        return _validation_failed_result(
            loop_input=loop_input,
            correlation_id=correlation_id,
            message=str(exc),
            code=PAPER_LOOP_DUPLICATE_SNAPSHOT,
            snapshot_ids=tuple(snapshot_ids),
        )
    return result.model_copy(update={"decision_snapshot_ids": tuple(snapshot_ids)})


def _save_input_decision_snapshots(
    store: SQLiteDecisionStore,
    *,
    loop_input: PaperLoopInput,
) -> tuple[DecisionId, ...]:
    """allocator/analysis DecisionSnapshot을 저장한다."""
    allocator_snapshot = DecisionSnapshot.create(
        decision_id=loop_input.allocator_decision.decision_id,
        created_at=loop_input.allocator_decision.created_at,
        schema_name=ALLOCATOR_DECISION_SCHEMA,
        raw_payload=loop_input.allocator_decision.model_dump(mode="json"),
        validation_result=passed_validation_result(
            schema_name=ALLOCATOR_DECISION_SCHEMA,
            validator_version=ALLOCATOR_VALIDATOR_VERSION,
        ),
    )
    analysis_snapshot = DecisionSnapshot.create(
        decision_id=loop_input.analysis_decision.decision_id,
        created_at=loop_input.analysis_decision.created_at,
        schema_name=ANALYSIS_DECISION_SCHEMA,
        raw_payload=loop_input.analysis_decision.model_dump(mode="json"),
        validation_result=passed_validation_result(
            schema_name=ANALYSIS_DECISION_SCHEMA,
            validator_version=ANALYSIS_VALIDATOR_VERSION,
        ),
    )
    with store.transaction():
        store.save_decision_snapshot(allocator_snapshot)
        store.save_decision_snapshot(analysis_snapshot)
    return (
        loop_input.allocator_decision.decision_id,
        loop_input.analysis_decision.decision_id,
    )


def _save_paper_loop_snapshot(
    store: SQLiteDecisionStore,
    loop_input: PaperLoopInput,
    result: PaperLoopResult,
) -> tuple[DecisionId, ...]:
    """paper_loop.v1 DecisionSnapshot을 저장한다."""
    snapshot = build_paper_loop_snapshot(loop_input=loop_input, result=result)
    with store.transaction():
        store.save_decision_snapshot(snapshot)
    return (loop_input.normalized_run_id,)


def _validation_failed_result(
    *,
    loop_input: PaperLoopInput,
    correlation_id: str,
    message: str,
    code: str,
    snapshot_ids: tuple[DecisionId, ...],
) -> PaperLoopResult:
    from risk.order_generation import OrderGenerationResult

    validation = failed_validation_result(code=code, message=message)
    empty_generation = OrderGenerationResult(
        status=OrderGenerationStatus.BLOCKED,
        order_intent=None,
        validation_result=validation,
        correlation_id=correlation_id,
    )
    return PaperLoopResult(
        status=PaperLoopStatus.VALIDATION_FAILED,
        validation_result=validation,
        risk_result=validation,
        order_generation_result=empty_generation,
        correlation_id=correlation_id,
        decision_snapshot_ids=snapshot_ids,
    )


def _current_position_quantity(
    broker: PaperBrokerAdapter,
    loop_input: PaperLoopInput,
) -> Decimal | None:
    """현재 보유 수량을 조회한다. 없으면 None."""
    from domain.enums import Market

    market = Market(loop_input.analysis_decision.market.upper())
    position = broker.get_position(
        loop_input.analysis_decision.symbol,
        market,
        loop_input.broker_account_role,
    )
    if position is None:
        return None
    return position.quantity


def _compute_and_save_nav_snapshot(
    ledger: SQLiteLedger,
    *,
    broker: PaperBrokerAdapter,
    loop_input: PaperLoopInput,
    order_id: str,
) -> NavSnapshot:
    """FILLED 이후 단일 통화 NAV snapshot을 계산·저장한다.

    Transaction boundary (Phase 11 MVP):
    - PaperBroker fill/cash/position write는 Phase 3 PaperBroker transaction이 소유한다.
    - NAV snapshot은 Phase 11 post-fill diagnostic write이며 fill과 같은 transaction이 아니다.
    - Phase 11 MVP에서는 이 boundary를 유지한다.
    """
    currency = loop_input.market_price.currency
    account_role = loop_input.broker_account_role

    cash = broker.get_cash(currency, account_role)
    positions = broker.list_positions()

    invested = Decimal("0")
    for position in positions:
        if position.currency != currency:
            raise ValueError(
                "Phase 11 MVP does not support multi-currency NAV computation."
            )
        if position.market_price is not None:
            invested += position.quantity * position.market_price

    total_nav = cash.amount + invested
    snapshot = NavSnapshot(
        snapshot_id=f"nav-{order_id}",
        as_of=loop_input.created_at,
        total_nav_krw=total_nav,
        cash_krw=cash.amount,
        invested_krw=invested,
    )
    with ledger.transaction():
        ledger.save_nav_snapshot(snapshot)
    return snapshot

