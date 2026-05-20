from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from config import ExecutionMode


@dataclass(frozen=True)
class CashSnapshot:
    currency: str
    available: Decimal
    total: Decimal
    as_of: datetime


@dataclass(frozen=True)
class Position:
    symbol: str
    market: str
    asset_class: str
    quantity: Decimal
    avg_cost: Decimal
    market_price: Decimal
    account_role: str


@dataclass(frozen=True)
class OrderIntent:
    order_intent_id: str
    decision_id: str
    symbol: str
    market: str
    asset_class: str
    side: str  # BUY | SELL
    quantity: Decimal
    execution_mode: ExecutionMode
    account_role: str


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str
    submitted_at: datetime


@dataclass(frozen=True)
class CancelResult:
    order_id: str
    status: str
    canceled_at: datetime


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    quantity: Decimal
    fill_price: Decimal
    commission: Decimal
    tax: Decimal
    slippage: Decimal
    filled_at: datetime


class BrokerAdapter(Protocol):
    def get_cash(self) -> CashSnapshot: ...
    def get_positions(self) -> list[Position]: ...
    def place_order(self, order: OrderIntent) -> OrderResult: ...
    def cancel_order(self, order_id: str) -> CancelResult: ...
    def get_fills(self) -> list[Fill]: ...
