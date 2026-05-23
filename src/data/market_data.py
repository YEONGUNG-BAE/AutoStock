from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain._datetime import require_timezone_aware_datetime
from domain._decimal import to_decimal
from domain._strings import normalize_required_string
from domain.identifiers import DateId
from domain.source import DateIdSourceRecord, FactType


class MarketDataPoint(BaseModel):
    """yfinance 등 시장 가격 adapter 출력. DateIdSourceRecord 변환 전 intermediate model이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    market: str | None = None
    price: Decimal = Field(gt=Decimal("0"))
    currency: str | None = None
    source_name: str
    source_timestamp: datetime
    as_of: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol", "source_name", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("market", "currency", mode="before")
    @classmethod
    def validate_optional_strings(cls, value: Any, info) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("price", mode="before")
    @classmethod
    def validate_price(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="price")

    @field_validator("source_timestamp", "as_of", mode="before")
    @classmethod
    def validate_timezone_aware_datetimes(cls, value: Any, info) -> datetime:
        return require_timezone_aware_datetime(value, field_name=info.field_name)

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any) -> dict[str, Any]:
        from decision.canonical_json import canonicalize_payload

        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value


class MacroDataPoint(BaseModel):
    """FRED macro observation adapter 출력. DateIdSourceRecord 변환 전 intermediate model이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    series_id: str
    value: Decimal = Field(gt=Decimal("0"))
    source_name: str
    source_timestamp: datetime
    as_of: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("series_id", "source_name", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="value")

    @field_validator("source_timestamp", "as_of", mode="before")
    @classmethod
    def validate_timezone_aware_datetimes(cls, value: Any, info) -> datetime:
        return require_timezone_aware_datetime(value, field_name=info.field_name)

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any) -> dict[str, Any]:
        from decision.canonical_json import canonicalize_payload

        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value


class DisclosureRecord(BaseModel):
    """DART 공시 adapter 출력. DateIdSourceRecord 변환 전 intermediate model이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    title: str
    source_name: str
    source_timestamp: datetime
    as_of: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    source_url: str | None = None

    @field_validator("symbol", "title", "source_name", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("source_url", mode="before")
    @classmethod
    def validate_optional_source_url(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name="source_url")

    @field_validator("source_timestamp", "as_of", mode="before")
    @classmethod
    def validate_timezone_aware_datetimes(cls, value: Any, info) -> datetime:
        return require_timezone_aware_datetime(value, field_name=info.field_name)

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any) -> dict[str, Any]:
        from decision.canonical_json import canonicalize_payload

        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("payload must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value


def market_data_point_to_source_record(
    point: MarketDataPoint,
    date_id: DateId,
) -> DateIdSourceRecord:
    """MarketDataPoint를 FactType.PRICE DateIdSourceRecord로 변환한다. 저장은 caller 책임이다."""
    payload = _build_market_payload(point)
    return DateIdSourceRecord(
        date_id=date_id,
        fact_type=FactType.PRICE,
        source_name=point.source_name,
        source_timestamp=point.source_timestamp,
        created_at=point.as_of,
        summary=f"{point.symbol} latest price {point.price}",
        payload=payload,
        symbol=point.symbol,
        market=point.market,
        source_url=None,
    )


def macro_data_point_to_source_record(
    point: MacroDataPoint,
    date_id: DateId,
) -> DateIdSourceRecord:
    """MacroDataPoint를 FactType.MACRO DateIdSourceRecord로 변환한다. 저장은 caller 책임이다."""
    payload = _build_macro_payload(point)
    return DateIdSourceRecord(
        date_id=date_id,
        fact_type=FactType.MACRO,
        source_name=point.source_name,
        source_timestamp=point.source_timestamp,
        created_at=point.as_of,
        summary=f"{point.series_id} latest observation {point.value}",
        payload=payload,
        symbol=None,
        market=None,
        source_url=None,
    )


def disclosure_record_to_source_record(
    record: DisclosureRecord,
    date_id: DateId,
) -> DateIdSourceRecord:
    """DisclosureRecord를 FactType.DISCLOSURE DateIdSourceRecord로 변환한다. 저장은 caller 책임이다."""
    payload = _build_disclosure_payload(record)
    return DateIdSourceRecord(
        date_id=date_id,
        fact_type=FactType.DISCLOSURE,
        source_name=record.source_name,
        source_timestamp=record.source_timestamp,
        created_at=record.as_of,
        summary=record.title,
        payload=payload,
        symbol=record.symbol,
        market=None,
        source_url=record.source_url,
    )


def _build_market_payload(point: MarketDataPoint) -> dict[str, Any]:
    """원본 payload에 정규화 필드를 병합해 canonical JSON-compatible dict를 만든다."""
    from decision.canonical_json import canonicalize_payload

    merged: dict[str, Any] = dict(point.payload)
    merged["symbol"] = point.symbol
    merged["market"] = point.market
    merged["price"] = str(point.price)
    merged["currency"] = point.currency
    return canonicalize_payload(merged)


def _build_macro_payload(point: MacroDataPoint) -> dict[str, Any]:
    """원본 payload에 정규화 필드를 병합해 canonical JSON-compatible dict를 만든다."""
    from decision.canonical_json import canonicalize_payload

    merged: dict[str, Any] = dict(point.payload)
    merged["series_id"] = point.series_id
    merged["value"] = str(point.value)
    return canonicalize_payload(merged)


def _build_disclosure_payload(record: DisclosureRecord) -> dict[str, Any]:
    """원본 payload에 정규화 필드를 병합해 canonical JSON-compatible dict를 만든다."""
    from decision.canonical_json import canonicalize_payload

    merged: dict[str, Any] = dict(record.payload)
    merged["symbol"] = record.symbol
    merged["title"] = record.title
    merged["source_url"] = record.source_url
    return canonicalize_payload(merged)
