from __future__ import annotations

from domain.enums import AccountRole, Currency, Market
from domain.market import MarketPrice
from domain.order import OrderIntent, OrderResult
from domain.position import CashSnapshot, Position

from broker.kis_client import KisReadOnlyClient
from broker.kis_models import KisAccountRoleError, KisPositionSnapshot


class KisLiveOrderBlockedError(RuntimeError):
    """KIS read-only adapter에서 주문 submission을 차단할 때 발생한다."""


class KisLiveReadOnlyBrokerAdapter:
    """KIS live read-only broker adapter. 주문 endpoint를 호출하지 않는다."""

    _EXECUTION_ACCOUNT_ROLES: tuple[AccountRole, ...] = (
        AccountRole.KR_TAX_ADVANTAGED,
        AccountRole.US_REGULAR,
    )

    def __init__(self, client: KisReadOnlyClient) -> None:
        self._client = client

    def submit_order(self, intent: OrderIntent, market_price: MarketPrice) -> OrderResult:
        raise KisLiveOrderBlockedError(
            "KIS live read-only adapter does not submit orders. "
            "Phase 14 allows read-only inquiry only."
        )

    def get_cash(self, currency: Currency, account_role: AccountRole) -> CashSnapshot:
        _reject_paper_role(account_role)
        balance = self._client.get_balance(account_role)
        if balance.currency != currency:
            raise ValueError(
                f"Requested currency={currency.value} does not match "
                f"KIS balance currency={balance.currency.value} for account_role={account_role.value}."
            )
        return CashSnapshot(
            currency=balance.currency,
            amount=balance.cash.amount,
            account_role=account_role,
            as_of=balance.as_of,
        )

    def get_position(
        self,
        symbol: str,
        market: Market,
        account_role: AccountRole,
    ) -> Position | None:
        _reject_paper_role(account_role)
        for snapshot in self._client.list_positions(account_role):
            if snapshot.symbol == symbol and snapshot.market == market:
                return _to_domain_position(snapshot)
        return None

    def list_positions(self) -> tuple[Position, ...]:
        positions: list[Position] = []
        for account_role in self._EXECUTION_ACCOUNT_ROLES:
            for snapshot in self._client.list_positions(account_role):
                positions.append(_to_domain_position(snapshot))
        return tuple(positions)


def _reject_paper_role(account_role: AccountRole) -> None:
    if account_role == AccountRole.PAPER:
        raise KisAccountRoleError("AccountRole.PAPER is not valid for KIS live adapter.")


def _to_domain_position(snapshot: KisPositionSnapshot) -> Position:
    return Position(
        symbol=snapshot.symbol,
        market=snapshot.market,
        asset_class=snapshot.asset_class,
        account_role=snapshot.account_role,
        quantity=snapshot.quantity,
        avg_cost=snapshot.avg_cost,
        currency=snapshot.currency,
        market_price=snapshot.market_price,
    )


__all__ = [
    "KisLiveOrderBlockedError",
    "KisLiveReadOnlyBrokerAdapter",
]
