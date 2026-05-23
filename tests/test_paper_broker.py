from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from broker import PaperBrokerAdapter
from config.settings import ExecutionMode
from domain import (
    AccountRole,
    AssetClass,
    CashSnapshot,
    Currency,
    Market,
    MarketPrice,
    Money,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from ledger import SQLiteLedger


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
SYMBOL = "005930"
INITIAL_CASH = Decimal("10000000")
PRICE = Decimal("70000")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "paper.db"


@pytest.fixture
def broker(db_path: Path) -> PaperBrokerAdapter:
    return PaperBrokerAdapter.create(
        db_path,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=INITIAL_CASH,
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
    )


def test_buy_market_decreases_cash_increases_position_and_creates_fill(
    broker: PaperBrokerAdapter,
) -> None:
    result = broker.submit_order(_market_buy(order_id="buy-1"), _market_price())

    cash = broker.get_cash(Currency.KRW, AccountRole.PAPER)
    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    fill = broker._ledger.get_fill_by_order_id("buy-1")

    assert result.status == OrderStatus.FILLED
    assert result.accepted is True
    assert cash.amount == INITIAL_CASH - (Decimal("10") * PRICE)
    assert position is not None
    assert position.quantity == Decimal("10")
    assert fill is not None


def test_buy_sets_avg_cost_to_fill_price(broker: PaperBrokerAdapter) -> None:
    broker.submit_order(_market_buy(order_id="buy-avg-1", quantity=Decimal("10")), _market_price())

    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    assert position is not None
    assert position.avg_cost == PRICE


def test_additional_buy_updates_avg_cost_as_weighted_average(broker: PaperBrokerAdapter) -> None:
    broker.submit_order(_market_buy(order_id="buy-avg-2a", quantity=Decimal("10")), _market_price())
    broker.submit_order(
        _market_buy(order_id="buy-avg-2b", quantity=Decimal("10")),
        _market_price(price=Decimal("80000")),
    )

    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    expected_avg = (Decimal("10") * PRICE + Decimal("10") * Decimal("80000")) / Decimal("20")

    assert position is not None
    assert position.avg_cost == expected_avg


def test_sell_market_decreases_position_increases_cash_and_creates_fill(
    broker: PaperBrokerAdapter,
) -> None:
    broker.submit_order(_market_buy(order_id="seed-buy"), _market_price())
    sell_price = Decimal("75000")

    result = broker.submit_order(
        _market_sell(order_id="sell-1", quantity=Decimal("4")),
        _market_price(price=sell_price),
    )

    cash = broker.get_cash(Currency.KRW, AccountRole.PAPER)
    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    fill = broker._ledger.get_fill_by_order_id("sell-1")

    assert result.status == OrderStatus.FILLED
    assert cash.amount == INITIAL_CASH - (Decimal("10") * PRICE) + (Decimal("4") * sell_price)
    assert position is not None
    assert position.quantity == Decimal("6")
    assert fill is not None


def test_sell_keeps_avg_cost_for_remaining_position(broker: PaperBrokerAdapter) -> None:
    broker.submit_order(_market_buy(order_id="seed-buy-2"), _market_price())
    before = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    assert before is not None
    sell_price = Decimal("75000")

    broker.submit_order(
        _market_sell(order_id="sell-partial"),
        _market_price(price=sell_price),
    )

    after = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    assert after is not None
    assert after.avg_cost == before.avg_cost
    assert after.market_price == sell_price


def test_full_sell_deletes_position_row(broker: PaperBrokerAdapter) -> None:
    """전량 매도 정책: quantity=0 행을 남기지 않고 current_positions 행을 삭제한다."""
    broker.submit_order(_market_buy(order_id="seed-buy-3"), _market_price())
    broker.submit_order(
        _market_sell(order_id="sell-all", quantity=Decimal("10")),
        _market_price(price=Decimal("75000")),
    )

    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    assert position is None
    assert broker.list_positions() == ()


def test_insufficient_cash_buy_is_rejected_without_fill(broker: PaperBrokerAdapter) -> None:
    result = broker.submit_order(
        _market_buy(order_id="buy-no-cash", quantity=Decimal("1000")),
        _market_price(),
    )
    fill = broker._ledger.get_fill_by_order_id("buy-no-cash")

    assert result.status == OrderStatus.REJECTED
    assert result.accepted is False
    assert result.rejection_reason == "insufficient cash"
    assert fill is None
    assert broker.get_cash(Currency.KRW, AccountRole.PAPER).amount == INITIAL_CASH


def test_insufficient_position_sell_is_rejected_without_fill(broker: PaperBrokerAdapter) -> None:
    result = broker.submit_order(
        _market_sell(order_id="sell-no-pos", quantity=Decimal("1")),
        _market_price(),
    )
    fill = broker._ledger.get_fill_by_order_id("sell-no-pos")

    assert result.status == OrderStatus.REJECTED
    assert result.accepted is False
    assert result.rejection_reason == "insufficient position quantity"
    assert fill is None


def test_buy_limit_pending_when_market_above_limit(broker: PaperBrokerAdapter) -> None:
    result = broker.submit_order(
        _limit_buy(order_id="buy-limit-pending", limit_price=Decimal("65000")),
        _market_price(price=Decimal("70000")),
    )
    fill = broker._ledger.get_fill_by_order_id("buy-limit-pending")

    assert result.status == OrderStatus.PENDING
    assert result.accepted is True
    assert fill is None
    assert broker.get_cash(Currency.KRW, AccountRole.PAPER).amount == INITIAL_CASH
    assert broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER) is None


