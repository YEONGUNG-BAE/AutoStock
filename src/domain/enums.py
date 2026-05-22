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
    ISA = "ISA"
    GENERAL = "GENERAL"
    CMA = "CMA"
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
