from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config.settings import ExecutionMode
from domain import (
    AccountRole,
    AssetClass,
    CashSnapshot,
    Currency,
    Fill,
    Market,
    NavSnapshot,
    OrderIntent,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from domain.money import Money
from ledger import SQLiteLedger


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)

EXPECTED_TABLES = {
    "current_cash",
    "current_positions",
    "fills",
    "nav_snapshots",
    "order_intents",
    "order_results",
    "paper_cash_ledger",
}


def test_sqlite_ledger_creates_required_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)

    assert EXPECTED_TABLES.issubset(set(ledger.list_tables()))
    ledger.close()


def test_cash_mutation_public_api_is_apply_cash_change_only(tmp_path: Path) -> None:
    """cash 변경 public backdoor가 없고 apply_cash_change()만 노출되는지 확인한다."""
    ledger = SQLiteLedger(tmp_path / "ledger.db")

    assert not hasattr(ledger, "upsert_cash")
    assert not hasattr(ledger, "append_cash_ledger_entry")

    with ledger.transaction():
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("1000"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id=None,
            correlation_id=None,
            delta_amount=Decimal("1000"),
            reason="INITIAL_CASH",
        )

    cash = ledger.get_cash(Currency.KRW, AccountRole.PAPER)
    entries = ledger.list_cash_ledger_entries()
    assert cash is not None
    assert cash.amount == Decimal("1000")
    assert len(entries) == 1
    assert isinstance(entries[0].delta_amount, Decimal)
    assert entries[0].balance_after == Decimal("1000")
    ledger.close()


def test_money_decimal_datetime_enum_round_trip_through_domain_models(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)

    intent = _order_intent(order_id="order-roundtrip")
    with ledger.transaction():
        ledger.save_order_intent(intent)
        ledger.save_order_result(
            OrderResult(
                order_id="order-roundtrip",
                status=OrderStatus.FILLED,
                accepted=True,
                created_at=NOW,
            )
        )
        ledger.save_fill(_fill(order_id="order-roundtrip", fill_id="fill-roundtrip"))

    restored_intent = ledger.get_order_intent("order-roundtrip")
    restored_result = ledger.get_order_result("order-roundtrip")
    restored_fill = ledger.get_fill_by_order_id("order-roundtrip")

    assert restored_intent == intent
    assert restored_result is not None
    assert restored_result.status == OrderStatus.FILLED
    assert restored_fill is not None
    assert restored_fill.quantity == Decimal("10")
    assert restored_fill.commission.amount == Decimal("0")
    ledger.close()


def test_initial_cash_persist_and_restore(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)
    cash = CashSnapshot(
        currency=Currency.KRW,
        amount=Decimal("10000000"),
        account_role=AccountRole.PAPER,
        as_of=NOW,
    )
    with ledger.transaction():
        ledger.apply_cash_change(
            cash,
            order_id=None,
            correlation_id=None,
            delta_amount=cash.amount,
            reason="INITIAL_CASH",
        )

    restored = ledger.get_cash(Currency.KRW, AccountRole.PAPER)
    entries = ledger.list_cash_ledger_entries(
        currency=Currency.KRW,
        account_role=AccountRole.PAPER,
    )
    assert restored == cash
    assert len(entries) == 1
    assert entries[0].reason == "INITIAL_CASH"
    assert entries[0].delta_amount == Decimal("10000000")
    assert entries[0].balance_after == Decimal("10000000")
    ledger.close()


def test_position_persist_and_restore(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)
    position = Position(
        symbol="005930",
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        quantity=Decimal("10"),
        avg_cost=Decimal("70000"),
        currency=Currency.KRW,
        market_price=Decimal("71000"),
    )
    with ledger.transaction():
        ledger.upsert_position(position)

    restored = ledger.get_position("005930", Market.KR, AccountRole.PAPER)
    listed = ledger.list_positions()

    assert restored == position
    assert listed == (position,)
    ledger.close()


def test_order_intent_result_fill_records_are_queryable(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)
    intent = _order_intent(order_id="order-record")
    result = OrderResult(
        order_id="order-record",
        status=OrderStatus.PENDING,
        accepted=True,
        created_at=NOW,
    )
    fill = _fill(order_id="order-record", fill_id="fill-record")

    with ledger.transaction():
        ledger.save_order_intent(intent)
        ledger.save_order_result(result)
        ledger.save_fill(fill)

    assert ledger.get_order_intent("order-record") == intent
    assert ledger.get_order_result("order-record") == result
    assert ledger.get_fill_by_order_id("order-record") == fill
    ledger.close()


def test_has_processed_order_detects_duplicate_order_id(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)
    intent = _order_intent(order_id="order-dup")

    assert ledger.has_processed_order("order-dup") is False

    with ledger.transaction():
        ledger.save_order_intent(intent)
        ledger.save_order_result(
            OrderResult(
                order_id="order-dup",
                status=OrderStatus.FILLED,
                accepted=True,
                created_at=NOW,
            )
        )

    assert ledger.has_processed_order("order-dup") is True
    ledger.close()