def test_buy_limit_filled_when_market_at_or_below_limit(broker: PaperBrokerAdapter) -> None:
    """Phase 3 단순 모델: LIMIT 체결가는 limit_price가 아니라 market_price.price다."""
    market_price = Decimal("69000")
    result = broker.submit_order(
        _limit_buy(order_id="buy-limit-fill", limit_price=Decimal("70000")),
        _market_price(price=market_price),
    )
    fill = broker._ledger.get_fill_by_order_id("buy-limit-fill")
    cash = broker.get_cash(Currency.KRW, AccountRole.PAPER)
    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)

    assert result.status == OrderStatus.FILLED
    assert fill is not None
    assert fill.fill_price == market_price
    assert cash.amount == INITIAL_CASH - (Decimal("10") * market_price)
    assert position is not None
    assert position.avg_cost == market_price


def test_sell_limit_pending_when_market_below_limit(broker: PaperBrokerAdapter) -> None:
    broker.submit_order(_market_buy(order_id="seed-buy-4"), _market_price())

    result = broker.submit_order(
        _limit_sell(order_id="sell-limit-pending", limit_price=Decimal("75000")),
        _market_price(price=Decimal("74000")),
    )
    fill = broker._ledger.get_fill_by_order_id("sell-limit-pending")
    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)

    assert result.status == OrderStatus.PENDING
    assert result.accepted is True
    assert fill is None
    assert position is not None
    assert position.quantity == Decimal("10")


def test_sell_limit_filled_when_market_at_or_above_limit(broker: PaperBrokerAdapter) -> None:
    """Phase 3 단순 모델: LIMIT 체결가는 limit_price가 아니라 market_price.price다."""
    broker.submit_order(_market_buy(order_id="seed-buy-5"), _market_price())
    market_price = Decimal("76000")

    result = broker.submit_order(
        _limit_sell(order_id="sell-limit-fill", limit_price=Decimal("75000"), quantity=Decimal("4")),
        _market_price(price=market_price),
    )
    fill = broker._ledger.get_fill_by_order_id("sell-limit-fill")
    cash = broker.get_cash(Currency.KRW, AccountRole.PAPER)

    assert result.status == OrderStatus.FILLED
    assert fill is not None
    assert fill.fill_price == market_price
    assert cash.amount == INITIAL_CASH - (Decimal("10") * PRICE) + (Decimal("4") * market_price)


