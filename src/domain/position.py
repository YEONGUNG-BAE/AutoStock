from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from domain._datetime import require_timezone_aware_datetime
from domain._decimal import to_decimal, to_optional_decimal
from domain._strings import normalize_required_string
from domain.enums import AccountRole, AssetClass, Currency, Market


class Position(BaseModel):
    """보유 포지션 스냅샷이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    market: Market
    asset_class: AssetClass
    account_role: AccountRole
    quantity: Decimal = Field(ge=Decimal("0"))
    avg_cost: Decimal = Field(ge=Decimal("0"))
    currency: Currency
    market_price: Decimal | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="symbol")

    @field_validator("quantity", "avg_cost", mode="before")
    @classmethod
    def validate_non_negative_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="position_decimal")

    @field_validator("market_price", mode="before")
    @classmethod
    def validate_market_price(cls, value: Any) -> Decimal | None:
        return to_optional_decimal(value, field_name="market_price")

    @model_validator(mode="after")
    def validate_market_price_positive(self) -> Self:
        if self.market_price is not None and self.market_price <= Decimal("0"):
            raise ValueError("Position market_price must be greater than 0 when provided.")
        return self

    @computed_field
    @property
    def market_value(self) -> Decimal:
        price = self.market_price if self.market_price is not None else self.avg_cost
        return self.quantity * price


class CashSnapshot(BaseModel):
    """계좌별 현금 스냅샷이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: Currency
    amount: Decimal = Field(ge=Decimal("0"))
    account_role: AccountRole
    as_of: datetime

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="amount")

    @field_validator("as_of", mode="before")
    @classmethod
    def validate_as_of(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="as_of")
