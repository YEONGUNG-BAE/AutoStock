"""RTM-7b — concrete evidence → neutral health signal adapter (data 계층).

`KisWsTransportEvent`/`MonitorEvidence`는 data/monitor concrete 타입이다.
`market_data.health_policy`/`market_data.supervisor`는 이 모듈을 import하지 않는다 —
adapter가 중립 시그널로 변환해 supervisor/tracker에 전달한다.

금지: broker/ledger/execution import, raw frame/token 보존, filesystem write.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from data.kis_ws_source import KisWsTransportEvent
from market_data.health_policy import RecordResult
from market_data.models import MarketEventType
from market_data.monitor import MonitorEvidence

__all__ = [
    "AdapterError",
    "MarketSupervisorAdapter",
    "NeutralMarketSignal",
    "NeutralTransportSignal",
    "TransportKind",
]


class AdapterError(Exception):
    """adapter 입력/매핑 위반. raw frame/credential/예외 repr을 담지 않는다."""


class TransportKind(StrEnum):
    CONNECTED = "connected"
    ALL_SUBSCRIBED = "all_subscribed"
    DISCONNECT = "disconnect"
    PING_RECEIVED = "ping_received"
    PONG_SENT = "pong_sent"


_KIS_TRANSPORT_MAP: dict[str, str] = {
    "connected": TransportKind.CONNECTED.value,
    "all_subscribed": TransportKind.ALL_SUBSCRIBED.value,
    "disconnect": TransportKind.DISCONNECT.value,
    "ping_received": TransportKind.PING_RECEIVED.value,
    "pong_sent": TransportKind.PONG_SENT.value,
    "ack": TransportKind.ALL_SUBSCRIBED.value,
    "subscribed": TransportKind.ALL_SUBSCRIBED.value,
}

_MONITOR_TRANSPORT_MAP: dict[str, str] = {
    "connect": TransportKind.CONNECTED.value,
    "eof": TransportKind.DISCONNECT.value,
    "drop": TransportKind.DISCONNECT.value,
    "cancelled": TransportKind.DISCONNECT.value,
}


@dataclass(frozen=True)
class NeutralTransportSignal:
    kind: str
    at: datetime


@dataclass(frozen=True)
class NeutralMarketSignal:
    event_type: str
    at: datetime


class _RecordSink(Protocol):
    def record_transport_event(self, *, kind: str, at: datetime, now: datetime) -> RecordResult: ...

    def record_market_event(
        self, *, event_type: str, at: datetime, now: datetime
    ) -> RecordResult: ...


def _sanitize_reason(code: str | None) -> str | None:
    if code is None:
        return None
    cleaned = code.strip()
    if not cleaned or len(cleaned) > 64:
        return "sanitized"
    lowered = cleaned.lower()
    for forbidden in ("token", "approval", "secret", "password", "account", "frame", "raw"):
        if forbidden in lowered:
            return "sanitized"
    return cleaned


class MarketSupervisorAdapter:
    """concrete evidence → neutral signal → tracker/supervisor record_* 호출."""

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        self._clock = clock

    def adapt_kis_transport(self, event: KisWsTransportEvent) -> NeutralTransportSignal:
        if event.at is None:
            raise AdapterError("KisWsTransportEvent.at is required.")
        if event.kind == "subscription_sent":
            raise AdapterError("subscription_sent is not a health signal.")
        kind = _KIS_TRANSPORT_MAP.get(event.kind)
        if kind is None:
            raise AdapterError(f"unknown KisWsTransportEvent.kind: {event.kind}")
        return NeutralTransportSignal(kind=kind, at=event.at)

    def adapt_monitor_evidence(self, evidence: MonitorEvidence) -> NeutralMarketSignal | None:
        if evidence.kind != "apply" or evidence.event_type is None:
            return None
        et = evidence.event_type
        if et not in (
            MarketEventType.BEST_BID_ASK.value,
            MarketEventType.TRADE.value,
            MarketEventType.HEARTBEAT.value,
        ):
            raise AdapterError(f"unknown monitor event_type: {et}")
        return NeutralMarketSignal(event_type=et, at=evidence.timestamp)

    def adapt_monitor_transport(self, evidence: MonitorEvidence) -> NeutralTransportSignal | None:
        kind = _MONITOR_TRANSPORT_MAP.get(evidence.kind)
        if kind is None:
            return None
        return NeutralTransportSignal(kind=kind, at=evidence.timestamp)

    def forward_kis_transport(
        self, event: KisWsTransportEvent, sink: _RecordSink
    ) -> RecordResult:
        signal = self.adapt_kis_transport(event)
        now = self._clock()
        return sink.record_transport_event(kind=signal.kind, at=signal.at, now=now)

    def forward_monitor_evidence(
        self, evidence: MonitorEvidence, sink: _RecordSink
    ) -> tuple[RecordResult | None, RecordResult | None]:
        now = self._clock()
        market_result: RecordResult | None = None
        transport_result: RecordResult | None = None
        market = self.adapt_monitor_evidence(evidence)
        if market is not None:
            market_result = sink.record_market_event(
                event_type=market.event_type, at=market.at, now=now
            )
        transport = self.adapt_monitor_transport(evidence)
        if transport is not None:
            transport_result = sink.record_transport_event(
                kind=transport.kind, at=transport.at, now=now
            )
        return market_result, transport_result

    @staticmethod
    def sanitize_reason_code(code: str | None) -> str | None:
        return _sanitize_reason(code)