def test_target_weight_percent_order_is_rejected(broker: PaperBrokerAdapter) -> None:
    intent = OrderIntent(
        order_id="weight-order",
        correlation_id="corr-weight",
        symbol=SYMBOL,
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        target_weight_percent=Decimal("5"),
        created_at=NOW,
    )
    result = broker.submit_order(intent, _market_price())
    fill = broker._ledger.get_fill_by_order_id("weight-order")

    assert result.status == OrderStatus.REJECTED
    assert result.accepted is False
    assert result.rejection_reason == "target_weight_percent requires sizing before broker execution"
    assert fill is None


def test_duplicate_order_id_is_not_double_executed(broker: PaperBrokerAdapter) -> None:
    intent = _market_buy(order_id="dup-order")
    first = broker.submit_order(intent, _market_price())
    second = broker.submit_order(intent, _market_price())

    cash = broker.get_cash(Currency.KRW, AccountRole.PAPER)
    fill = broker._ledger.get_fill_by_order_id("dup-order")

    assert first.status == OrderStatus.FILLED
    assert second.status == OrderStatus.REJECTED
    assert second.rejection_reason == "duplicate order_id"
    assert cash.amount == INITIAL_CASH - (Decimal("10") * PRICE)
    assert fill is not None


def test_order_id_and_correlation_id_preserved_in_ledger(broker: PaperBrokerAdapter) -> None:
    intent = _market_buy(order_id="audit-order", correlation_id="audit-corr")
    broker.submit_order(intent, _market_price())

    stored_intent = broker._ledger.get_order_intent("audit-order")
    stored_fill = broker._ledger.get_fill_by_order_id("audit-order")

    assert stored_intent is not None
    assert stored_intent.order_id == "audit-order"
    assert stored_intent.correlation_id == "audit-corr"
    assert stored_fill is not None
    assert stored_fill.order_id == "audit-order"


def test_broker_state_restores_after_reopen(db_path: Path) -> None:
    broker = PaperBrokerAdapter.create(
        db_path,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=INITIAL_CASH,
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
    )
    broker.submit_order(_market_buy(order_id="restore-buy"), _market_price())
    broker._ledger.close()

    restored = PaperBrokerAdapter(SQLiteLedger(db_path))
    cash = restored.get_cash(Currency.KRW, AccountRole.PAPER)
    position = restored.get_position(SYMBOL, Market.KR, AccountRole.PAPER)

    assert cash.amount == INITIAL_CASH - (Decimal("10") * PRICE)
    assert position is not None
    assert position.quantity == Decimal("10")
    restored._ledger.close()


def test_symbol_mismatch_is_rejected_without_side_effects(broker: PaperBrokerAdapter) -> None:
    intent = _market_buy(order_id="mismatch-symbol", correlation_id="corr-mismatch")
    market_price = MarketPrice(
        symbol="000660",
        market=Market.KR,
        currency=Currency.KRW,
        price=PRICE,
        as_of=NOW,
    )
    result = broker.submit_order(intent, market_price)

    assert result.status == OrderStatus.REJECTED
    assert result.rejection_reason == "symbol mismatch"
    assert broker._ledger.get_fill_by_order_id("mismatch-symbol") is None
    assert broker.get_cash(Currency.KRW, AccountRole.PAPER).amount == INITIAL_CASH
    assert broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER) is None
    stored = broker._ledger.get_order_intent("mismatch-symbol")
    assert stored is not None
    assert stored.correlation_id == "corr-mismatch"


def test_market_mismatch_is_rejected_without_side_effects(broker: PaperBrokerAdapter) -> None:
    intent = _market_buy(order_id="mismatch-market")
    market_price = MarketPrice(
        symbol=SYMBOL,
        market=Market.US,
        currency=Currency.USD,
        price=Decimal("100"),
        as_of=NOW,
    )
    result = broker.submit_order(intent, market_price)

    assert result.status == OrderStatus.REJECTED
    assert result.rejection_reason == "market mismatch"
    assert broker._ledger.get_fill_by_order_id("mismatch-market") is None
    assert broker.get_cash(Currency.KRW, AccountRole.PAPER).amount == INITIAL_CASH


