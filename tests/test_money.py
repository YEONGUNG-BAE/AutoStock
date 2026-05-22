from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain import Currency, Money


def test_money_from_int_uses_decimal() -> None:
    money = Money.from_int(1000, Currency.USD)

    assert money.amount == Decimal("1000")
    assert money.currency == Currency.USD


def test_money_allows_negative_amount_for_ledger_delta() -> None:
    money = Money.from_str("-500.25", Currency.KRW)

    assert money.amount == Decimal("-500.25")


def test_money_rejects_non_finite_amount() -> None:
    with pytest.raises(ValidationError, match="amount must be a finite decimal"):
        Money(amount=Decimal("NaN"), currency=Currency.KRW)
