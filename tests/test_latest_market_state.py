"""RTM-2 — latest market-state store tests (network/broker/ledger-free)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domain.enums import Currency, Market
from market_data.latest_state import (
    ApplyStatus,
    FutureMarketEventError,
    LatestMarketStateStore,
    MarketStateFreshnessPolicy,
    MissingMarketStateError,
    StaleMarketStateError,
)
from market_data.models import (
    MarketEventType,
    MarketHeartbeat,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)

_BASE = datetime(2026, 6, 8, 0, 5, 0, tzinfo=UTC)


def _seq(sequence: int, *, provider: str = "kis", channel: str = "H0STCNT0|005930", at: datetime = _BASE) -> ProviderSequence:
    return ProviderSequence(provider=provider, channel=channel, sequence=sequence, received_at=at)


def _trade(
    *,
    sequence: int = 1,
    market: Market = Market.KR,
    symbol: str = "005930",
    provider: str = "kis",
    channel: str = "H0STCNT0|005930",
    trade_at: datetime = _BASE,
    received_at: datetime = _BASE,
    price: str = "70000",
) -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider=provider,
        symbol=symbol,
        market=market,
        currency=Currency.KRW if market is Market.KR else Currency.USD,
        price=Decimal(price),
        quantity=Decimal("10"),
        trade_at=trade_at,
        received_at=received_at,
        provider_sequence=_seq(sequence, provider=provider, channel=channel, at=received_at),
    )


def _quote(
    *,
    sequence: int = 1,
    market: Market = Market.KR,
    symbol: str = "005930",
    provider: str = "kis",
    channel: str = "H0STASP0|005930",
    quote_at: datetime = _BASE,
    received_at: datetime = _BASE,
) -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider=provider,
        symbol=symbol,
        market=market,
        currency=Currency.KRW if market is Market.KR else Currency.USD,
        bid_price=Decimal("69900"),
        ask_price=Decimal("70000"),
        bid_quantity=Decimal("100"),
        ask_quantity=Decimal("80"),
        quote_at=quote_at,
        received_at=received_at,
        provider_sequence=_seq(sequence, provider=provider, channel=channel, at=received_at),
    )


def _heartbeat(
    *,
    provider: str = "kis",
    channel: str = "PINGPONG",
    sent_at: datetime = _BASE,
    received_at: datetime = _BASE,
) -> MarketHeartbeat:
    return MarketHeartbeat(provider=provider, channel=channel, sent_at=sent_at, received_at=received_at)


def _now(seconds: float) -> datetime:
    return _BASE + timedelta(seconds=seconds)


# --- basic apply / peek -----------------------------------------------------


def test_apply_trade_then_peek_fresh() -> None:
    store = LatestMarketStateStore()
    result = store.apply(_trade(sequence=1), now=_now(1))
    assert result.status is ApplyStatus.APPLIED
    assert result.applied is True
    snap = store.peek(Market.KR, "005930", now=_now(2))
    assert snap.trade is not None
    assert snap.trade_fresh is True
    assert snap.quote is None
    assert snap.quote_fresh is False


def test_kr_and_us_same_symbol_kept_separate() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(market=Market.KR, symbol="AAA", channel="kr|AAA", price="100"), now=_now(1))
    store.apply(_trade(market=Market.US, symbol="AAA", channel="us|AAA", price="200"), now=_now(1))
    kr = store.peek(Market.KR, "AAA", now=_now(1))
    us = store.peek(Market.US, "AAA", now=_now(1))
    assert kr.trade is not None and str(kr.trade.price) == "100"
    assert us.trade is not None and str(us.trade.price) == "200"


# --- ordering / stream identity ---------------------------------------------


def test_duplicate_sequence_rejected_and_unchanged() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=5, price="70000"), now=_now(1))
    before = store.peek(Market.KR, "005930", now=_now(2))
    result = store.apply(_trade(sequence=5, price="99999"), now=_now(2))
    assert result.status is ApplyStatus.DUPLICATE
    assert store.peek(Market.KR, "005930", now=_now(2)) == before


def test_decreasing_sequence_rejected() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=5), now=_now(1))
    result = store.apply(_trade(sequence=4), now=_now(2))
    assert result.status is ApplyStatus.OUT_OF_ORDER


def test_stream_mismatch_rejected_and_unchanged() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=1, provider="kis", channel="chan-A"), now=_now(1))
    before = store.peek(Market.KR, "005930", now=_now(2))
    # higher sequence but different stream identity must not replace nor reset
    result = store.apply(_trade(sequence=2, provider="other", channel="chan-B"), now=_now(2))
    assert result.status is ApplyStatus.STREAM_MISMATCH
    assert store.peek(Market.KR, "005930", now=_now(2)) == before


def test_higher_sequence_but_received_at_regression_rejected() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=1, received_at=_now(5), trade_at=_now(5)), now=_now(6))
    result = store.apply(_trade(sequence=2, received_at=_now(3), trade_at=_now(6)), now=_now(7))
    assert result.status is ApplyStatus.OUT_OF_ORDER


def test_higher_sequence_but_event_time_regression_rejected() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=1, received_at=_now(5), trade_at=_now(5)), now=_now(6))
    result = store.apply(_trade(sequence=2, received_at=_now(6), trade_at=_now(3)), now=_now(7))
    assert result.status is ApplyStatus.OUT_OF_ORDER


def test_equal_event_time_increasing_sequence_applied() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=1, trade_at=_BASE, received_at=_BASE), now=_now(1))
    result = store.apply(_trade(sequence=2, trade_at=_BASE, received_at=_now(0.5)), now=_now(1))
    assert result.status is ApplyStatus.APPLIED


def test_rejected_apply_snapshot_unchanged() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=3), now=_now(1))
    before = store.peek(Market.KR, "005930", now=_now(2))
    store.apply(_trade(sequence=1), now=_now(2))  # out of order
    store.apply(_trade(sequence=3), now=_now(2))  # duplicate
    assert store.peek(Market.KR, "005930", now=_now(2)) == before


# --- future / naive-now contract violations ---------------------------------


def test_future_event_rejected() -> None:
    store = LatestMarketStateStore()
    with pytest.raises(FutureMarketEventError):
        store.apply(_trade(sequence=1, trade_at=_now(10)), now=_now(5))


def test_naive_now_rejected_everywhere() -> None:
    store = LatestMarketStateStore()
    naive = datetime(2026, 6, 8, 0, 5, 0)
    with pytest.raises(ValueError):
        store.apply(_trade(sequence=1), now=naive)
    with pytest.raises(ValueError):
        store.peek(Market.KR, "005930", now=naive)
    with pytest.raises(ValueError):
        store.require_fresh(Market.KR, "005930", now=naive)
    with pytest.raises(ValueError):
        store.peek_liveness("kis", "PINGPONG", now=naive)


# --- freshness / require_fresh ----------------------------------------------


def test_trade_fresh_quote_stale_combination() -> None:
    store = LatestMarketStateStore()
    store.apply(_quote(sequence=1, quote_at=_BASE, received_at=_BASE), now=_now(0))
    store.apply(_trade(sequence=1, trade_at=_now(20), received_at=_now(20)), now=_now(20))
    snap = store.peek(Market.KR, "005930", now=_now(25))
    assert snap.trade_fresh is True  # age 5s <= 10s
    assert snap.quote_fresh is False  # age 25s > 10s


def test_require_fresh_quote_only_passes_when_trade_stale() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=1, trade_at=_BASE, received_at=_BASE), now=_now(0))
    store.apply(_quote(sequence=1, quote_at=_now(20), received_at=_now(20)), now=_now(20))
    # trade is stale at now=25, quote is fresh
    store.require_fresh(
        Market.KR, "005930", now=_now(25), required=frozenset({MarketEventType.BEST_BID_ASK})
    )


def test_require_fresh_both_fails_when_one_stale() -> None:
    store = LatestMarketStateStore()
    store.apply(_trade(sequence=1, trade_at=_BASE, received_at=_BASE), now=_now(0))
    store.apply(_quote(sequence=1, quote_at=_now(20), received_at=_now(20)), now=_now(20))
    with pytest.raises(StaleMarketStateError):
        store.require_fresh(Market.KR, "005930", now=_now(25))  # default required = trade+quote


def test_require_fresh_missing_slot_raises() -> None:
    store = LatestMarketStateStore()
    with pytest.raises(MissingMarketStateError):
        store.require_fresh(Market.KR, "005930", now=_now(1))


def test_freshness_boundary_exactly_max_age_is_fresh() -> None:
    policy = MarketStateFreshnessPolicy()
    assert policy.is_fresh(_BASE, now=_now(10)) is True
    assert policy.is_fresh(_BASE, now=_now(10.001)) is False
    assert policy.is_fresh(_now(5), now=_BASE) is False  # future relative to now


# --- heartbeat liveness -----------------------------------------------------


def test_heartbeat_does_not_touch_trade_quote_slots() -> None:
    store = LatestMarketStateStore()
    store.apply(_heartbeat(), now=_now(1))
    snap = store.peek(Market.KR, "005930", now=_now(1))
    assert snap.trade is None
    assert snap.quote is None


def test_heartbeat_liveness_separate_key() -> None:
    store = LatestMarketStateStore()
    store.apply(_heartbeat(provider="kis", channel="PINGPONG", received_at=_BASE), now=_now(1))
    live = store.peek_liveness("kis", "PINGPONG", now=_now(2))
    assert live.heartbeat is not None
    assert live.is_live is True
    store.require_live("kis", "PINGPONG", now=_now(2))


def test_heartbeat_older_received_at_rejected() -> None:
    store = LatestMarketStateStore()
    store.apply(_heartbeat(received_at=_now(5)), now=_now(6))
    older = store.apply(_heartbeat(received_at=_now(3)), now=_now(6))
    assert older.status is ApplyStatus.OUT_OF_ORDER
    dup = store.apply(_heartbeat(received_at=_now(5)), now=_now(6))
    assert dup.status is ApplyStatus.DUPLICATE


def test_require_live_stale_and_missing() -> None:
    store = LatestMarketStateStore()
    with pytest.raises(MissingMarketStateError):
        store.require_live("kis", "PINGPONG", now=_now(1))
    store.apply(_heartbeat(received_at=_BASE), now=_now(0))
    with pytest.raises(StaleMarketStateError):
        store.require_live("kis", "PINGPONG", now=_now(20))


# --- concurrency ------------------------------------------------------------


def test_concurrent_apply_is_atomic_and_keeps_highest_sequence() -> None:
    store = LatestMarketStateStore()
    count = 32
    events = [
        _trade(sequence=i, trade_at=_now(i), received_at=_now(i), price=str(70000 + i))
        for i in range(1, count + 1)
    ]
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []

    def worker(event: NormalizedTradeTick) -> None:
        barrier.wait()
        try:
            store.apply(event, now=_now(count + 1))
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(event,)) for event in events]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    snap = store.peek(Market.KR, "005930", now=_now(count + 1))
    assert snap.trade is not None
    assert snap.trade.provider_sequence.sequence == count
