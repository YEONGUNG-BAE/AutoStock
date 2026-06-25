from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from market_data.kis_official_ws_parser import (
    PROVIDER,
    TR_QUOTE,
    TR_TRADE,
    KisOfficialWsFrameParser,
    KisOfficialWsParseError,
    KisOfficialWsUnsupportedTrIdError,
)
from market_data.source_errors import (
    MalformedMarketFrameAfterAck,
    MarketSourceIteratorError,
    SourceIteratorUnknownAfterAck,
    UnsupportedTrIdAfterAck,
    WebSocketClosedAfterAck,
    WebSocketProtocolErrorAfterAck,
    WebSocketReceiveTimeoutAfterAck,
)
from market_data.models import MarketEvent, MarketHeartbeat

_SUBSCRIBE = "1"
_UNSUBSCRIBE = "2"
_SUPPORTED_TR_IDS = frozenset({TR_QUOTE, TR_TRADE})
_PINGPONG_TR_ID = "PINGPONG"
_PINGPONG_CHANNEL = "PINGPONG"
_ACK_SUCCESS = "0"


class KisWsSourceError(Exception):
    """KIS websocket source 계열 공통 예외. raw frame/credential은 담지 않는다."""


class KisWsSubscriptionError(KisWsSourceError):
    """구독 ack가 실패(rt_cd != 0)했거나 구독 설정/순서가 잘못된 경우."""


