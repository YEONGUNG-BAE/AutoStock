from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from market_data.models import (
    MarketEvent,
    MarketHeartbeat,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)

# Declared fixture envelope contract. RTM-1 parses ONLY this version. Official KIS
# WebSocket frame layout / field positions are unverified and deferred to RTM-6.
PROVIDER_CONTRACT = "kis-ws-fixture-v1"

_ENVELOPE_REQUIRED = frozenset({"provider_contract", "provider", "type", "channel", "received_at", "payload"})
_ENVELOPE_OPTIONAL = frozenset({"sequence"})

_TRADE_REQUIRED = frozenset({"symbol", "market", "currency", "price", "quantity", "trade_at"})
_TRADE_OPTIONAL = frozenset({"cumulative_volume"})

_QUOTE_REQUIRED = frozenset(
    {"symbol", "market", "currency", "bid_price", "ask_price", "bid_quantity", "ask_quantity", "quote_at"}
)
_QUOTE_OPTIONAL: frozenset[str] = frozenset()

_HEARTBEAT_REQUIRED = frozenset({"sent_at"})
_HEARTBEAT_OPTIONAL: frozenset[str] = frozenset()


class MarketDataParserError(Exception):
    """market_data 파서 계열 공통 예외. raw frame/credential은 절대 포함하지 않는다."""


class KisWsParseError(MarketDataParserError):
    """KIS WebSocket fixture frame 파싱 실패. 메시지에 원본 frame 값을 담지 않는다."""


class SequenceViolationError(MarketDataParserError):
    """채널별 provider sequence 위반(중복/감소/누락)."""


