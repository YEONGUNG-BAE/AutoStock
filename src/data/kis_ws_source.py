from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from market_data.kis_official_ws_parser import (
    PROVIDER,
    TR_QUOTE,
    TR_TRADE,
    KisOfficialWsFrameParser,
)
from market_data.models import MarketEvent, MarketHeartbeat

_SUBSCRIBE = "1"
_UNSUBSCRIBE = "2"
_SUPPORTED_TR_IDS = frozenset({TR_QUOTE, TR_TRADE})
_PINGPONG_TR_ID = "PINGPONG"
_PINGPONG_CHANNEL = "PINGPONG"


class KisWsSourceError(Exception):
    """KIS websocket source 계열 공통 예외. raw frame/credential은 담지 않는다."""


class KisWsSubscriptionError(KisWsSourceError):
    """구독 ack가 실패(rt_cd != 0)했거나 구독 설정이 잘못된 경우."""


class WebSocketLike(Protocol):
    """주입식 websocket 연결의 구조적 계약. 테스트는 fake 연결을 주입한다."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def pong(self, data: bytes = b"") -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class KisWsTransportEvent:
    """transport-health evidence 한 건. raw frame/token/account는 절대 담지 않는다.

    market-data-health(trade/quote/parsed/applied)와 분리된 신호다.
    """

    kind: str
    tr_id: str | None = None
    symbol: str | None = None
    rt_cd: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class KisWsSubscription:
    tr_id: str
    symbol: str

    def __post_init__(self) -> None:
        if self.tr_id not in _SUPPORTED_TR_IDS:
            raise KisWsSubscriptionError("subscription tr_id must be H0STASP0 or H0STCNT0.")
        if not self.symbol or not self.symbol.strip():
            raise KisWsSubscriptionError("subscription symbol must be non-empty.")


class KisWsMarketEventSource:
    """KIS 국내 실시간 read-only websocket MarketEventSource.

    경계: connect 1회 → subscribe → event yield → disconnect만 한다. 내부 reconnect/
    backoff/heartbeat-timeout 루프가 없다(그 책임은 MarketMonitor 단독). 단절·iterator
    오류는 MarketMonitor가 drop으로 받아 source_factory로 새 source(=새 sequence epoch)를
    만들어 재구독한다. broker/ledger/paper execution을 import·호출하지 않으며 raw frame을
    로깅하지 않는다. asyncio.CancelledError는 정리 후 재전파한다.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], Awaitable[WebSocketLike]],
        approval_key: str,
        subscriptions: Sequence[KisWsSubscription],
        clock: Callable[[], datetime],
        receive_timeout_seconds: float,
        max_events: int | None = None,
        on_transport_event: Callable[[KisWsTransportEvent], None] | None = None,
    ) -> None:
        if not approval_key or not approval_key.strip():
            raise KisWsSourceError("approval_key is required.")
        if not subscriptions:
            raise KisWsSubscriptionError("at least one subscription is required.")
        if receive_timeout_seconds <= 0:
            raise KisWsSourceError("receive_timeout_seconds must be greater than 0.")
        if max_events is not None and max_events < 1:
            raise KisWsSourceError("max_events must be >= 1 when set.")
        self._connect = connect
        self._approval_key = approval_key
        self._subscriptions = tuple(subscriptions)
        self._clock = clock
        self._receive_timeout_seconds = receive_timeout_seconds
        self._max_events = max_events
        self._on_transport_event = on_transport_event
        self._parser = KisOfficialWsFrameParser()

    async def events(self) -> AsyncIterator[MarketEvent]:
        connection = await self._connect()
        self._emit(KisWsTransportEvent(kind="connected"))
        emitted = 0
        try:
            for subscription in self._subscriptions:
                await connection.send(self._subscribe_message(subscription, _SUBSCRIBE))
                self._emit(
                    KisWsTransportEvent(
                        kind="subscription_sent",
                        tr_id=subscription.tr_id,
                        symbol=subscription.symbol,
                    )
                )

            while True:
                if self._max_events is not None and emitted >= self._max_events:
                    return
                raw = await asyncio.wait_for(connection.recv(), self._receive_timeout_seconds)
                message = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                async for event in self._handle_message(connection, message):
                    yield event
                    emitted += 1
                    if self._max_events is not None and emitted >= self._max_events:
                        return
        except asyncio.CancelledError:
            await self._safe_unsubscribe(connection)
            await _safe_close(connection)
            self._emit(KisWsTransportEvent(kind="disconnect", detail="cancelled"))
            raise
        finally:
            await self._safe_unsubscribe(connection)
            await _safe_close(connection)
            self._emit(KisWsTransportEvent(kind="disconnect"))

    async def _handle_message(
        self, connection: WebSocketLike, message: str
    ) -> AsyncIterator[MarketEvent]:
        if message and message[0] in ("0", "1"):
            received_at = self._clock()
            for event in self._parser.parse_frame(message, received_at=received_at):
                yield event
            return
        # 제어(JSON) frame: PINGPONG 또는 구독 ack.
        control = self._parse_control(message)
        tr_id = _control_tr_id(control)
        if tr_id == _PINGPONG_TR_ID:
            await connection.pong(message.encode("utf-8"))
            self._emit(KisWsTransportEvent(kind="ping_received"))
            self._emit(KisWsTransportEvent(kind="pong_sent"))
            now = self._clock()
            yield MarketHeartbeat(
                provider=PROVIDER,
                channel=_PINGPONG_CHANNEL,
                sent_at=now,
                received_at=now,
            )
            return
        self._handle_ack(control, tr_id)

    def _handle_ack(self, control: dict, tr_id: str | None) -> None:
        body = control.get("body")
        rt_cd = body.get("rt_cd") if isinstance(body, dict) else None
        self._emit(KisWsTransportEvent(kind="ack", tr_id=tr_id, rt_cd=rt_cd if isinstance(rt_cd, str) else None))
        if isinstance(rt_cd, str) and rt_cd != "0":
            raise KisWsSubscriptionError(f"subscription ack failed for tr_id={tr_id} rt_cd={rt_cd}.")

    def _parse_control(self, message: str) -> dict:
        try:
            parsed = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            raise KisWsSourceError("received a non-data, non-JSON control frame.") from None
        if not isinstance(parsed, dict):
            raise KisWsSourceError("control frame is not a JSON object.")
        return parsed

    def _subscribe_message(self, subscription: KisWsSubscription, tr_type: str) -> str:
        return json.dumps(
            {
                "header": {
                    "approval_key": self._approval_key,
                    "custtype": "P",
                    "tr_type": tr_type,
                    "content-type": "utf-8",
                },
                "body": {"input": {"tr_id": subscription.tr_id, "tr_key": subscription.symbol}},
            }
        )

    async def _safe_unsubscribe(self, connection: WebSocketLike) -> None:
        for subscription in self._subscriptions:
            try:
                await connection.send(self._subscribe_message(subscription, _UNSUBSCRIBE))
                self._emit(
                    KisWsTransportEvent(
                        kind="unsubscribe_sent",
                        tr_id=subscription.tr_id,
                        symbol=subscription.symbol,
                    )
                )
            except Exception:  # noqa: BLE001 — best-effort cleanup, never mask the original path
                return

    def _emit(self, event: KisWsTransportEvent) -> None:
        if self._on_transport_event is None:
            return
        self._on_transport_event(event)


def _control_tr_id(control: dict) -> str | None:
    header = control.get("header")
    if isinstance(header, dict):
        tr_id = header.get("tr_id")
        if isinstance(tr_id, str):
            return tr_id
    return None


async def _safe_close(connection: WebSocketLike) -> None:
    try:
        await connection.close()
    except Exception:  # noqa: BLE001 — best-effort close
        return


async def open_kis_websocket(
    websocket_url: str, *, connect_timeout_seconds: float
) -> WebSocketLike:
    """실 KIS websocket 연결을 연다. websockets는 지연 import한다(테스트는 fake 주입).

    이 함수는 operator smoke의 --run 경로에서만 쓰인다. 자동 실행 경로가 아니다.
    """
    from websockets.asyncio.client import connect

    return await connect(websocket_url, open_timeout=connect_timeout_seconds)


__all__ = [
    "KisWsMarketEventSource",
    "KisWsSourceError",
    "KisWsSubscription",
    "KisWsSubscriptionError",
    "KisWsTransportEvent",
    "WebSocketLike",
    "open_kis_websocket",
]
