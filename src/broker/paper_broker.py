from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from domain.enums import AccountRole, Currency, Market, OrderSide, OrderStatus, OrderType
from domain.market import MarketPrice
from domain.money import Money
from domain.order import Fill, OrderIntent, OrderResult
from domain.position import CashSnapshot, Position
from ledger.sqlite_ledger import SQLiteLedger


# Phase 3 기본 fee는 0이다. 이후 Phase에서 fee model을 주입할 수 있도록 구조만 열어둔다.
FeeCalculator = Callable[[OrderIntent, Decimal, Currency], tuple[Money, Money]]


def _zero_fees(_intent: OrderIntent, _fill_price: Decimal, currency: Currency) -> tuple[Money, Money]:
    zero = Money.zero(currency)
    return zero, zero


def _expected_currency_for_market(market: Market) -> Currency:
    if market == Market.KR:
        return Currency.KRW
    if market == Market.US:
        return Currency.USD
    raise ValueError(f"unsupported market for currency mapping: {market.value}")


@dataclass(frozen=True)
class _ExecutionDecision:
    """체결 판단 결과. fill_price/fee는 주문 처리 1회당 한 번만 계산한다."""

    status: OrderStatus
    accepted: bool
    rejection_reason: str | None
    fill_price: Decimal | None = None
    commission: Money | None = None
    tax: Money | None = None
    slippage: Money | None = None


