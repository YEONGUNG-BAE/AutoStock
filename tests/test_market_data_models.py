"""RTM-1 — normalized market-event domain model tests (fixture/network-free)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from domain.enums import Currency, Market
from market_data.models import (
    MarketEvent,
    MarketEventType,
    MarketHeartbeat,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)

_TS = datetime(2026, 6, 8, 0, 5, 0, tzinfo=UTC)
_EVENT_ADAPTER = TypeAdapter(MarketEvent)


def _sequence(**overrides: object) -> ProviderSequence:
    base: dict[str, object] = {
        "provider": "kis",
        "channel": "H0STCNT0|005930",
        "sequence": 1,
        "received_at": _TS,
    }
    base.update(overrides)
    return ProviderSequence(**base)


def _trade(**overrides: object) -> NormalizedTradeTick:
    base: dict[str, object] = {
        "provider": "kis",
        "symbol": "005930",
        "market": Market.KR,
        "currency": Currency.KRW,
        "price": Decimal("70000"),
        "quantity": Decimal("10"),
        "trade_at": _TS,
        "received_at": _TS,
        "provider_sequence": _sequence(),
    }
    base.update(overrides)
    return NormalizedTradeTick(**base)


def _quote(**overrides: object) -> NormalizedBestBidAsk:
    base: dict[str, object] = {
        "provider": "kis",
        "symbol": "005930",
        "market": Market.KR,
        "currency": Currency.KRW,
        "bid_price": Decimal("69900"),
        "ask_price": Decimal("70000"),
        "bid_quantity": Decimal("120"),
        "ask_quantity": Decimal("80"),
        "quote_at": _TS,
        "received_at": _TS,
        "provider_sequence": _sequence(channel="H0STASP0|005930"),
    }
    base.update(overrides)
    return NormalizedBestBidAsk(**base)


def _heartbeat(**overrides: object) -> MarketHeartbeat:
    base: dict[str, object] = {
        "provider": "kis",
        "channel": "PINGPONG",
        "sent_at": _TS,
        "received_at": _TS,
    }
    base.update(overrides)
    return MarketHeartbeat(**base)


def test_valid_trade_tick() -> None:
    tick = _trade()
    assert tick.event_type is MarketEventType.TRADE
    assert tick.price == Decimal("70000")
    assert tick.quantity == Decimal("10")
    assert tick.provider_sequence.sequence == 1


def test_valid_best_bid_ask() -> None:
    quote = _quote()
    assert quote.event_type is MarketEventType.BEST_BID_ASK
    assert quote.bid_price < quote.ask_price


def test_valid_heartbeat_optional_sequence() -> None:
    beat = _heartbeat()
    assert beat.event_type is MarketEventType.HEARTBEAT
    assert beat.provider_sequence is None
    assert _heartbeat(provider_sequence=_sequence(channel="PINGPONG")).provider_sequence is not None


def test_models_are_frozen() -> None:
    tick = _trade()
    with pytest.raises(ValidationError):
        tick.price = Decimal("1")  # type: ignore[misc]


def test_timezone_naive_timestamp_rejected() -> None:
    naive = datetime(2026, 6, 8, 0, 5, 0)
    with pytest.raises(ValidationError):
        _trade(trade_at=naive)
    with pytest.raises(ValidationError):
        _sequence(received_at=naive)


def test_zero_and_negative_price_rejected() -> None:
    with pytest.raises(ValidationError):
        _trade(price=Decimal("0"))
    with pytest.raises(ValidationError):
        _trade(price=Decimal("-1"))


def test_zero_and_negative_quantity_rejected() -> None:
    with pytest.raises(ValidationError):
        _trade(quantity=Decimal("0"))
    with pytest.raises(ValidationError):
        _trade(quantity=Decimal("-5"))


def test_non_finite_price_rejected() -> None:
    with pytest.raises(ValidationError):
        _trade(price="NaN")


def test_negative_cumulative_volume_rejected() -> None:
    with pytest.raises(ValidationError):
        _trade(cumulative_volume=Decimal("-1"))
    assert _trade(cumulative_volume=None).cumulative_volume is None


def test_crossed_book_rejected() -> None:
    with pytest.raises(ValidationError):
        _quote(bid_price=Decimal("70001"), ask_price=Decimal("70000"))
    # equal bid/ask is allowed (locked, not crossed)
    assert _quote(bid_price=Decimal("70000"), ask_price=Decimal("70000")).bid_price == Decimal("70000")


def test_negative_book_quantity_rejected() -> None:
    with pytest.raises(ValidationError):
        _quote(bid_quantity=Decimal("-1"))


def test_blank_symbol_rejected() -> None:
    with pytest.raises(ValidationError):
        _trade(symbol="   ")
    with pytest.raises(ValidationError):
        _quote(symbol="")


def test_negative_sequence_rejected() -> None:
    with pytest.raises(ValidationError):
        _sequence(sequence=-1)


def test_bool_sequence_rejected() -> None:
    with pytest.raises(ValidationError):
        _sequence(sequence=True)


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        NormalizedTradeTick(
            provider="kis",
            symbol="005930",
            market=Market.KR,
            currency=Currency.KRW,
            price=Decimal("70000"),
            quantity=Decimal("10"),
            trade_at=_TS,
            received_at=_TS,
            provider_sequence=_sequence(),
            unexpected="x",
        )


def test_unknown_market_rejected() -> None:
    with pytest.raises(ValidationError):
        _trade(market="JP")


def test_discriminated_union_roundtrip() -> None:
    for event in (_trade(), _quote(), _heartbeat()):
        dumped = _EVENT_ADAPTER.dump_python(event)
        restored = _EVENT_ADAPTER.validate_python(dumped)
        assert type(restored) is type(event)
        assert restored == event


def test_discriminated_union_rejects_unknown_event_type() -> None:
    payload = _EVENT_ADAPTER.dump_python(_trade())
    payload["event_type"] = "unknown"
    with pytest.raises(ValidationError):
        _EVENT_ADAPTER.validate_python(payload)