class WebSocketLike(Protocol):
    """주입식 websocket 연결의 구조적 계약. 테스트는 fake 연결을 주입한다."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def pong(self, data: bytes = b"") -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class KisWsTransportEvent:
    """transport-health evidence 한 건. raw frame/token/account는 절대 담지 않는다.

    market-data-health(trade/quote/parsed/applied)와 분리된 신호다. kind 예:
    connected / subscription_sent / ack / subscribed / all_subscribed /
    ping_received / pong_sent / unsubscribe_sent / disconnect. `at`은 source clock으로
    찍은 시각(연결 지속시간 산출용)이며 `_emit`에서 자동 부여된다.
    """

    kind: str
    tr_id: str | None = None
    symbol: str | None = None
    rt_cd: str | None = None
    detail: str | None = None
    at: datetime | None = None


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

    경계: connect 1회 → subscribe → (모든 구독 ack 확인) → event yield → disconnect만
    한다. 내부 reconnect/backoff/heartbeat-timeout 루프가 없다(그 책임은 MarketMonitor
    단독). 단절·iterator 오류는 MarketMonitor가 drop으로 받아 source_factory로 새
    source(=새 sequence epoch)를 만들어 재구독한다. broker/ledger/paper execution을
    import·호출하지 않으며 raw frame을 로깅하지 않는다. asyncio.CancelledError는 정리 후
    재전파한다.

    ack 배리어: 모든 요청 구독이 성공 ack(rt_cd=="0")을 받기 전에는 시세 frame을 수용하지
    않는다. ack는 `(tr_id, tr_key)` 단위로 추적하며 누락 rt_cd / 알 수 없는 식별자 / 중복
    ack는 fail-closed로 거부한다. 구독하지 않은 `(tr_id, symbol)` 시세 frame도 거부한다.
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
        self._subscribed_keys = frozenset((s.tr_id, s.symbol) for s in self._subscriptions)
        self._subscribed_channels = frozenset(f"{s.tr_id}|{s.symbol}" for s in self._subscriptions)
        self._pending_acks: set[tuple[str, str]] = set()
        self._acked: set[tuple[str, str]] = set()

    async def events(self) -> AsyncIterator[MarketEvent]:
        # 새 epoch마다 ack 상태를 초기화한다(fresh source가 정석이지만 방어적으로 리셋).
        self._pending_acks = set(self._subscribed_keys)
        self._acked = set()
        connection = await self._connect()
        self._emit(KisWsTransportEvent(kind="connected"))
        emitted = 0
        cancelled = False
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
                try:
                    raw = await asyncio.wait_for(
                        connection.recv(), self._receive_timeout_seconds
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    if not self._pending_acks:
                        raise WebSocketReceiveTimeoutAfterAck() from None
                    raise
                except Exception:
                    if not self._pending_acks:
                        raise WebSocketClosedAfterAck() from None
                    raise
                message = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                try:
                    async for event in self._handle_message(connection, message):
                        yield event
                        emitted += 1
                        if self._max_events is not None and emitted >= self._max_events:
                            return
                except MarketSourceIteratorError:
                    raise
                except Exception:
                    if not self._pending_acks:
                        raise SourceIteratorUnknownAfterAck() from None
                    raise
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # 정확히 한 번만 정리한다(취소 경로도 finally 단일 경로로 수렴).
            await self._safe_unsubscribe(connection)
            await _safe_close(connection)
            self._emit(
                KisWsTransportEvent(kind="disconnect", detail="cancelled" if cancelled else None)
            )

    async def _handle_message(
        self, connection: WebSocketLike, message: str
    ) -> AsyncIterator[MarketEvent]:
        if message and message[0] in ("0", "1"):
            # ack 배리어: 모든 구독이 확인되기 전에는 시세 frame을 수용하지 않는다.
            if self._pending_acks:
                raise KisWsSubscriptionError(
                    "market frame received before all subscriptions were acked."
                )
            received_at = self._clock()
            try:
                parsed_events = self._parser.parse_frame(message, received_at=received_at)
            except KisOfficialWsUnsupportedTrIdError:
                raise UnsupportedTrIdAfterAck() from None
            except KisOfficialWsParseError:
                raise MalformedMarketFrameAfterAck() from None
            for event in parsed_events:
                channel = _event_channel(event)
                if channel not in self._subscribed_channels:
                    raise WebSocketProtocolErrorAfterAck() from None
                yield event
            return
        # 제어(JSON) frame: PINGPONG 또는 구독 ack.
        try:
            control = self._parse_control(message)
        except KisWsSourceError:
            if not self._pending_acks:
                raise WebSocketProtocolErrorAfterAck() from None
            raise
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
        try:
            self._handle_ack(control, tr_id)
        except KisWsSubscriptionError:
            if not self._pending_acks:
                raise WebSocketProtocolErrorAfterAck() from None
            raise

    def _handle_ack(self, control: dict, tr_id: str | None) -> None:
        header = control.get("header")
        tr_key = header.get("tr_key") if isinstance(header, dict) else None
        body = control.get("body")
        rt_cd = body.get("rt_cd") if isinstance(body, dict) else None
        self._emit(
            KisWsTransportEvent(
                kind="ack",
                tr_id=tr_id if isinstance(tr_id, str) else None,
                symbol=tr_key if isinstance(tr_key, str) else None,
                rt_cd=rt_cd if isinstance(rt_cd, str) else None,
            )
        )
        # 누락/비-문자/non-zero rt_cd 모두 실패로 본다(fail-closed). missing rt_cd 통과 금지.
        if not isinstance(rt_cd, str) or rt_cd != _ACK_SUCCESS:
            raise KisWsSubscriptionError(
                f"subscription ack not successful for tr_id={tr_id} (rt_cd missing or non-zero)."
            )
        if not isinstance(tr_id, str) or not isinstance(tr_key, str):
            raise KisWsSubscriptionError("subscription ack missing tr_id/tr_key identity.")
        identity = (tr_id, tr_key)
        if identity not in self._subscribed_keys:
            raise KisWsSubscriptionError(f"ack for an unrequested subscription tr_id={tr_id}.")
        if identity in self._acked:
            raise KisWsSubscriptionError(f"duplicate ack for tr_id={tr_id}.")
        self._acked.add(identity)
        self._pending_acks.discard(identity)
        self._emit(KisWsTransportEvent(kind="subscribed", tr_id=tr_id, symbol=tr_key))
        if not self._pending_acks:
            self._emit(KisWsTransportEvent(kind="all_subscribed"))

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
        stamped = event if event.at is not None else replace(event, at=self._clock())
        self._on_transport_event(stamped)


def _event_channel(event: MarketEvent) -> str:
    sequence = getattr(event, "provider_sequence", None)
    channel = getattr(sequence, "channel", None)
    if not isinstance(channel, str):
        # 시세 path는 항상 provider_sequence를 갖는다. 없으면 계약 위반이므로 fail-closed.
        raise KisWsSubscriptionError("market event missing provider_sequence channel.")
    return channel


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
