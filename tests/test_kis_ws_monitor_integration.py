"""RTM-6 — KisWsMarketEventSource driven through MarketMonitor (fake WS, no network).

Proves the architecture boundary: the source does connect-once/subscribe/yield/disconnect,
and MarketMonitor is the sole reconnect owner. A dropped connection triggers a fresh source
(new sequence epoch) via source_factory, and the stream is reset before the first event of
the new epoch so the restarted sequence is accepted.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from data.kis_ws_source import (
    KisWsMarketEventSource,
    KisWsSubscription,
    KisWsTransportEvent,
)
from market_data.latest_state import LatestMarketStateStore
from market_data.monitor import MarketMonitor, MonitorState
from market_data.protocols import MarketEventSource

from domain.enums import Market

_KST = ZoneInfo("Asia/Seoul")
_NOW = datetime(2026, 6, 12, 10, 0, 0, tzinfo=_KST)
_TRADE_LEN = 46


def _trade_frame(prpr: str) -> str:
    record = ["0"] * _TRADE_LEN
    record[0] = "005930"
    record[1] = "095959"
    record[2] = prpr
    record[12] = "10"
    record[13] = "123456"
    record[33] = "20260612"
    return f"0|H0STCNT0|1|{'^'.join(record)}"


def _ack(tr_id: str = "H0STCNT0", tr_key: str = "005930") -> str:
    return json.dumps(
        {"header": {"tr_id": tr_id, "tr_key": tr_key}, "body": {"rt_cd": "0", "msg1": "x"}}
    )


class _ScriptedWebSocket:
    """recv가 inbox를 모두 내보낸 뒤 RuntimeError로 서버 단절을 흉내낸다."""

    def __init__(self, inbox: list[str]) -> None:
        self._inbox = list(inbox)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._inbox:
            raise RuntimeError("simulated server drop")
        return self._inbox.pop(0)

    async def pong(self, data: bytes = b"") -> None:  # pragma: no cover - no pings here
        pass

    async def close(self) -> None:
        self.closed = True


async def _noop_sleep(_seconds: float) -> None:
    return None


def test_source_through_monitor_applies_and_reconnects() -> None:
    transport_events: list[KisWsTransportEvent] = []
    sockets = [
        _ScriptedWebSocket([_ack(), _trade_frame("70000")]),  # epoch 1: ack, one trade, drop
        _ScriptedWebSocket([_ack(), _trade_frame("70100")]),  # epoch 2: ack, one trade, stop
    ]
    connects = iter(sockets)

    def source_factory() -> MarketEventSource:
        ws = next(connects)

        async def connect() -> _ScriptedWebSocket:
            return ws

        return KisWsMarketEventSource(
            connect=connect,
            approval_key="APV-XYZ",
            subscriptions=[KisWsSubscription(tr_id="H0STCNT0", symbol="005930")],
            clock=lambda: _NOW,
            receive_timeout_seconds=5.0,
            on_transport_event=transport_events.append,
        )

    store = LatestMarketStateStore()
    monitor = MarketMonitor(
        store=store,
        source_factory=source_factory,
        clock=lambda: _NOW,
        sleep=_noop_sleep,
        session_id="ws-monitor-int",
        max_events=2,
    )
    summary = asyncio.run(monitor.run())

    assert summary.applied == 2
    assert summary.connection_attempts == 2  # one drop -> one reconnect
    assert summary.final_state is MonitorState.STOPPED

    # both fake sockets subscribed (each epoch resubscribes via a fresh source).
    assert sockets[0].sent  # epoch 1 sent a subscribe
    assert sockets[1].sent  # epoch 2 resubscribed
    # transport-health shows two connects (one per epoch).
    assert sum(1 for e in transport_events if e.kind == "connected") == 2

    # latest store reflects the most recent applied trade price.
    snapshot = store.peek(Market.KR, "005930", now=_NOW)
    assert snapshot.trade is not None
    assert snapshot.trade.price == Decimal("70100")