class SequenceTracker:
    """채널별 provider sequence의 중복·감소·gap을 fail-closed로 검출한다."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], int] = {}

    def observe(self, *, provider: str, channel: str, sequence: int) -> None:
        key = (provider, channel)
        last = self._last.get(key)
        if last is not None:
            if sequence == last:
                raise SequenceViolationError(
                    f"duplicate sequence for channel={channel!r}: {sequence} already seen."
                )
            if sequence < last:
                raise SequenceViolationError(
                    f"decreasing sequence for channel={channel!r}: {sequence} < {last}."
                )
            if sequence > last + 1:
                raise SequenceViolationError(
                    f"sequence gap for channel={channel!r}: expected {last + 1}, got {sequence}."
                )
        self._last[key] = sequence


class KisWsFrameParser:
    """fixture envelope(`kis-ws-fixture-v1`)만 정규화 MarketEvent로 변환한다.

    network/env/filesystem 접근이 없으며, 선언된 contract 외에는 모두 fail-closed.
    """

    def __init__(self, *, track_sequence: bool = True) -> None:
        self._track_sequence = track_sequence
        self._tracker = SequenceTracker()

    def parse(self, frame: Any) -> MarketEvent:
        if not isinstance(frame, Mapping):
            raise KisWsParseError("frame must be a mapping object.")

        _require_keys(frame, required=_ENVELOPE_REQUIRED, optional=_ENVELOPE_OPTIONAL, where="frame")

        contract = frame.get("provider_contract")
        if contract != PROVIDER_CONTRACT:
            raise KisWsParseError(
                f"unknown provider_contract; parser only accepts {PROVIDER_CONTRACT!r}."
            )

        provider = _require_text(frame.get("provider"), field="provider")
        channel = _require_text(frame.get("channel"), field="channel")
        event_type = _require_text(frame.get("type"), field="type")
        received_at = frame.get("received_at")
        payload = frame.get("payload")
        if not isinstance(payload, Mapping):
            raise KisWsParseError("frame.payload must be a mapping object.")

        sequence_value = frame.get("sequence")

        if event_type == "trade":
            event = self._build_trade(provider, channel, received_at, sequence_value, payload)
        elif event_type == "best_bid_ask":
            event = self._build_quote(provider, channel, received_at, sequence_value, payload)
        elif event_type == "heartbeat":
            event = self._build_heartbeat(provider, channel, received_at, sequence_value, payload)
        else:
            raise KisWsParseError("unknown frame type; expected trade|best_bid_ask|heartbeat.")

        self._track(event)
        return event

    def _build_trade(
        self,
        provider: str,
        channel: str,
        received_at: Any,
        sequence_value: Any,
        payload: Mapping[str, Any],
    ) -> NormalizedTradeTick:
        if sequence_value is None:
            raise KisWsParseError("trade frame requires a sequence.")
        _require_keys(payload, required=_TRADE_REQUIRED, optional=_TRADE_OPTIONAL, where="payload")
        provider_sequence = _build_provider_sequence(provider, channel, sequence_value, received_at)
        return _build_model(
            NormalizedTradeTick,
            provider=provider,
            symbol=payload.get("symbol"),
            market=payload.get("market"),
            currency=payload.get("currency"),
            price=payload.get("price"),
            quantity=payload.get("quantity"),
            trade_at=payload.get("trade_at"),
            received_at=received_at,
            provider_sequence=provider_sequence,
            cumulative_volume=payload.get("cumulative_volume"),
        )

    def _build_quote(
        self,
        provider: str,
        channel: str,
        received_at: Any,
        sequence_value: Any,
        payload: Mapping[str, Any],
    ) -> NormalizedBestBidAsk:
        if sequence_value is None:
            raise KisWsParseError("best_bid_ask frame requires a sequence.")
        _require_keys(payload, required=_QUOTE_REQUIRED, optional=_QUOTE_OPTIONAL, where="payload")
        provider_sequence = _build_provider_sequence(provider, channel, sequence_value, received_at)
        return _build_model(
            NormalizedBestBidAsk,
            provider=provider,
            symbol=payload.get("symbol"),
            market=payload.get("market"),
            currency=payload.get("currency"),
            bid_price=payload.get("bid_price"),
            ask_price=payload.get("ask_price"),
            bid_quantity=payload.get("bid_quantity"),
            ask_quantity=payload.get("ask_quantity"),
            quote_at=payload.get("quote_at"),
            received_at=received_at,
            provider_sequence=provider_sequence,
        )

    def _build_heartbeat(
        self,
        provider: str,
        channel: str,
        received_at: Any,
        sequence_value: Any,
        payload: Mapping[str, Any],
    ) -> MarketHeartbeat:
        _require_keys(payload, required=_HEARTBEAT_REQUIRED, optional=_HEARTBEAT_OPTIONAL, where="payload")
        provider_sequence = (
            _build_provider_sequence(provider, channel, sequence_value, received_at)
            if sequence_value is not None
            else None
        )
        return _build_model(
            MarketHeartbeat,
            provider=provider,
            channel=channel,
            sent_at=payload.get("sent_at"),
            received_at=received_at,
            provider_sequence=provider_sequence,
        )

    def _track(self, event: MarketEvent) -> None:
        if not self._track_sequence:
            return
        seq = event.provider_sequence
        if seq is None:
            return
        self._tracker.observe(provider=seq.provider, channel=seq.channel, sequence=seq.sequence)


def _build_provider_sequence(
    provider: str,
    channel: str,
    sequence_value: Any,
    received_at: Any,
) -> ProviderSequence:
    return _build_model(
        ProviderSequence,
        provider=provider,
        channel=channel,
        sequence=sequence_value,
        received_at=received_at,
    )


def _build_model(model: type, **fields: Any) -> Any:
    try:
        return model(**fields)
    except ValidationError as exc:
        raise KisWsParseError(_sanitize_validation_error(model.__name__, exc)) from None


def _sanitize_validation_error(model_name: str, exc: ValidationError) -> str:
    # loc + msg만 사용한다. input 값(raw frame/credential)은 절대 포함하지 않는다.
    parts: list[str] = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(item) for item in error.get("loc", ()))
        msg = error.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    detail = "; ".join(parts) if parts else "invalid value"
    return f"{model_name} validation failed: {detail}"


def _require_keys(
    mapping: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str],
    where: str,
) -> None:
    keys = set(mapping.keys())
    allowed = required | optional
    extra = keys - allowed
    if extra:
        names = ", ".join(sorted(extra))
        raise KisWsParseError(f"{where} has unexpected field(s): {names}.")
    missing = required - keys
    if missing:
        names = ", ".join(sorted(missing))
        raise KisWsParseError(f"{where} is missing required field(s): {names}.")


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KisWsParseError(f"{field} must be a non-empty string.")
    return value.strip()


__all__ = [
    "PROVIDER_CONTRACT",
    "KisWsFrameParser",
    "KisWsParseError",
    "MarketDataParserError",
    "SequenceTracker",
    "SequenceViolationError",
]