def test_cash_and_position_restore_after_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"

    ledger = SQLiteLedger(db_path)
    with ledger.transaction():
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("5000000"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id=None,
            correlation_id=None,
            delta_amount=Decimal("5000000"),
            reason="INITIAL_CASH",
        )
        ledger.upsert_position(
            Position(
                symbol="AAPL",
                market=Market.US,
                asset_class=AssetClass.US_EQUITY,
                account_role=AccountRole.US_REGULAR,
                quantity=Decimal("3"),
                avg_cost=Decimal("150"),
                currency=Currency.USD,
            )
        )
    ledger.close()

    reopened = SQLiteLedger(db_path)
    cash = reopened.get_cash(Currency.KRW, AccountRole.PAPER)
    position = reopened.get_position("AAPL", Market.US, AccountRole.US_REGULAR)
    entries = reopened.list_cash_ledger_entries()

    assert cash is not None
    assert cash.amount == Decimal("5000000")
    assert position is not None
    assert position.quantity == Decimal("3")
    assert len(entries) == 1
    reopened.close()


def test_cash_ledger_entries_restore_after_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)
    with ledger.transaction():
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("1100"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id=None,
            correlation_id=None,
            delta_amount=Decimal("1100"),
            reason="INITIAL_CASH",
        )
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("1000"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id="order-1",
            correlation_id="corr-1",
            delta_amount=Decimal("-100"),
            reason="BUY_FILL",
        )
    ledger.close()

    reopened = SQLiteLedger(db_path)
    entries = reopened.list_cash_ledger_entries()
    assert len(entries) == 2
    assert entries[1].order_id == "order-1"
    assert entries[1].correlation_id == "corr-1"
    assert entries[1].delta_amount == Decimal("-100")
    assert entries[1].balance_after == Decimal("1000")
    reopened.close()


def test_apply_cash_change_updates_projection_and_ledger_in_one_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)

    with ledger.transaction():
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("1000"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id=None,
            correlation_id=None,
            delta_amount=Decimal("1000"),
            reason="INITIAL_CASH",
        )
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("900"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id="order-tx",
            correlation_id="corr-tx",
            delta_amount=Decimal("-100"),
            reason="BUY_FILL",
        )

    cash = ledger.get_cash(Currency.KRW, AccountRole.PAPER)
    entries = ledger.list_cash_ledger_entries()
    assert cash is not None
    assert cash.amount == Decimal("900")
    assert entries[1].balance_after == Decimal("900")
    ledger.close()


def test_apply_cash_change_rejects_delta_balance_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)

    with ledger.transaction():
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("100000"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id=None,
            correlation_id=None,
            delta_amount=Decimal("100000"),
            reason="INITIAL_CASH",
        )

    with pytest.raises(ValueError, match="cash ledger balance mismatch"):
        with ledger.transaction():
            ledger.apply_cash_change(
                CashSnapshot(
                    currency=Currency.KRW,
                    amount=Decimal("90000"),
                    account_role=AccountRole.PAPER,
                    as_of=NOW,
                ),
                order_id="bad-delta",
                correlation_id="corr-bad",
                delta_amount=Decimal("-30000"),
                reason="BUY_FILL",
            )

    cash = ledger.get_cash(Currency.KRW, AccountRole.PAPER)
    entries = ledger.list_cash_ledger_entries()
    assert cash is not None
    assert cash.amount == Decimal("100000")
    assert len(entries) == 1
    ledger.close()


def test_cash_ledger_delta_sum_equals_current_cash(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)

    with ledger.transaction():
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("100000"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id=None,
            correlation_id=None,
            delta_amount=Decimal("100000"),
            reason="INITIAL_CASH",
        )
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("70000"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id="buy-1",
            correlation_id="corr-1",
            delta_amount=Decimal("-30000"),
            reason="BUY_FILL",
        )
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("50000"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id="buy-2",
            correlation_id="corr-2",
            delta_amount=Decimal("-20000"),
            reason="BUY_FILL",
        )
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("60000"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id="sell-1",
            correlation_id="corr-3",
            delta_amount=Decimal("10000"),
            reason="SELL_FILL",
        )

    cash = ledger.get_cash(Currency.KRW, AccountRole.PAPER)
    entries = ledger.list_cash_ledger_entries(
        currency=Currency.KRW,
        account_role=AccountRole.PAPER,
    )
    delta_sum = sum(entry.delta_amount for entry in entries)

    assert cash is not None
    assert cash.amount == Decimal("60000")
    assert delta_sum == Decimal("60000")
    ledger.close()


