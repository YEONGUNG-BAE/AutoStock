from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from domain._decimal import to_decimal
from domain.enums import Currency


class Money(BaseModel):
    """금액과 통화를 함께 보존한다. cash ledger delta 등 음수 amount를 허용한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    amount: Decimal
    currency: Currency

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="amount")

    @classmethod
    def from_int(cls, amount: int, currency: Currency) -> "Money":
        return cls(amount=Decimal(amount), currency=currency)

    @classmethod
    def from_str(cls, amount: str, currency: Currency) -> "Money":
        return cls(amount=Decimal(amount), currency=currency)

    @classmethod
    def zero(cls, currency: Currency) -> "Money":
        return cls(amount=Decimal("0"), currency=currency)
