from __future__ import annotations

from enum import StrEnum

from config.settings import ExecutionMode


class Market(StrEnum):
    KR = "KR"
    US = "US"


class AssetClass(StrEnum):
    KR_EQUITY = "KR_EQUITY"
    US_EQUITY = "US_EQUITY"
    GOLD = "GOLD"
    CASH = "CASH"


class AccountRole(StrEnum):
    """Portfolio/account role. KIS product name mapping은 Phase 14 adapter/config layer에서 처리한다."""

    KR_TAX_ADVANTAGED = "KR_TAX_ADVANTAGED"
    US_REGULAR = "US_REGULAR"
    CASH_BUFFER = "CASH_BUFFER"
    PAPER = "PAPER"


class Currency(StrEnum):
    KRW = "KRW"
    USD = "USD"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class TimeInForce(StrEnum):
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


__all__ = [
    "AccountRole",
    "AssetClass",
    "Currency",
    "ExecutionMode",
    "Market",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "TimeInForce",
]
