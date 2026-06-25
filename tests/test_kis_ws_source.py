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
from market_data.source_errors import (
    MalformedMarketFrameAfterAck,
    SourceIteratorUnknownAfterAck,
    UnsupportedTrIdAfterAck,
    WebSocketClosedAfterAck,
    WebSocketProtocolErrorAfterAck,
    WebSocketReceiveTimeoutAfterAck,
)

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


def _quote_frame_with_trailing_empty(*, symbol: str = "005930") -> str:
    return _quote_frame(symbol=symbol) + "^"


def _unsupported_tr_id_frame() -> str:
    return "0|H0STXXX0|1|005930^095959"


def _malformed_quote_frame() -> str:
    return "0|H0STASP0|1|005930^095959"


def _pingpong() -> str:
    return json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20260612100000"}, "body": {}})


def _ack(tr_id: str, *, tr_key: str = "005930", rt_cd: str = "0") -> str:
    return json.dumps(
        {"header": {"tr_id": tr_id, "tr_key": tr_key}, "body": {"rt_cd": rt_cd, "msg1": "x"}}
    )


def _ack_missing_rt_cd(tr_id: str, *, tr_key: str = "005930") -> str:
    return json.dumps({"header": {"tr_id": tr_id, "tr_key": tr_key}, "body": {"msg1": "x"}})


def _acks() -> list[str]:
    """양쪽 구독(H0STCNT0/H0STASP0|005930)을 성공 ack로 모두 푸는 프레임 쌍."""
    return [_ack("H0STCNT0"), _ack("H0STASP0")]


class _Cancel:
    """recv가 CancelledError를 던지도록 하는 스크립트 마커."""


class _FakeWebSocket:
    def __init__(self, inbox: list[object]) -> None:
        self._inbox = list(inbox)
        self.sent: list[str] = []
        self.pongs: list[bytes] = []
        self.closed = False
        self.close_count = 0

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
        self.close_count += 1


class _BlockingWebSocket(_FakeWebSocket):
    async def recv(self) -> str | bytes:
        if self._inbox:
            item = self._inbox.pop(0)
            if isinstance(item, _Cancel):
                raise asyncio.CancelledError()
            if isinstance(item, Exception):
                raise item
            return item  # type: ignore[return-value]
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
    ws = _FakeWebSocket([*_acks(), _trade_frame()])
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
    ws = _FakeWebSocket([*_acks(), _trade_frame(), _quote_frame()])
    source = _source(ws, max_events=2)
    events = asyncio.run(_drain(source, 2))
    assert isinstance(events[0], NormalizedTradeTick)
    assert isinstance(events[1], NormalizedBestBidAsk)
    assert events[0].price == Decimal("70000")


def test_quote_frame_with_trailing_empty_delimiter_is_yielded() -> None:
    ws = _FakeWebSocket([*_acks(), _quote_frame_with_trailing_empty()])
    source = _source(ws, max_events=1)
    events = asyncio.run(_drain(source, 1))
    assert len(events) == 1
    assert isinstance(events[0], NormalizedBestBidAsk)
    assert events[0].provider_sequence.channel == "H0STASP0|005930"


def test_pingpong_triggers_pong_and_heartbeat() -> None:
    ws = _FakeWebSocket([_pingpong(), *_acks(), _trade_frame()])
    source = _source(ws, max_events=2)
    events = asyncio.run(_drain(source, 2))
    assert isinstance(events[0], MarketHeartbeat)
    assert events[0].channel == "PINGPONG"
    assert ws.pongs == [_pingpong().encode("utf-8")]
    assert isinstance(events[1], NormalizedTradeTick)


def test_data_frame_accepted_after_all_acks() -> None:
    ws = _FakeWebSocket([*_acks(), _trade_frame()])
    source = _source(ws, max_events=1)
    events = asyncio.run(_drain(source, 1))
    assert isinstance(events[0], NormalizedTradeTick)
    kinds = [e.kind for e in source._transport_log]  # type: ignore[attr-defined]
    assert "all_subscribed" in kinds


def test_data_frame_before_any_ack_is_rejected() -> None:
    ws = _FakeWebSocket([_trade_frame()])
    source = _source(ws)
    with pytest.raises(KisWsSubscriptionError, match="before all subscriptions were acked"):
        asyncio.run(_drain(source, 5))


def test_data_frame_after_partial_ack_is_rejected() -> None:
    # 한쪽 구독만 ack된 상태에서 들어온 시세 frame은 배리어가 거부한다.
    ws = _FakeWebSocket([_ack("H0STCNT0"), _trade_frame()])
    source = _source(ws)
    with pytest.raises(KisWsSubscriptionError, match="before all subscriptions were acked"):
        asyncio.run(_drain(source, 5))


def test_subscribe_ack_failure_fails_closed() -> None:
    ws = _FakeWebSocket([_ack("H0STCNT0", rt_cd="1")])
    source = _source(ws)
    with pytest.raises(KisWsSubscriptionError, match="rt_cd missing or non-zero"):
        asyncio.run(_drain(source, 5))


