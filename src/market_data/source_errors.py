from __future__ import annotations


class MarketSourceIteratorError(Exception):
    """Sanitized market-source iterator failure.

    The public evidence surface must use only ``reason_subcode``. Exception text
    is intentionally generic and must not carry frames, URLs, tokens, or secrets.
    """

    reason_subcode = "source_iterator_unknown_after_ack"

    def __init__(self) -> None:
        super().__init__(self.reason_subcode)


class WebSocketClosedAfterAck(MarketSourceIteratorError):
    reason_subcode = "websocket_closed_after_ack"


class WebSocketReceiveTimeoutAfterAck(MarketSourceIteratorError):
    reason_subcode = "websocket_receive_timeout_after_ack"


class WebSocketProtocolErrorAfterAck(MarketSourceIteratorError):
    reason_subcode = "websocket_protocol_error_after_ack"


class MalformedMarketFrameAfterAck(MarketSourceIteratorError):
    reason_subcode = "malformed_market_frame_after_ack"


class UnsupportedTrIdAfterAck(MarketSourceIteratorError):
    reason_subcode = "unsupported_tr_id_after_ack"


class SourceIteratorUnknownAfterAck(MarketSourceIteratorError):
    reason_subcode = "source_iterator_unknown_after_ack"


__all__ = [
    "MalformedMarketFrameAfterAck",
    "MarketSourceIteratorError",
    "SourceIteratorUnknownAfterAck",
    "UnsupportedTrIdAfterAck",
    "WebSocketClosedAfterAck",
    "WebSocketProtocolErrorAfterAck",
    "WebSocketReceiveTimeoutAfterAck",
]
