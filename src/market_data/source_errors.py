from __future__ import annotations

from collections.abc import Mapping


class MarketSourceIteratorError(Exception):
    """Sanitized market-source iterator failure.

    The public evidence surface must use only ``reason_subcode`` and the optional
    ``parser_metadata`` (sanitized counts/identifiers, never raw frames). Exception
    text is intentionally generic and must not carry frames, URLs, tokens, or secrets.
    """

    reason_subcode = "source_iterator_unknown_after_ack"

    def __init__(self, *, parser_metadata: Mapping[str, object] | None = None) -> None:
        super().__init__(self.reason_subcode)
        self.parser_metadata: dict[str, object] | None = (
            dict(parser_metadata) if parser_metadata else None
        )


class WebSocketClosedAfterAck(MarketSourceIteratorError):
    reason_subcode = "websocket_closed_after_ack"


class WebSocketReceiveTimeoutAfterAck(MarketSourceIteratorError):
    reason_subcode = "websocket_receive_timeout_after_ack"


class WebSocketProtocolErrorAfterAck(MarketSourceIteratorError):
    reason_subcode = "websocket_protocol_error_after_ack"


class MalformedMarketFrameAfterAck(MarketSourceIteratorError):
    """Base for malformed market-frame drops. Concrete subclasses split the failure
    into a sanitized parser stage so evidence pins down the exact mismatch."""

    reason_subcode = "malformed_market_frame_after_ack"


class MalformedQuoteFieldCountAfterAck(MalformedMarketFrameAfterAck):
    reason_subcode = "malformed_quote_field_count_after_ack"


class MalformedTradeFieldCountAfterAck(MalformedMarketFrameAfterAck):
    reason_subcode = "malformed_trade_field_count_after_ack"


class MalformedCountAfterAck(MalformedMarketFrameAfterAck):
    reason_subcode = "malformed_count_after_ack"


class MalformedRequiredFieldAfterAck(MalformedMarketFrameAfterAck):
    reason_subcode = "malformed_required_field_after_ack"


class MalformedLayoutAfterAck(MalformedMarketFrameAfterAck):
    reason_subcode = "malformed_layout_after_ack"


class MalformedControlAfterAck(MalformedMarketFrameAfterAck):
    reason_subcode = "malformed_control_after_ack"


class UnsupportedTrIdAfterAck(MarketSourceIteratorError):
    reason_subcode = "unsupported_tr_id_after_ack"


class SourceIteratorUnknownAfterAck(MarketSourceIteratorError):
    reason_subcode = "source_iterator_unknown_after_ack"


__all__ = [
    "MalformedControlAfterAck",
    "MalformedCountAfterAck",
    "MalformedLayoutAfterAck",
    "MalformedMarketFrameAfterAck",
    "MalformedQuoteFieldCountAfterAck",
    "MalformedRequiredFieldAfterAck",
    "MalformedTradeFieldCountAfterAck",
    "MarketSourceIteratorError",
    "SourceIteratorUnknownAfterAck",
    "UnsupportedTrIdAfterAck",
    "WebSocketClosedAfterAck",
    "WebSocketProtocolErrorAfterAck",
    "WebSocketReceiveTimeoutAfterAck",
]