def test_kr_market_with_usd_currency_is_rejected(broker: PaperBrokerAdapter) -> None:
    result = broker.submit_order(
        _market_buy(order_id="mismatch-kr-usd"),
        _market_price(currency=Currency.USD),
    )

    assert result.status == OrderStatus.REJECTED
    assert result.rejection_reason == "currency mismatch"
    assert broker._ledger.get_fill_by_order_id("mismatch-kr-usd") is None


def test_us_market_with_krw_currency_is_rejected(db_path: Path) -> None:
    intent = OrderIntent(
        order_id="mismatch-us-krw",
        correlation_id="corr-us",
        symbol="AAPL",
        market=Market.US,
        asset_class=AssetClass.US_EQUITY,
        account_role=AccountRole.GENERAL,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        quantity=Decimal("1"),
        created_at=NOW,
    )
    market_price = MarketPrice(
        symbol="AAPL",
        market=Market.US,
        currency=Currency.KRW,
        price=Decimal("100"),
        as_of=NOW,
    )
    us_broker = PaperBrokerAdapter.create(
        db_path,
        initial_cash=CashSnapshot(
            currency=Currency.USD,
            amount=Decimal("10000"),
            account_role=AccountRole.GENERAL,
            as_of=NOW,
        ),
    )
    result = us_broker.submit_order(intent, market_price)

    assert result.status == OrderStatus.REJECTED
    assert result.rejection_reason == "currency mismatch"


def test_processed_order_id_with_mismatch_market_price_still_reports_duplicate(
    broker: PaperBrokerAdapter,
) -> None:
    intent = _market_buy(order_id="dup-priority")
    broker.submit_order(intent, _market_price())
    mismatch_price = MarketPrice(
        symbol="OTHER",
        market=Market.US,
        currency=Currency.USD,
        price=Decimal("1"),
        as_of=NOW,
    )
    second = broker.submit_order(intent, mismatch_price)

    assert second.status == OrderStatus.REJECTED
    assert second.rejection_reason == "duplicate order_id"


def test_buy_limit_pending_when_market_strictly_above_limit(broker: PaperBrokerAdapter) -> None:
    result = broker.submit_order(
        _limit_buy(order_id="buy-limit-pending-2", limit_price=Decimal("70000")),
        _market_price(price=Decimal("71000")),
    )

    assert result.status == OrderStatus.PENDING
    assert broker._ledger.get_fill_by_order_id("buy-limit-pending-2") is None
    assert broker.get_cash(Currency.KRW, AccountRole.PAPER).amount == INITIAL_CASH
    assert broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER) is None


def test_sell_limit_pending_when_market_strictly_below_limit(broker: PaperBrokerAdapter) -> None:
    broker.submit_order(_market_buy(order_id="seed-buy-limit-pending"), _market_price())
    result = broker.submit_order(
        _limit_sell(order_id="sell-limit-pending-2", limit_price=Decimal("70000")),
        _market_price(price=Decimal("69000")),
    )

    assert result.status == OrderStatus.PENDING
    assert broker._ledger.get_fill_by_order_id("sell-limit-pending-2") is None
    position = broker.get_position(SYMBOL, Market.KR, AccountRole.PAPER)
    assert position is not None
    assert position.quantity == Decimal("10")


def test_fill_slippage_is_money_zero_not_none(broker: PaperBrokerAdapter) -> None:
    broker.submit_order(_market_buy(order_id="slippage-check"), _market_price())
    fill = broker._ledger.get_fill_by_order_id("slippage-check")

    assert fill is not None
    assert fill.slippage is not None
    assert fill.slippage.amount == Decimal("0")
    assert fill.slippage.currency == Currency.KRW


def test_initial_cash_creates_paper_cash_ledger_row(broker: PaperBrokerAdapter) -> None:
    entries = broker._ledger.list_cash_ledger_entries(
        currency=Currency.KRW,
        account_role=AccountRole.PAPER,
    )
    assert len(entries) == 1
    assert entries[0].reason == "INITIAL_CASH"
    assert entries[0].delta_amount == INITIAL_CASH
    assert entries[0].balance_after == INITIAL_CASH


