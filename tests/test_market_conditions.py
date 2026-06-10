from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from domain import Currency
from domain.enums import Market
from market_data.conditions import (
    Comparator,
    ConditionClause,
    Metric,
    evaluate_all,
    evaluate_clause,
    metric_value,
    rule_required_slots,
)
from market_data.latest_state import LatestMarketStateSnapshot
from market_data.models import (
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _quote(*, bid: str = "99", ask: str = "101") -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        bid_price=bid,
        ask_price=ask,
        bid_quantity="10",
        ask_quantity="10",
        quote_at=NOW,
        received_at=NOW,
        provider_sequence=ProviderSequence(
            provider="kis", channel="quote", sequence=1, received_at=NOW
        ),
    )


def _trade(*, price: str = "100") -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        price=price,
        quantity="1",
        trade_at=NOW,
        received_at=NOW,
        provider_sequence=ProviderSequence(
            provider="kis", channel="trade", sequence=1, received_at=NOW
        ),
    )


def _snap(
    *,
    trade: NormalizedTradeTick | None = None,
    quote: NormalizedBestBidAsk | None = None,
    trade_fresh: bool = True,
    quote_fresh: bool = True,
) -> LatestMarketStateSnapshot:
    return LatestMarketStateSnapshot(
        market=Market.KR,
        symbol="005930",
        trade=trade,
        quote=quote,
        trade_fresh=trade_fresh,
        quote_fresh=quote_fresh,
        evaluated_at=NOW,
    )


def _clause(metric: Metric, comparator: Comparator, threshold: str) -> ConditionClause:
    return ConditionClause(metric=metric, comparator=comparator, threshold=threshold)


def test_metric_value_reads_trade_and_quote_slots() -> None:
    snap = _snap(trade=_trade(price="100"), quote=_quote(bid="99", ask="101"))
    assert metric_value(Metric.LAST_TRADE_PRICE, snap) == Decimal("100")
    assert metric_value(Metric.BEST_BID_PRICE, snap) == Decimal("99")
    assert metric_value(Metric.BEST_ASK_PRICE, snap) == Decimal("101")


def test_metric_value_spread_bps_uses_mid() -> None:
    # bid=99, ask=101 → mid=100, spread=2 → 2/100*10000 = 200bps
    snap = _snap(quote=_quote(bid="99", ask="101"))
    assert metric_value(Metric.SPREAD_BPS, snap) == Decimal("200")


def test_metric_value_returns_none_when_slot_missing() -> None:
    no_trade = _snap(quote=_quote())
    assert metric_value(Metric.LAST_TRADE_PRICE, no_trade) is None
    no_quote = _snap(trade=_trade())
    assert metric_value(Metric.BEST_BID_PRICE, no_quote) is None
    assert metric_value(Metric.SPREAD_BPS, no_quote) is None


def test_evaluate_clause_lte_and_gte_boundary_inclusive() -> None:
    snap = _snap(trade=_trade(price="100"))
    assert evaluate_clause(_clause(Metric.LAST_TRADE_PRICE, Comparator.LTE, "100"), snap) is True
    assert evaluate_clause(_clause(Metric.LAST_TRADE_PRICE, Comparator.GTE, "100"), snap) is True
    assert evaluate_clause(_clause(Metric.LAST_TRADE_PRICE, Comparator.LTE, "99"), snap) is False
    assert evaluate_clause(_clause(Metric.LAST_TRADE_PRICE, Comparator.GTE, "101"), snap) is False


def test_evaluate_clause_missing_value_is_fail_closed_false() -> None:
    snap = _snap(quote=_quote())  # no trade slot
    clause = _clause(Metric.LAST_TRADE_PRICE, Comparator.LTE, "100")
    assert evaluate_clause(clause, snap) is False


def test_evaluate_all_requires_every_clause_true() -> None:
    snap = _snap(trade=_trade(price="100"), quote=_quote(bid="99", ask="101"))
    rules = (
        _clause(Metric.LAST_TRADE_PRICE, Comparator.LTE, "100"),
        _clause(Metric.BEST_ASK_PRICE, Comparator.LTE, "101"),
    )
    assert evaluate_all(rules, snap) is True
    rules_one_false = (
        _clause(Metric.LAST_TRADE_PRICE, Comparator.LTE, "100"),
        _clause(Metric.BEST_ASK_PRICE, Comparator.LTE, "100"),
    )
    assert evaluate_all(rules_one_false, snap) is False


def test_evaluate_all_empty_is_false() -> None:
    assert evaluate_all((), _snap(quote=_quote())) is False


def test_rule_required_slots() -> None:
    trade_only = (_clause(Metric.LAST_TRADE_PRICE, Comparator.LTE, "100"),)
    quote_only = (_clause(Metric.SPREAD_BPS, Comparator.LTE, "50"),)
    both = trade_only + quote_only
    assert rule_required_slots(trade_only) == (True, False)
    assert rule_required_slots(quote_only) == (False, True)
    assert rule_required_slots(both) == (True, True)


def test_spread_bps_threshold_allows_zero() -> None:
    clause = _clause(Metric.SPREAD_BPS, Comparator.LTE, "0")
    assert clause.threshold == Decimal("0")


def test_negative_spread_threshold_rejected() -> None:
    with pytest.raises(ValidationError):
        _clause(Metric.SPREAD_BPS, Comparator.LTE, "-1")


def test_nonpositive_price_threshold_rejected() -> None:
    with pytest.raises(ValidationError):
        _clause(Metric.LAST_TRADE_PRICE, Comparator.LTE, "0")


def test_invalid_threshold_type_rejected() -> None:
    with pytest.raises(ValidationError):
        ConditionClause(metric=Metric.LAST_TRADE_PRICE, comparator=Comparator.LTE, threshold=1.5)


def test_condition_clause_is_frozen_and_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        ConditionClause(
            metric=Metric.LAST_TRADE_PRICE,
            comparator=Comparator.LTE,
            threshold="100",
            extra="x",
        )
