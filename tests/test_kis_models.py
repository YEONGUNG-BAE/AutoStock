from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from broker.kis_models import (
    IsaSupportStatus,
    KisAccessToken,
    KisAccountRef,
    KisAccountRoleError,
    KisBalanceSnapshot,
    KisOrderbookSnapshot,
    KisPositionSnapshot,
    KisReadOnlySmokeResult,
    mask_account_number,
)
from domain.enums import AccountRole, AssetClass, Currency, Market
from domain.money import Money


def test_mask_account_number_short() -> None:
    assert mask_account_number("1234") == "****"


def test_mask_account_number_normal() -> None:
    masked = mask_account_number("123456789012")
    assert masked.startswith("12")
    assert masked.endswith("12")
    assert "*" in masked
    assert "123456789012" not in masked


def test_kis_account_ref_rejects_paper() -> None:
    with pytest.raises(ValueError, match="PAPER"):
        KisAccountRef(
            account_role=AccountRole.PAPER,
            account_env_var="KIS_ISA_ACCOUNT",
            account_number_masked="12****12",
        )


def test_kis_access_token_requires_non_blank_token() -> None:
    with pytest.raises(ValueError):
        KisAccessToken(access_token="")


def test_kis_balance_snapshot_uses_decimal() -> None:
    snapshot = KisBalanceSnapshot(
        account_role=AccountRole.KR_TAX_ADVANTAGED,
        currency=Currency.KRW,
        cash=Money(amount=Decimal("1000000"), currency=Currency.KRW),
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert snapshot.cash.amount == Decimal("1000000")


def test_kis_position_snapshot_uses_decimal() -> None:
    snapshot = KisPositionSnapshot(
        symbol="005930",
        market=Market.KR,
        account_role=AccountRole.KR_TAX_ADVANTAGED,
        asset_class=AssetClass.KR_EQUITY,
        quantity=Decimal("10"),
        avg_cost=Decimal("70000"),
        currency=Currency.KRW,
        market_price=Decimal("71000"),
    )
    assert snapshot.quantity == Decimal("10")


def test_kis_orderbook_snapshot_validates_positive_prices() -> None:
    with pytest.raises(ValueError):
        KisOrderbookSnapshot(
            symbol="005930",
            market=Market.KR,
            bid1=Decimal("0"),
            ask1=Decimal("71000"),
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_kis_read_only_smoke_result_defaults() -> None:
    result = KisReadOnlySmokeResult(
        token_ok=True,
        balance_ok=True,
        quote_ok=True,
        orderbook_ok=True,
        isa_support_status=IsaSupportStatus.SUPPORTED,
        checked_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert result.errors == ()


def test_account_role_error_is_exception() -> None:
    assert issubclass(KisAccountRoleError, Exception)