def test_ack_missing_rt_cd_fails_closed() -> None:
    ws = _FakeWebSocket([_ack_missing_rt_cd("H0STCNT0")])
    source = _source(ws)
    with pytest.raises(KisWsSubscriptionError, match="rt_cd missing or non-zero"):
        asyncio.run(_drain(source, 5))


def test_ack_for_unrequested_subscription_is_rejected() -> None:
    ws = _FakeWebSocket([_ack("H0STCNT0", tr_key="999999")])
    source = _source(ws)
    with pytest.raises(KisWsSubscriptionError, match="unrequested subscription"):
        asyncio.run(_drain(source, 5))


def test_duplicate_ack_is_rejected() -> None:
    ws = _FakeWebSocket([_ack("H0STCNT0"), _ack("H0STCNT0")])
    source = _source(ws)
    with pytest.raises(KisWsSubscriptionError, match="duplicate ack"):
        asyncio.run(_drain(source, 5))


def test_unsubscribed_symbol_frame_is_rejected() -> None:
    # 모든 구독이 ack됐어도, 구독하지 않은 종목 시세 frame은 fail-closed로 거부한다.
    ws = _FakeWebSocket([*_acks(), _trade_frame(symbol="000660")])
    source = _source(ws)
    with pytest.raises(WebSocketProtocolErrorAfterAck):
        asyncio.run(_drain(source, 5))


def test_cancellation_cleans_up_and_reraises() -> None:
    ws = _FakeWebSocket([_Cancel()])
    source = _source(ws)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_drain(source, 5))
    # cleanup runs exactly once (single finally path; no duplicate except-branch cleanup).
    assert ws.closed is True
    assert ws.close_count == 1
    # unsubscribe (tr_type "2") sent exactly once per subscription during cleanup.
    unsub = [json.loads(m) for m in ws.sent if json.loads(m)["header"]["tr_type"] == "2"]
    assert [u["body"]["input"]["tr_id"] for u in unsub] == ["H0STCNT0", "H0STASP0"]
    disconnects = [e for e in source._transport_log if e.kind == "disconnect"]  # type: ignore[attr-defined]
    assert len(disconnects) == 1
    assert disconnects[0].detail == "cancelled"


def test_clean_disconnect_unsubscribes_and_closes() -> None:
    ws = _FakeWebSocket([*_acks(), _trade_frame()])
    source = _source(ws, max_events=1)
    asyncio.run(_drain(source, 1))
    assert ws.closed is True
    tr_types = [json.loads(m)["header"]["tr_type"] for m in ws.sent]
    assert "2" in tr_types  # unsubscribe issued on clean exit


def test_transport_events_emitted() -> None:
    ws = _FakeWebSocket([_pingpong(), *_acks(), _trade_frame()])
    source = _source(ws, max_events=2)
    asyncio.run(_drain(source, 2))
    log = source._transport_log  # type: ignore[attr-defined]
    kinds = [e.kind for e in log]
    assert kinds[0] == "connected"
    assert "subscription_sent" in kinds
    assert "ack" in kinds
    assert "subscribed" in kinds
    assert "all_subscribed" in kinds
    assert "ping_received" in kinds
    assert "pong_sent" in kinds
    assert kinds[-1] == "disconnect"
    # every emitted transport event carries a source-clock timestamp.
    assert all(e.at == _NOW for e in log)


def test_receive_timeout_raises_for_monitor_drop() -> None:
    ws = _BlockingWebSocket([])
    source = _source(ws, receive_timeout_seconds=0.01)
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(_drain(source, 1))


@pytest.mark.parametrize(
    ("inbox", "expected_error"),
    [
        ([*_acks()], WebSocketClosedAfterAck),
        ([*_acks(), "garbage-not-json-not-data"], WebSocketProtocolErrorAfterAck),
        ([*_acks(), _unsupported_tr_id_frame()], UnsupportedTrIdAfterAck),
        ([*_acks(), _malformed_quote_frame()], MalformedMarketFrameAfterAck),
    ],
)
def test_after_ack_source_errors_are_sanitized_subcode_types(
    inbox: list[object], expected_error: type[Exception]
) -> None:
    source = _source(_FakeWebSocket(inbox))
    with pytest.raises(expected_error):
        asyncio.run(_drain(source, 1))


def test_after_ack_receive_timeout_is_sanitized_subcode_type() -> None:
    source = _source(_BlockingWebSocket([*_acks()]), receive_timeout_seconds=0.01)
    with pytest.raises(WebSocketReceiveTimeoutAfterAck):
        asyncio.run(_drain(source, 1))


def test_after_ack_unexpected_iterator_error_is_sanitized_subcode_type() -> None:
    source = _source(_FakeWebSocket([*_acks(), _trade_frame()]))

    class _BoomParser:
        def parse_frame(self, raw: str, *, received_at: datetime) -> list[object]:  # noqa: ARG002
            raise RuntimeError("raw frame must not leak")

    source._parser = _BoomParser()  # type: ignore[attr-defined]
    with pytest.raises(SourceIteratorUnknownAfterAck):
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
