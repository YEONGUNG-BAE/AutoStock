from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from config.settings import ExecutionMode
from domain._datetime import require_timezone_aware_datetime
from domain._decimal import to_decimal, to_optional_decimal
from domain._strings import normalize_required_string
from domain.enums import AccountRole, AssetClass, Market, OrderSide, OrderStatus, OrderType, TimeInForce
from domain.money import Money


class OrderIntent(BaseModel):
    """주문 의도를 표현한다. PaperBroker와 RiskFilter가 공유하는 입력 타입이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    correlation_id: str
    symbol: str
    market: Market
    asset_class: AssetClass
    account_role: AccountRole
    side: OrderSide
    order_type: OrderType
    execution_mode: ExecutionMode
    time_in_force: TimeInForce = TimeInForce.DAY
    quantity: Decimal | None = None
    target_weight_percent: Decimal | None = None
    limit_price: Decimal | None = None
    reason_code: str | None = None
    source_decision_id: str | None = None
    created_at: datetime

    @field_validator("order_id", "correlation_id", "symbol", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("quantity", "target_weight_percent", "limit_price", mode="before")
    @classmethod
    def validate_optional_decimals(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return to_decimal(value, field_name="optional_decimal")

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="created_at")

    @model_validator(mode="after")
    def validate_order_intent(self) -> Self:
        has_quantity = self.quantity is not None
        has_target_weight = self.target_weight_percent is not None

        if has_quantity and has_target_weight:
            raise ValueError("OrderIntent requires exactly one of quantity or target_weight_percent.")

        if not has_quantity and not has_target_weight:
            raise ValueError("OrderIntent requires exactly one of quantity or target_weight_percent.")

        if has_quantity and self.quantity <= Decimal("0"):
            raise ValueError("OrderIntent quantity must be greater than 0.")

        if has_target_weight and not (Decimal("0") <= self.target_weight_percent <= Decimal("100")):
            raise ValueError("OrderIntent target_weight_percent must be between 0 and 100.")

        if self.order_type == OrderType.LIMIT:
            if self.limit_price is None or self.limit_price <= Decimal("0"):
                raise ValueError("LIMIT OrderIntent requires limit_price greater than 0.")
        elif self.order_type == OrderType.MARKET and self.limit_price is not None:
            raise ValueError("MARKET OrderIntent must not include limit_price.")

        return self


class OrderResult(BaseModel):
    """주문 접수/거절 결과를 표현한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    status: OrderStatus
    accepted: bool
    rejection_reason: str | None = None
    created_at: datetime

    @field_validator("order_id", mode="before")
    @classmethod
    def validate_order_id(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="order_id")

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="created_at")

    @model_validator(mode="after")
    def validate_order_result(self) -> Self:
        if self.status == OrderStatus.REJECTED and self.accepted:
            raise ValueError("OrderResult with status=REJECTED must have accepted=False.")

        if not self.accepted and not self.rejection_reason:
            raise ValueError("OrderResult with accepted=False requires rejection_reason.")

        if self.accepted and self.status not in {OrderStatus.PENDING, OrderStatus.FILLED}:
            raise ValueError("Accepted OrderResult status must be PENDING or FILLED.")

        return self


class Fill(BaseModel):
    """체결 결과를 표현한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fill_id: str
    order_id: str
    symbol: str
    market: Market
    side: OrderSide
    quantity: Decimal = Field(gt=Decimal("0"))
    fill_price: Decimal = Field(gt=Decimal("0"))
    commission: Money
    tax: Money
    slippage: Money | None = None
    filled_at: datetime

    @field_validator("fill_id", "order_id", "symbol", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("quantity", "fill_price", mode="before")
    @classmethod
    def validate_positive_decimals(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="fill_decimal")

    @field_validator("filled_at", mode="before")
    @classmethod
    def validate_filled_at(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="filled_at")

    @model_validator(mode="after")
    def validate_commission_and_tax(self) -> Self:
        if self.commission.amount < Decimal("0"):
            raise ValueError("Fill commission.amount must be greater than or equal to 0.")

        if self.tax.amount < Decimal("0"):
            raise ValueError("Fill tax.amount must be greater than or equal to 0.")

        return self
