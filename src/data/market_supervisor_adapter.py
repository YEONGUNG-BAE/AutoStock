"""RTM-7b — concrete evidence → neutral health signal adapter (data 계층).

`KisWsTransportEvent`/`MonitorEvidence`는 data/monitor concrete 타입이다.
`market_data.health_policy`/`market_data.supervisor`는 이 모듈을 import하지 않는다.
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
    "InformationalTransportEvent",
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


# tracker all_subscribed 상태를 변경하는 kind만 매핑한다.
_KIS_HEALTH_SIGNAL_MAP: dict[str, str] = {
    "connected": TransportKind.CONNECTED.value,
    "all_subscribed": TransportKind.ALL_SUBSCRIBED.value,
    "disconnect": TransportKind.DISCONNECT.value,
    "ping_received": TransportKind.PING_RECEIVED.value,
    "pong_sent": TransportKind.PONG_SENT.value,
}

# informational — tracker 상태를 변경하지 않는다.
_KIS_INFORMATIONAL_KINDS = frozenset(
    {"subscription_sent", "ack", "subscribed", "unsubscribe_sent"}
)

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


@dataclass(frozen=True)
class InformationalTransportEvent:
    """tracker에 기록하지 않는 informational transport evidence."""

    kind: str
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

    def adapt_kis_transport(
        self, event: KisWsTransportEvent
    ) -> NeutralTransportSignal | InformationalTransportEvent:
        if event.at is None:
            raise AdapterError("KisWsTransportEvent.at is required.")
        if event.kind in _KIS_INFORMATIONAL_KINDS:
            return InformationalTransportEvent(kind=event.kind, at=event.at)
        kind = _KIS_HEALTH_SIGNAL_MAP.get(event.kind)
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
    ) -> RecordResult | None:
        adapted = self.adapt_kis_transport(event)
        if isinstance(adapted, InformationalTransportEvent):
            return None
        now = max(self._clock(), adapted.at)
        return sink.record_transport_event(kind=adapted.kind, at=adapted.at, now=now)

    def forward_monitor_evidence(
        self, evidence: MonitorEvidence, sink: _RecordSink
    ) -> tuple[RecordResult | None, RecordResult | None]:
        now = max(self._clock(), evidence.timestamp)
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