def test_buy_fill_appends_negative_cash_ledger_delta(broker: PaperBrokerAdapter) -> None:
    broker.submit_order(_market_buy(order_id="cash-ledger-buy"), _market_price())
    entries = broker._ledger.list_cash_ledger_entries(
        currency=Currency.KRW,
        account_role=AccountRole.PAPER,
    )
    buy_entries = [entry for entry in entries if entry.reason == "BUY_FILL"]

    assert len(buy_entries) == 1
    assert buy_entries[0].delta_amount == -(Decimal("10") * PRICE)
    assert buy_entries[0].balance_after == INITIAL_CASH - (Decimal("10") * PRICE)
    assert buy_entries[0].order_id == "cash-ledger-buy"


def test_sell_fill_appends_positive_cash_ledger_delta(broker: PaperBrokerAdapter) -> None:
    broker.submit_order(_market_buy(order_id="seed-cash-ledger"), _market_price())
    sell_price = Decimal("75000")
    broker.submit_order(
        _market_sell(order_id="cash-ledger-sell", quantity=Decimal("4")),
        _market_price(price=sell_price),
    )
    entries = broker._ledger.list_cash_ledger_entries(
        currency=Currency.KRW,
        account_role=AccountRole.PAPER,
    )
    sell_entries = [entry for entry in entries if entry.reason == "SELL_FILL"]

    assert len(sell_entries) == 1
    assert sell_entries[0].delta_amount == Decimal("4") * sell_price
    assert sell_entries[0].balance_after == broker.get_cash(Currency.KRW, AccountRole.PAPER).amount


def test_rejected_order_does_not_append_cash_ledger_row(broker: PaperBrokerAdapter) -> None:
    before = len(broker._ledger.list_cash_ledger_entries())
    broker.submit_order(
        _market_buy(order_id="reject-cash-ledger", quantity=Decimal("1000")),
        _market_price(),
    )
    after = len(broker._ledger.list_cash_ledger_entries())
    assert after == before


def test_pending_order_does_not_append_cash_ledger_row(broker: PaperBrokerAdapter) -> None:
    before = len(broker._ledger.list_cash_ledger_entries())
    broker.submit_order(
        _limit_buy(order_id="pending-cash-ledger", limit_price=Decimal("65000")),
        _market_price(price=Decimal("70000")),
    )
    after = len(broker._ledger.list_cash_ledger_entries())
    assert after == before


def test_duplicate_filled_order_id_does_not_add_fill_or_change_cash(
    broker: PaperBrokerAdapter,
) -> None:
    """duplicate 정책: prior FILLED result가 있으면 재제출은 duplicate REJECTED."""
    intent = _market_buy(order_id="dup-filled")
    broker.submit_order(intent, _market_price())
    cash_before = broker.get_cash(Currency.KRW, AccountRole.PAPER).amount
    second = broker.submit_order(intent, _market_price())

    assert second.rejection_reason == "duplicate order_id"
    assert broker.get_cash(Currency.KRW, AccountRole.PAPER).amount == cash_before
    assert broker._ledger.get_order_result("dup-filled") is not None
    assert broker._ledger.get_order_result("dup-filled").status == OrderStatus.FILLED


def test_duplicate_rejected_order_id_does_not_overwrite_prior_result(
    broker: PaperBrokerAdapter,
) -> None:
    """duplicate 정책: prior REJECTED result가 있어도 재제출은 duplicate REJECTED."""
    broker.submit_order(
        _market_buy(order_id="dup-rejected", quantity=Decimal("1000")),
        _market_price(),
    )
    first_result = broker._ledger.get_order_result("dup-rejected")
    assert first_result is not None
    assert first_result.status == OrderStatus.REJECTED

    second = broker.submit_order(
        _market_buy(order_id="dup-rejected", quantity=Decimal("1000")),
        _market_price(),
    )
    still = broker._ledger.get_order_result("dup-rejected")

    assert second.rejection_reason == "duplicate order_id"
    assert still == first_result


