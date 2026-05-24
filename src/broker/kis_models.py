from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain._datetime import require_timezone_aware_datetime
from domain._decimal import to_decimal, to_optional_decimal
from domain._strings import normalize_required_string
from domain.enums import AccountRole, AssetClass, Currency, Market
from domain.money import Money


class KisClientError(RuntimeError):
    """KIS read-only 클라이언트 공통 오류."""


class KisCredentialError(KisClientError):
    """필수 KIS 자격증명 또는 계좌 환경변수가 없을 때 발생한다."""


class KisAccountRoleError(KisClientError):
    """KIS live 경로에서 허용되지 않는 AccountRole일 때 발생한다."""


class KisHttpError(KisClientError):
    """KIS HTTP 응답이 2xx가 아니거나 파싱에 실패했을 때 발생한다."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class IsaSupportStatus(StrEnum):
    """ISA read-only smoke 결과 상태."""

    UNKNOWN = "UNKNOWN"
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    SKIPPED = "SKIPPED"


class KisAccessToken(BaseModel):
    """KIS OAuth access token 스냅샷."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: str
    token_type: str | None = None
    expires_at: datetime | None = None
    raw_expires_in_seconds: int | None = None

    @field_validator("access_token", mode="before")
    @classmethod
    def validate_access_token(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="access_token")

    @field_validator("expires_at", mode="before")
    @classmethod
    def validate_expires_at(cls, value: Any) -> datetime | None:
        if value is None:
            return None
        return require_timezone_aware_datetime(value, field_name="expires_at")


class KisAccountRef(BaseModel):
    """KIS 계좌 참조. 실제 계좌번호는 마스킹된 형태만 보관한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_role: AccountRole
    account_env_var: str
    account_number_masked: str

    @field_validator("account_env_var", "account_number_masked", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @model_validator(mode="after")
    def reject_paper_role(self) -> KisAccountRef:
        if self.account_role == AccountRole.PAPER:
            raise ValueError("AccountRole.PAPER is not valid for KIS live account mapping.")
        return self


class KisBalanceSnapshot(BaseModel):
    """KIS 잔고 조회 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_role: AccountRole
    currency: Currency
    cash: Money
    as_of: datetime
    raw_payload_hash: str | None = None

    @field_validator("as_of", mode="before")
    @classmethod
    def validate_as_of(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="as_of")


class KisPositionSnapshot(BaseModel):
    """KIS 보유 종목 스냅샷."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    market: Market
    account_role: AccountRole
    asset_class: AssetClass
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
        return to_decimal(value, field_name="kis_position_decimal")

    @field_validator("market_price", mode="before")
    @classmethod
    def validate_market_price(cls, value: Any) -> Decimal | None:
        return to_optional_decimal(value, field_name="market_price")


class KisOrderbookSnapshot(BaseModel):
    """KIS 호가 스냅샷."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    market: Market
    bid1: Decimal = Field(gt=Decimal("0"))
    ask1: Decimal = Field(gt=Decimal("0"))
    as_of: datetime

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="symbol")

    @field_validator("bid1", "ask1", mode="before")
    @classmethod
    def validate_prices(cls, value: Any) -> Decimal:
        return to_decimal(value, field_name="orderbook_price")

    @field_validator("as_of", mode="before")
    @classmethod
    def validate_as_of(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="as_of")


class KisReadOnlySmokeResult(BaseModel):
    """KIS read-only smoke-check 결과."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token_ok: bool
    balance_ok: bool
    quote_ok: bool
    orderbook_ok: bool
    isa_support_status: IsaSupportStatus
    errors: tuple[str, ...] = ()
    checked_at: datetime

    @field_validator("checked_at", mode="before")
    @classmethod
    def validate_checked_at(cls, value: Any) -> datetime:
        return require_timezone_aware_datetime(value, field_name="checked_at")


def mask_account_number(account_number: str) -> str:
    """계좌번호를 로그/표시용으로 마스킹한다. 원본은 반환하지 않는다."""

    normalized = account_number.strip()
    if len(normalized) <= 4:
        return "****"
    return f"{normalized[:2]}{'*' * (len(normalized) - 4)}{normalized[-2:]}"


__all__ = [
    "IsaSupportStatus",
    "KisAccessToken",
    "KisAccountRef",
    "KisAccountRoleError",
    "KisBalanceSnapshot",
    "KisClientError",
    "KisCredentialError",
    "KisHttpError",
    "KisOrderbookSnapshot",
    "KisPositionSnapshot",
    "KisReadOnlySmokeResult",
    "mask_account_number",
]