def test_apply_cash_change_rolls_back_on_transaction_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)

    with pytest.raises(RuntimeError, match="force rollback"):
        with ledger.transaction():
            ledger.apply_cash_change(
                CashSnapshot(
                    currency=Currency.KRW,
                    amount=Decimal("50000"),
                    account_role=AccountRole.PAPER,
                    as_of=NOW,
                ),
                order_id=None,
                correlation_id=None,
                delta_amount=Decimal("50000"),
                reason="INITIAL_CASH",
            )
            raise RuntimeError("force rollback")

    assert ledger.get_cash(Currency.KRW, AccountRole.PAPER) is None
    assert ledger.list_cash_ledger_entries() == ()
    ledger.close()


def test_apply_cash_change_rollback_preserves_existing_cash(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)

    with ledger.transaction():
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("100000"),
                account_role=AccountRole.PAPER,
                as_of=NOW,
            ),
            order_id=None,
            correlation_id=None,
            delta_amount=Decimal("100000"),
            reason="INITIAL_CASH",
        )

    with pytest.raises(RuntimeError, match="force rollback"):
        with ledger.transaction():
            ledger.apply_cash_change(
                CashSnapshot(
                    currency=Currency.KRW,
                    amount=Decimal("70000"),
                    account_role=AccountRole.PAPER,
                    as_of=NOW,
                ),
                order_id="rollback-buy",
                correlation_id="corr-rollback",
                delta_amount=Decimal("-30000"),
                reason="BUY_FILL",
            )
            raise RuntimeError("force rollback")

    cash = ledger.get_cash(Currency.KRW, AccountRole.PAPER)
    entries = ledger.list_cash_ledger_entries()
    assert cash is not None
    assert cash.amount == Decimal("100000")
    assert len(entries) == 1
    ledger.close()


def test_nav_snapshot_store_only(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)
    snapshot = NavSnapshot(
        snapshot_id="nav-1",
        as_of=NOW,
        total_nav_krw=Decimal("10000000"),
        cash_krw=Decimal("2000000"),
        invested_krw=Decimal("8000000"),
    )
    with ledger.transaction():
        ledger.save_nav_snapshot(snapshot)
    ledger.close()


def test_restore_rejects_non_finite_decimal(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)
    with ledger.transaction():
        ledger._conn.execute(
            """
            INSERT INTO current_cash (currency, account_role, amount, as_of)
            VALUES (?, ?, ?, ?)
            """,
            ("KRW", "PAPER", "Infinity", NOW.isoformat()),
        )

    with pytest.raises(ValueError, match="amount must be a finite decimal"):
        ledger.get_cash(Currency.KRW, AccountRole.PAPER)
    ledger.close()


def test_sqlite_ledger_preserves_semantic_account_role(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_path)
    with ledger.transaction():
        ledger.apply_cash_change(
            CashSnapshot(
                currency=Currency.KRW,
                amount=Decimal("1000000"),
                account_role=AccountRole.KR_TAX_ADVANTAGED,
                as_of=NOW,
            ),
            order_id=None,
            correlation_id=None,
            delta_amount=Decimal("1000000"),
            reason="INITIAL_CASH",
        )
        ledger.upsert_position(
            Position(
                symbol="005930",
                market=Market.KR,
                asset_class=AssetClass.KR_EQUITY,
                account_role=AccountRole.KR_TAX_ADVANTAGED,
                quantity=Decimal("10"),
                avg_cost=Decimal("70000"),
                currency=Currency.KRW,
            )
        )
        ledger.upsert_position(
            Position(
                symbol="AAPL",
                market=Market.US,
                asset_class=AssetClass.US_EQUITY,
                account_role=AccountRole.US_REGULAR,
                quantity=Decimal("2"),
                avg_cost=Decimal("150"),
                currency=Currency.USD,
            )
        )
    ledger.close()

    reopened = SQLiteLedger(db_path)
    kr_cash = reopened.get_cash(Currency.KRW, AccountRole.KR_TAX_ADVANTAGED)
    kr_position = reopened.get_position("005930", Market.KR, AccountRole.KR_TAX_ADVANTAGED)
    us_position = reopened.get_position("AAPL", Market.US, AccountRole.US_REGULAR)

    assert kr_cash is not None
    assert kr_cash.amount == Decimal("1000000")
    assert kr_position is not None
    assert kr_position.quantity == Decimal("10")
    assert us_position is not None
    assert us_position.quantity == Decimal("2")
    reopened.close()


def _order_intent(*, order_id: str = "order-1") -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        correlation_id="corr-1",
        symbol="005930",
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        quantity=Decimal("10"),
        created_at=NOW,
    )


def _fill(*, order_id: str, fill_id: str) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        symbol="005930",
        market=Market.KR,
        side=OrderSide.BUY,
        quantity=Decimal("10"),
        fill_price=Decimal("70000"),
        commission=Money.zero(Currency.KRW),
        tax=Money.zero(Currency.KRW),
        filled_at=NOW,
    )
