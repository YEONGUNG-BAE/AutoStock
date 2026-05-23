from __future__ import annotations

from typing import Protocol

from domain.enums import AccountRole, Currency, Market
from domain.market import MarketPrice
from domain.order import OrderIntent, OrderResult
from domain.position import CashSnapshot, Position


class BrokerAdapter(Protocol):
    """브로커 어댑터 공통 인터페이스. 외부 API를 호출하지 않는다."""

    def submit_order(self, intent: OrderIntent, market_price: MarketPrice) -> OrderResult:
        """주문 의도를 접수하고 체결/대기/거절 결과를 반환한다."""
        ...

    def get_cash(self, currency: Currency, account_role: AccountRole) -> CashSnapshot:
        """계좌 역할별 현금 스냅샷을 조회한다."""
        ...

    def get_position(
        self,
        symbol: str,
        market: Market,
        account_role: AccountRole,
    ) -> Position | None:
        """종목 포지션을 조회한다. 없으면 None을 반환한다."""
        ...

    def list_positions(self) -> tuple[Position, ...]:
        """보유 포지션 전체를 조회한다."""
        ...
