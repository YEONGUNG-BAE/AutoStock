from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain._decimal import to_decimal
from domain.enums import Currency, Market


class MarketPrice(BaseModel):
    """외부 조회 없이 저장·검증만 하는 시장 가격 스냅샷이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1)
    market: Market
    currency: Currency
    price: Decimal = Field(gt=Decimal("0"))
    as_of: datetime

    @field_validator("price", mode="before")
    @classmethod
    def validate_price(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="price")