class PaperBrokerAdapter:
    """자체 SQLite 원장 기반 paper execution adapter."""

    def __init__(
        self,
        ledger: SQLiteLedger,
        *,
        initial_cash: CashSnapshot | tuple[CashSnapshot, ...] | None = None,
        fee_calculator: FeeCalculator | None = None,
    ) -> None:
        self._ledger = ledger
        self._fee_calculator = fee_calculator or _zero_fees
        if initial_cash is not None:
            self._seed_initial_cash(initial_cash)

    @classmethod
    def create(
        cls,
        db_path: Path | str,
        *,
        initial_cash: CashSnapshot | tuple[CashSnapshot, ...],
        fee_calculator: FeeCalculator | None = None,
    ) -> PaperBrokerAdapter:
        """SQLite 파일과 초기 현금으로 PaperBrokerAdapter를 생성한다."""
        ledger = SQLiteLedger(db_path)
        return cls(ledger, initial_cash=initial_cash, fee_calculator=fee_calculator)

    def submit_order(self, intent: OrderIntent, market_price: MarketPrice) -> OrderResult:
        """주문 의도를 즉시 체결/대기/거절 처리한다."""
        now = intent.created_at

        # 1) duplicate order_id 확인 — Phase 3에서 최우선 정책.
        # order_results.order_id는 unique이므로 duplicate 시 추가 row를 insert하지 않고
        # in-memory rejected OrderResult만 반환한다. 기존 원장 record는 유지된다.
        if self._ledger.has_processed_order(intent.order_id):
            return OrderResult(
                order_id=intent.order_id,
                status=OrderStatus.REJECTED,
                accepted=False,
                rejection_reason="duplicate order_id",
                created_at=now,
            )

        # 2) intent / market_price mismatch 검증
        mismatch_reason = self._validate_intent_market_price_match(intent, market_price)
        if mismatch_reason is not None:
            return self._record_rejected_order(
                intent,
                rejection_reason=mismatch_reason,
                created_at=now,
            )

        # 3) target_weight_percent reject — sizing 전 단계, fee 계산 없음
        if intent.target_weight_percent is not None:
            return self._record_rejected_order(
                intent,
                rejection_reason="target_weight_percent requires sizing before broker execution",
                created_at=now,
            )

        # domain invariant: target_weight reject 이후 quantity는 항상 존재한다.
        assert intent.quantity is not None

        # 4) _evaluate_order — FILLED 경로에서만 fee/tax/slippage 1회 계산
        decision = self._evaluate_order(intent, market_price)

        if decision.status == OrderStatus.REJECTED:
            return self._record_rejected_order(
                intent,
                rejection_reason=decision.rejection_reason or "order rejected",
                created_at=now,
            )

        if decision.status == OrderStatus.PENDING:
            return self._record_pending_order(intent, created_at=now)

        return self._record_filled_order(intent, market_price, decision, created_at=now)

    def get_cash(self, currency: Currency, account_role: AccountRole) -> CashSnapshot:
        """계좌 역할별 현금 스냅샷을 조회한다."""
        cash = self._ledger.get_cash(currency, account_role)
        if cash is None:
            raise LookupError(
                f"Cash snapshot not found for currency={currency.value}, account_role={account_role.value}."
            )
        return cash

    def get_position(
        self,
        symbol: str,
        market: Market,
        account_role: AccountRole,
    ) -> Position | None:
        """종목 포지션을 조회한다."""
        return self._ledger.get_position(symbol, market, account_role)

    def list_positions(self) -> tuple[Position, ...]:
        """보유 포지션 전체를 조회한다."""
        return self._ledger.list_positions()

    def _validate_intent_market_price_match(
        self,
        intent: OrderIntent,
        market_price: MarketPrice,
    ) -> str | None:
        if intent.symbol != market_price.symbol:
            return "symbol mismatch"
        if intent.market != market_price.market:
            return "market mismatch"
        expected_currency = _expected_currency_for_market(market_price.market)
        if market_price.currency != expected_currency:
            return "currency mismatch"
        return None

    def _seed_initial_cash(self, initial_cash: CashSnapshot | tuple[CashSnapshot, ...]) -> None:
        snapshots = (initial_cash,) if isinstance(initial_cash, CashSnapshot) else initial_cash
        with self._ledger.transaction():
            for cash in snapshots:
                existing = self._ledger.get_cash(cash.currency, cash.account_role)
                if existing is None:
                    self._ledger.apply_cash_change(
                        cash,
                        order_id=None,
                        correlation_id=None,
                        delta_amount=cash.amount,
                        reason="INITIAL_CASH",
                    )

    def _evaluate_order(self, intent: OrderIntent, market_price: MarketPrice) -> _ExecutionDecision:
        assert intent.quantity is not None
        quantity = intent.quantity

        if intent.order_type == OrderType.LIMIT:
            assert intent.limit_price is not None
            # LIMIT은 최대 매수가/최소 매도가 조건으로 PENDING 게이팅만 수행한다.
            if intent.side == OrderSide.BUY and market_price.price > intent.limit_price:
                return _ExecutionDecision(
                    status=OrderStatus.PENDING,
                    accepted=True,
                    rejection_reason=None,
                )
            if intent.side == OrderSide.SELL and market_price.price < intent.limit_price:
                return _ExecutionDecision(
                    status=OrderStatus.PENDING,
                    accepted=True,
                    rejection_reason=None,
                )

        # Phase 3 단순 paper broker: bid/ask/orderbook 없으므로
        # MARKET과 LIMIT 모두 fill_price = market_price.price 이다.
        # LIMIT과 MARKET의 차이는 PENDING 게이팅뿐이다.
        fill_price = market_price.price

        if intent.side == OrderSide.SELL:
            position = self._ledger.get_position(intent.symbol, intent.market, intent.account_role)
            held = position.quantity if position is not None else Decimal("0")
            if held < quantity:
                return _ExecutionDecision(
                    status=OrderStatus.REJECTED,
                    accepted=False,
                    rejection_reason="insufficient position quantity",
                )
            commission, tax = self._fee_calculator(intent, fill_price, market_price.currency)
            slippage = Money.zero(market_price.currency)
            return _ExecutionDecision(
                status=OrderStatus.FILLED,
                accepted=True,
                rejection_reason=None,
                fill_price=fill_price,
                commission=commission,
                tax=tax,
                slippage=slippage,
            )

        commission, tax = self._fee_calculator(intent, fill_price, market_price.currency)
        slippage = Money.zero(market_price.currency)
        total_cost = quantity * fill_price + commission.amount + tax.amount
        cash = self._ledger.get_cash(market_price.currency, intent.account_role)
        available = cash.amount if cash is not None else Decimal("0")
        if available < total_cost:
            return _ExecutionDecision(
                status=OrderStatus.REJECTED,
                accepted=False,
                rejection_reason="insufficient cash",
            )

        return _ExecutionDecision(
            status=OrderStatus.FILLED,
            accepted=True,
            rejection_reason=None,
            fill_price=fill_price,
            commission=commission,
            tax=tax,
            slippage=slippage,
        )

    def _record_rejected_order(
        self,
        intent: OrderIntent,
        *,
        rejection_reason: str,
        created_at: datetime,
    ) -> OrderResult:
        result = OrderResult(
            order_id=intent.order_id,
            status=OrderStatus.REJECTED,
            accepted=False,
            rejection_reason=rejection_reason,
            created_at=created_at,
        )
        with self._ledger.transaction():
            if not self._ledger.has_processed_order(intent.order_id):
                self._ledger.save_order_intent(intent)
            self._ledger.save_order_result(result)
        return result

    def _record_pending_order(self, intent: OrderIntent, *, created_at: datetime) -> OrderResult:
        result = OrderResult(
            order_id=intent.order_id,
            status=OrderStatus.PENDING,
            accepted=True,
            rejection_reason=None,
            created_at=created_at,
        )
        with self._ledger.transaction():
            self._ledger.save_order_intent(intent)
            self._ledger.save_order_result(result)
        return result

    def _record_filled_order(
        self,
        intent: OrderIntent,
        market_price: MarketPrice,
        decision: _ExecutionDecision,
        *,
        created_at: datetime,
    ) -> OrderResult:
        assert intent.quantity is not None
        assert decision.fill_price is not None
        assert decision.commission is not None
        assert decision.tax is not None
        assert decision.slippage is not None

        fill = Fill(
            fill_id=f"{intent.order_id}-fill",
            order_id=intent.order_id,
            symbol=intent.symbol,
            market=intent.market,
            side=intent.side,
            quantity=intent.quantity,
            fill_price=decision.fill_price,
            commission=decision.commission,
            tax=decision.tax,
            slippage=decision.slippage,
            filled_at=created_at,
        )

        result = OrderResult(
            order_id=intent.order_id,
            status=OrderStatus.FILLED,
            accepted=True,
            rejection_reason=None,
            created_at=created_at,
        )

        with self._ledger.transaction():
            self._ledger.save_order_intent(intent)
            self._ledger.save_order_result(result)
            self._ledger.save_fill(fill)
            self._apply_fill(intent, market_price, fill)

        return result

    def _apply_fill(self, intent: OrderIntent, market_price: MarketPrice, fill: Fill) -> None:
        assert intent.quantity is not None
        proceeds = fill.quantity * fill.fill_price

        if intent.side == OrderSide.BUY:
            cash_delta = -(proceeds + fill.commission.amount + fill.tax.amount)
            self._increase_position(intent, market_price, fill)
            reason = "BUY_FILL"
        else:
            cash_delta = proceeds - fill.commission.amount - fill.tax.amount
            self._decrease_position(intent, fill)
            reason = "SELL_FILL"

        self._update_cash(
            intent.account_role,
            market_price.currency,
            cash_delta,
            fill.filled_at,
            order_id=intent.order_id,
            correlation_id=intent.correlation_id,
            reason=reason,
        )

    def _update_cash(
        self,
        account_role: AccountRole,
        currency: Currency,
        cash_delta: Decimal,
        as_of: datetime,
        *,
        order_id: str,
        correlation_id: str,
        reason: str,
    ) -> None:
        existing = self._ledger.get_cash(currency, account_role)
        current_amount = existing.amount if existing is not None else Decimal("0")
        new_amount = current_amount + cash_delta

        if new_amount < Decimal("0"):
            raise ValueError("Cash amount would become negative after fill application.")

        self._ledger.apply_cash_change(
            CashSnapshot(
                currency=currency,
                amount=new_amount,
                account_role=account_role,
                as_of=as_of,
            ),
            order_id=order_id,
            correlation_id=correlation_id,
            delta_amount=cash_delta,
            reason=reason,
        )

    def _increase_position(self, intent: OrderIntent, market_price: MarketPrice, fill: Fill) -> None:
        existing = self._ledger.get_position(intent.symbol, intent.market, intent.account_role)
        if existing is None:
            new_position = Position(
                symbol=intent.symbol,
                market=intent.market,
                asset_class=intent.asset_class,
                account_role=intent.account_role,
                quantity=fill.quantity,
                avg_cost=fill.fill_price,
                currency=market_price.currency,
                market_price=fill.fill_price,
            )
        else:
            total_qty = existing.quantity + fill.quantity
            weighted_avg = (
                (existing.quantity * existing.avg_cost) + (fill.quantity * fill.fill_price)
            ) / total_qty
            new_position = Position(
                symbol=intent.symbol,
                market=intent.market,
                asset_class=intent.asset_class,
                account_role=intent.account_role,
                quantity=total_qty,
                avg_cost=weighted_avg,
                currency=market_price.currency,
                market_price=fill.fill_price,
            )
        self._ledger.upsert_position(new_position)

    def _decrease_position(self, intent: OrderIntent, fill: Fill) -> None:
        existing = self._ledger.get_position(intent.symbol, intent.market, intent.account_role)
        if existing is None:
            raise ValueError("Cannot decrease position that does not exist.")

        remaining_qty = existing.quantity - fill.quantity
        if remaining_qty < Decimal("0"):
            raise ValueError("Position quantity would become negative after sell fill.")

        # 전량 매도 시 포지션 행을 삭제한다. list_positions/get_position은 None/빈 목록을 반환한다.
        if remaining_qty == Decimal("0"):
            self._ledger.delete_position(intent.symbol, intent.market, intent.account_role)
            return

        self._ledger.upsert_position(
            Position(
                symbol=intent.symbol,
                market=intent.market,
                asset_class=intent.asset_class,
                account_role=intent.account_role,
                quantity=remaining_qty,
                avg_cost=existing.avg_cost,
                currency=existing.currency,
                market_price=fill.fill_price,
            )
        )
