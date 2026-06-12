"""RTM-6 — KIS websocket MarketEventSource tests (fake WS; no real network/DNS)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from data.kis_ws_source import (
    KisWsMarketEventSource,
    KisWsSourceError,
    KisWsSubscription,
    KisWsSubscriptionError,
    KisWsTransportEvent,
)
from market_data.models import MarketHeartbeat, NormalizedBestBidAsk, NormalizedTradeTick

_KST = ZoneInfo("Asia/Seoul")
_NOW = datetime(2026, 6, 12, 10, 0, 0, tzinfo=_KST)

_QUOTE_LEN = 59
_TRADE_LEN = 46


def _trade_frame(*, symbol: str = "005930", prpr: str = "70000") -> str:
    record = ["0"] * _TRADE_LEN
    record[0] = symbol
    record[1] = "095959"
    record[2] = prpr
    record[12] = "10"
    record[13] = "123456"
    record[33] = "20260612"
    return f"0|H0STCNT0|1|{'^'.join(record)}"


def _quote_frame(*, symbol: str = "005930") -> str:
    record = ["0"] * _QUOTE_LEN
    record[0] = symbol
    record[1] = "095959"
    record[3] = "70100"
    record[13] = "69900"
    record[23] = "120"
    record[33] = "0"
    return f"0|H0STASP0|1|{'^'.join(record)}"


def _pingpong() -> str:
    return json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20260612100000"}, "body": {}})


def _ack(tr_id: str, rt_cd: str = "0") -> str:
    return json.dumps({"header": {"tr_id": tr_id}, "body": {"rt_cd": rt_cd, "msg1": "x"}})


class _Cancel:
    """recv가 CancelledError를 던지도록 하는 스크립트 마커."""


class _FakeWebSocket:
    def __init__(self, inbox: list[object]) -> None:
        self._inbox = list(inbox)
        self.sent: list[str] = []
        self.pongs: list[bytes] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if not self._inbox:
            raise KisWsSourceError("simulated server close (inbox drained).")
        item = self._inbox.pop(0)
        if isinstance(item, _Cancel):
            raise asyncio.CancelledError()
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]

    async def pong(self, data: bytes = b"") -> None:
        self.pongs.append(data)

    async def close(self) -> None:
        self.closed = True


class _BlockingWebSocket(_FakeWebSocket):
    async def recv(self) -> str | bytes:
        await asyncio.Event().wait()  # never returns -> forces receive timeout
        raise AssertionError("unreachable")  # pragma: no cover


def _source(ws: _FakeWebSocket, **overrides: object) -> KisWsMarketEventSource:
    events: list[KisWsTransportEvent] = []
    overrides.setdefault("on_transport_event", events.append)
    source = KisWsMarketEventSource(
        connect=_connect_factory(ws),
        approval_key="APV-XYZ",
        subscriptions=[
            KisWsSubscription(tr_id="H0STCNT0", symbol="005930"),
            KisWsSubscription(tr_id="H0STASP0", symbol="005930"),
        ],
        clock=lambda: _NOW,
        receive_timeout_seconds=overrides.pop("receive_timeout_seconds", 5.0),
        max_events=overrides.pop("max_events", None),
        on_transport_event=overrides.pop("on_transport_event"),
    )
    source._transport_log = events  # type: ignore[attr-defined]  # test introspection
    return source


def _connect_factory(ws: _FakeWebSocket):
    async def _connect():
        return ws

    return _connect


async def _drain(source: KisWsMarketEventSource, limit: int) -> list:
    out: list = []
    async for event in source.events():
        out.append(event)
        if len(out) >= limit:
            break
    return out


def test_subscribe_messages_sent_for_each_subscription() -> None:
    ws = _FakeWebSocket([_trade_frame()])
    source = _source(ws, max_events=1)
    asyncio.run(_drain(source, 1))
    # first two sends are the subscribe messages (before unsubscribe in finally).
    sub0 = json.loads(ws.sent[0])
    sub1 = json.loads(ws.sent[1])
    assert sub0["header"]["approval_key"] == "APV-XYZ"
    assert sub0["header"]["tr_type"] == "1"
    assert sub0["header"]["custtype"] == "P"
    assert sub0["body"]["input"] == {"tr_id": "H0STCNT0", "tr_key": "005930"}
    assert sub1["body"]["input"] == {"tr_id": "H0STASP0", "tr_key": "005930"}


def test_data_frames_parsed_and_yielded() -> None:
    ws = _FakeWebSocket([_trade_frame(), _quote_frame()])
    source = _source(ws, max_events=2)
    events = asyncio.run(_drain(source, 2))
    assert isinstance(events[0], NormalizedTradeTick)
    assert isinstance(events[1], NormalizedBestBidAsk)
    assert events[0].price == Decimal("70000")


def test_pingpong_triggers_pong_and_heartbeat() -> None:
    ws = _FakeWebSocket([_pingpong(), _trade_frame()])
    source = _source(ws, max_events=2)
    events = asyncio.run(_drain(source, 2))
    assert isinstance(events[0], MarketHeartbeat)
    assert events[0].channel == "PINGPONG"
    assert ws.pongs == [_pingpong().encode("utf-8")]
    assert isinstance(events[1], NormalizedTradeTick)


def test_subscribe_ack_success_is_ignored() -> None:
    ws = _FakeWebSocket([_ack("H0STCNT0", "0"), _trade_frame()])
    source = _source(ws, max_events=1)
    events = asyncio.run(_drain(source, 1))
    assert isinstance(events[0], NormalizedTradeTick)


def test_subscribe_ack_failure_fails_closed() -> None:
    ws = _FakeWebSocket([_ack("H0STCNT0", "1")])
    source = _source(ws)
    with pytest.raises(KisWsSubscriptionError, match="rt_cd=1"):
        asyncio.run(_drain(source, 5))


def test_cancellation_cleans_up_and_reraises() -> None:
    ws = _FakeWebSocket([_Cancel()])
    source = _source(ws)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_drain(source, 5))
    assert ws.closed is True
    # unsubscribe (tr_type "2") sent for each subscription during cleanup.
    unsub = [json.loads(m) for m in ws.sent if json.loads(m)["header"]["tr_type"] == "2"]
    assert {u["body"]["input"]["tr_id"] for u in unsub} == {"H0STCNT0", "H0STASP0"}


def test_clean_disconnect_unsubscribes_and_closes() -> None:
    ws = _FakeWebSocket([_trade_frame()])
    source = _source(ws, max_events=1)
    asyncio.run(_drain(source, 1))
    assert ws.closed is True
    tr_types = [json.loads(m)["header"]["tr_type"] for m in ws.sent]
    assert "2" in tr_types  # unsubscribe issued on clean exit


def test_transport_events_emitted() -> None:
    ws = _FakeWebSocket([_pingpong(), _trade_frame()])
    source = _source(ws, max_events=2)
    asyncio.run(_drain(source, 2))
    kinds = [e.kind for e in source._transport_log]  # type: ignore[attr-defined]
    assert kinds[0] == "connected"
    assert "subscription_sent" in kinds
    assert "ping_received" in kinds
    assert "pong_sent" in kinds
    assert kinds[-1] == "disconnect"


def test_receive_timeout_raises_for_monitor_drop() -> None:
    ws = _BlockingWebSocket([])
    source = _source(ws, receive_timeout_seconds=0.01)
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(_drain(source, 1))


def test_non_json_control_frame_fails_closed() -> None:
    ws = _FakeWebSocket(["garbage-not-json-not-data"])
    source = _source(ws)
    with pytest.raises(KisWsSourceError):
        asyncio.run(_drain(source, 1))


def test_blank_approval_key_rejected() -> None:
    ws = _FakeWebSocket([])
    with pytest.raises(KisWsSourceError):
        KisWsMarketEventSource(
            connect=_connect_factory(ws),
            approval_key="  ",
            subscriptions=[KisWsSubscription(tr_id="H0STCNT0", symbol="005930")],
            clock=lambda: _NOW,
            receive_timeout_seconds=5.0,
        )


def test_no_subscriptions_rejected() -> None:
    ws = _FakeWebSocket([])
    with pytest.raises(KisWsSubscriptionError):
        KisWsMarketEventSource(
            connect=_connect_factory(ws),
            approval_key="APV",
            subscriptions=[],
            clock=lambda: _NOW,
            receive_timeout_seconds=5.0,
        )


def test_bad_subscription_tr_id_rejected() -> None:
    with pytest.raises(KisWsSubscriptionError):
        KisWsSubscription(tr_id="H0STXXX0", symbol="005930")
