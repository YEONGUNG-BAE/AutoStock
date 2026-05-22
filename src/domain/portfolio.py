from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain._decimal import to_decimal, to_optional_decimal
from domain.position import CashSnapshot, Position


NAV_TOTAL_TOLERANCE_KRW = Decimal("0.01")


class PortfolioSnapshot(BaseModel):
    """포트폴리오 전체 스냅샷이다. Phase 2에서는 환율 변환을 수행하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1)
    as_of: datetime
    positions: tuple[Position, ...]
    cash: tuple[CashSnapshot, ...]
    total_nav_krw: Decimal = Field(ge=Decimal("0"))
    cash_krw: Decimal = Field(ge=Decimal("0"))
    invested_percent: Decimal
    mdd_percent: Decimal | None = None

    @field_validator("total_nav_krw", "cash_krw", "invested_percent", mode="before")
    @classmethod
    def validate_required_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="portfolio_decimal")

    @field_validator("mdd_percent", mode="before")
    @classmethod
    def validate_mdd_percent(cls, value: Any) -> Decimal | None:
        return to_optional_decimal(value, field_name="mdd_percent")

    @model_validator(mode="after")
    def validate_portfolio_snapshot(self) -> Self:
        if not (Decimal("0") <= self.invested_percent <= Decimal("100")):
            raise ValueError("PortfolioSnapshot invested_percent must be between 0 and 100.")

        if self.mdd_percent is not None and self.mdd_percent > Decimal("0"):
            raise ValueError("PortfolioSnapshot mdd_percent must be less than or equal to 0.")

        return self


class NavSnapshot(BaseModel):
    """NAV 요약 스냅샷이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1)
    as_of: datetime
    total_nav_krw: Decimal = Field(ge=Decimal("0"))
    cash_krw: Decimal = Field(ge=Decimal("0"))
    invested_krw: Decimal = Field(ge=Decimal("0"))
    daily_return_percent: Decimal | None = None
    mdd_percent: Decimal | None = None

    @field_validator("total_nav_krw", "cash_krw", "invested_krw", mode="before")
    @classmethod
    def validate_required_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="nav_decimal")

    @field_validator("daily_return_percent", "mdd_percent", mode="before")
    @classmethod
    def validate_optional_percent(cls, value: Any) -> Decimal | None:
        return to_optional_decimal(value, field_name="optional_percent")

    @model_validator(mode="after")
    def validate_nav_snapshot(self) -> Self:
        expected_total = self.cash_krw + self.invested_krw
        if abs(expected_total - self.total_nav_krw) > NAV_TOTAL_TOLERANCE_KRW:
            raise ValueError(
                "NavSnapshot total_nav_krw must match cash_krw + invested_krw within tolerance."
            )

        if self.mdd_percent is not None and self.mdd_percent > Decimal("0"):
            raise ValueError("NavSnapshot mdd_percent must be less than or equal to 0.")

        return self