def test_duplicate_pending_order_id_does_not_reprocess(
    broker: PaperBrokerAdapter,
) -> None:
    """duplicate 정책: prior PENDING result가 있으면 재제출은 duplicate REJECTED. lifecycle은 이후 Phase."""
    intent = _limit_buy(order_id="dup-pending", limit_price=Decimal("65000"))
    first = broker.submit_order(intent, _market_price(price=Decimal("70000")))
    cash_before = broker.get_cash(Currency.KRW, AccountRole.PAPER).amount
    second = broker.submit_order(intent, _market_price(price=Decimal("60000")))

    assert first.status == OrderStatus.PENDING
    assert second.rejection_reason == "duplicate order_id"
    assert broker.get_cash(Currency.KRW, AccountRole.PAPER).amount == cash_before
    assert broker._ledger.get_fill_by_order_id("dup-pending") is None
    assert broker._ledger.get_order_result("dup-pending").status == OrderStatus.PENDING


def test_fee_calculator_called_once_for_filled_order(db_path: Path) -> None:
    call_count = 0

    def counting_fees(
        _intent: OrderIntent,
        _fill_price: Decimal,
        currency: Currency,
    ) -> tuple[Money, Money]:
        nonlocal call_count
        call_count += 1
        return Money(amount=Decimal("10"), currency=currency), Money.zero(currency)

    broker = PaperBrokerAdapter.create(
        db_path,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=INITIAL_CASH,
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
        fee_calculator=counting_fees,
    )
    broker.submit_order(_market_buy(order_id="fee-once"), _market_price())

    assert call_count == 1
    fill = broker._ledger.get_fill_by_order_id("fee-once")
    assert fill is not None
    assert fill.commission.amount == Decimal("10")


def test_fee_calculator_not_called_for_mismatch_duplicate_or_target_weight(
    db_path: Path,
) -> None:
    call_count = 0

    def counting_fees(
        _intent: OrderIntent,
        _fill_price: Decimal,
        currency: Currency,
    ) -> tuple[Money, Money]:
        nonlocal call_count
        call_count += 1
        return Money.zero(currency), Money.zero(currency)

    broker = PaperBrokerAdapter.create(
        db_path,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=INITIAL_CASH,
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
        fee_calculator=counting_fees,
    )

    broker.submit_order(
        _market_buy(order_id="fee-mismatch"),
        _market_price(currency=Currency.USD),
    )
    broker.submit_order(
        OrderIntent(
            order_id="fee-weight",
            correlation_id="corr-weight",
            symbol=SYMBOL,
            market=Market.KR,
            asset_class=AssetClass.KR_EQUITY,
            account_role=AccountRole.PAPER,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            execution_mode=ExecutionMode.NORMAL,
            target_weight_percent=Decimal("5"),
            created_at=NOW,
        ),
        _market_price(),
    )
    filled_intent = _market_buy(order_id="fee-dup")
    broker.submit_order(filled_intent, _market_price())
    broker.submit_order(filled_intent, _market_price())

    assert call_count == 1


def test_fee_calculator_once_for_insufficient_cash_buy(db_path: Path) -> None:
    call_count = 0

    def counting_fees(
        _intent: OrderIntent,
        _fill_price: Decimal,
        currency: Currency,
    ) -> tuple[Money, Money]:
        nonlocal call_count
        call_count += 1
        return Money(amount=Decimal("100"), currency=currency), Money.zero(currency)

    broker = PaperBrokerAdapter.create(
        db_path,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=INITIAL_CASH,
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
        fee_calculator=counting_fees,
    )
    result = broker.submit_order(
        _market_buy(order_id="fee-insufficient-cash", quantity=Decimal("1000")),
        _market_price(),
    )

    assert result.rejection_reason == "insufficient cash"
    assert call_count == 1


def test_fee_calculator_zero_for_insufficient_position_sell(db_path: Path) -> None:
    call_count = 0

    def counting_fees(
        _intent: OrderIntent,
        _fill_price: Decimal,
        currency: Currency,
    ) -> tuple[Money, Money]:
        nonlocal call_count
        call_count += 1
        return Money.zero(currency), Money.zero(currency)

    broker = PaperBrokerAdapter.create(
        db_path,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=INITIAL_CASH,
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
        fee_calculator=counting_fees,
    )
    result = broker.submit_order(
        _market_sell(order_id="fee-insufficient-sell", quantity=Decimal("1")),
        _market_price(),
    )

    assert result.rejection_reason == "insufficient position quantity"
    assert call_count == 0


