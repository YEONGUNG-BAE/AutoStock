from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain._datetime import parse_timezone_aware_datetime
from domain._decimal import to_decimal, to_optional_decimal
from domain._strings import normalize_required_string
from domain.enums import Currency, Market


class MarketEventType(StrEnum):
    TRADE = "trade"
    BEST_BID_ASK = "best_bid_ask"
    HEARTBEAT = "heartbeat"


class ProviderSequence(BaseModel):
    """Provider가 채널별로 부여한 단조 증가 sequence 메타데이터다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    channel: str
    sequence: int = Field(ge=0)
    received_at: datetime

    @field_validator("provider", mode="before")
    @classmethod
    def validate_provider(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="provider")

    @field_validator("channel", mode="before")
    @classmethod
    def validate_channel(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="channel")

    @field_validator("sequence", mode="before")
    @classmethod
    def validate_sequence(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("sequence must be an integer, not bool.")
        return value

    @field_validator("received_at", mode="before")
    @classmethod
    def validate_received_at(cls, value: Any) -> datetime:
        return parse_timezone_aware_datetime(value, field_name="received_at")


class NormalizedTradeTick(BaseModel):
    """정규화된 체결(trade) tick. 외부 호출 없이 검증·저장만 한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal[MarketEventType.TRADE] = MarketEventType.TRADE
    provider: str
    symbol: str
    market: Market
    currency: Currency
    price: Decimal = Field(gt=Decimal("0"))
    quantity: Decimal = Field(gt=Decimal("0"))
    trade_at: datetime
    received_at: datetime
    provider_sequence: ProviderSequence
    cumulative_volume: Decimal | None = Field(default=None, ge=Decimal("0"))

    @field_validator("provider", mode="before")
    @classmethod
    def validate_provider(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="provider")

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="symbol")

    @field_validator("price", mode="before")
    @classmethod
    def validate_price(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="price")

    @field_validator("quantity", mode="before")
    @classmethod
    def validate_quantity(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="quantity")

    @field_validator("cumulative_volume", mode="before")
    @classmethod
    def validate_cumulative_volume(cls, value: Any) -> Decimal | None:
        return to_optional_decimal(value, field_name="cumulative_volume")

    @field_validator("trade_at", "received_at", mode="before")
    @classmethod
    def validate_timestamps(cls, value: Any, info: Any) -> datetime:
        return parse_timezone_aware_datetime(value, field_name=info.field_name)


class NormalizedBestBidAsk(BaseModel):
    """정규화된 최우선 호가(best bid/ask) snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal[MarketEventType.BEST_BID_ASK] = MarketEventType.BEST_BID_ASK
    provider: str
    symbol: str
    market: Market
    currency: Currency
    bid_price: Decimal = Field(gt=Decimal("0"))
    ask_price: Decimal = Field(gt=Decimal("0"))
    bid_quantity: Decimal = Field(ge=Decimal("0"))
    ask_quantity: Decimal = Field(ge=Decimal("0"))
    quote_at: datetime
    received_at: datetime
    provider_sequence: ProviderSequence

    @field_validator("provider", mode="before")
    @classmethod
    def validate_provider(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="provider")

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="symbol")

    @field_validator("bid_price", "ask_price", "bid_quantity", "ask_quantity", mode="before")
    @classmethod
    def validate_book_numbers(cls, value: Any, info: Any) -> Decimal:
        return to_decimal(value, field_name=info.field_name)

    @field_validator("quote_at", "received_at", mode="before")
    @classmethod
    def validate_timestamps(cls, value: Any, info: Any) -> datetime:
        return parse_timezone_aware_datetime(value, field_name=info.field_name)

    @model_validator(mode="after")
    def reject_crossed_book(self) -> NormalizedBestBidAsk:
        if self.bid_price > self.ask_price:
            raise ValueError("bid_price must not exceed ask_price (crossed book).")
        return self


class MarketHeartbeat(BaseModel):
    """연결 상태 heartbeat 이벤트."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal[MarketEventType.HEARTBEAT] = MarketEventType.HEARTBEAT
    provider: str
    channel: str
    sent_at: datetime
    received_at: datetime
    provider_sequence: ProviderSequence | None = None

    @field_validator("provider", mode="before")
    @classmethod
    def validate_provider(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="provider")

    @field_validator("channel", mode="before")
    @classmethod
    def validate_channel(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="channel")

    @field_validator("sent_at", "received_at", mode="before")
    @classmethod
    def validate_timestamps(cls, value: Any, info: Any) -> datetime:
        return parse_timezone_aware_datetime(value, field_name=info.field_name)


MarketEvent = Annotated[
    Union[NormalizedTradeTick, NormalizedBestBidAsk, MarketHeartbeat],
    Field(discriminator="event_type"),
]


__all__ = [
    "MarketEvent",
    "MarketEventType",
    "MarketHeartbeat",
    "NormalizedBestBidAsk",
    "NormalizedTradeTick",
    "ProviderSequence",
]
