from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain._datetime import parse_timezone_aware_datetime
from domain._decimal import to_decimal
from domain._strings import normalize_required_string
from domain.enums import Currency, Market


class MarketPrice(BaseModel):
    """외부 조회 없이 저장·검증만 하는 시장 가격 스냅샷이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    market: Market
    currency: Currency
    price: Decimal = Field(gt=Decimal("0"))
    as_of: datetime

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="symbol")

    @field_validator("price", mode="before")
    @classmethod
    def validate_price(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="price")

    @field_validator("as_of", mode="before")
    @classmethod
    def validate_as_of(cls, value: Any) -> datetime:
        return parse_timezone_aware_datetime(value, field_name="as_of")