def test_fee_calculator_zero_for_limit_pending_orders(db_path: Path) -> None:
    call_count = 0

    def counting_fees(
        _intent: OrderIntent,
        _fill_price: Decimal,
        currency: Currency,
    ) -> tuple[Money, Money]:
        nonlocal call_count
        call_count += 1
        return Money.zero(currency), Money.zero(currency)

    broker = PaperBrokerAdapter.create(
        db_path,
        initial_cash=CashSnapshot(
            currency=Currency.KRW,
            amount=INITIAL_CASH,
            account_role=AccountRole.PAPER,
            as_of=NOW,
        ),
        fee_calculator=counting_fees,
    )
    buy_pending = broker.submit_order(
        _limit_buy(order_id="fee-pending-buy", limit_price=Decimal("65000")),
        _market_price(price=Decimal("70000")),
    )
    with broker._ledger.transaction():
        broker._ledger.upsert_position(
            Position(
                symbol=SYMBOL,
                market=Market.KR,
                asset_class=AssetClass.KR_EQUITY,
                account_role=AccountRole.PAPER,
                quantity=Decimal("10"),
                avg_cost=PRICE,
                currency=Currency.KRW,
                market_price=PRICE,
            )
        )
    sell_pending = broker.submit_order(
        _limit_sell(order_id="fee-pending-sell", limit_price=Decimal("75000")),
        _market_price(price=Decimal("74000")),
    )

    assert buy_pending.status == OrderStatus.PENDING
    assert sell_pending.status == OrderStatus.PENDING
    assert call_count == 0


def test_cash_ledger_delta_sum_equals_current_cash_after_broker_fills(
    broker: PaperBrokerAdapter,
) -> None:
    broker.submit_order(_market_buy(order_id="ledger-sum-buy-1"), _market_price())
    broker.submit_order(
        _market_buy(order_id="ledger-sum-buy-2", quantity=Decimal("5")),
        _market_price(price=Decimal("80000")),
    )
    broker.submit_order(
        _market_sell(order_id="ledger-sum-sell", quantity=Decimal("3")),
        _market_price(price=Decimal("75000")),
    )

    cash = broker.get_cash(Currency.KRW, AccountRole.PAPER)
    entries = broker._ledger.list_cash_ledger_entries(
        currency=Currency.KRW,
        account_role=AccountRole.PAPER,
    )
    delta_sum = sum(entry.delta_amount for entry in entries)

    assert delta_sum == cash.amount


def _market_price(
    *,
    price: Decimal = PRICE,
    currency: Currency = Currency.KRW,
    symbol: str = SYMBOL,
    market: Market = Market.KR,
) -> MarketPrice:
    return MarketPrice(
        symbol=symbol,
        market=market,
        currency=currency,
        price=price,
        as_of=NOW,
    )


def _market_buy(
    *,
    order_id: str,
    quantity: Decimal = Decimal("10"),
    correlation_id: str = "corr-buy",
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        correlation_id=correlation_id,
        symbol=SYMBOL,
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        quantity=quantity,
        created_at=NOW,
    )


def _market_sell(
    *,
    order_id: str,
    quantity: Decimal = Decimal("4"),
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        correlation_id="corr-sell",
        symbol=SYMBOL,
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        quantity=quantity,
        created_at=NOW,
    )


def _limit_buy(*, order_id: str, limit_price: Decimal) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        correlation_id="corr-limit-buy",
        symbol=SYMBOL,
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        execution_mode=ExecutionMode.NORMAL,
        quantity=Decimal("10"),
        limit_price=limit_price,
        created_at=NOW,
    )


def _limit_sell(
    *,
    order_id: str,
    limit_price: Decimal,
    quantity: Decimal = Decimal("4"),
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        correlation_id="corr-limit-sell",
        symbol=SYMBOL,
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        execution_mode=ExecutionMode.NORMAL,
        quantity=quantity,
        limit_price=limit_price,
        created_at=NOW,
    )
