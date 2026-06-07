from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from market_data.models import MarketEvent


@runtime_checkable
class MarketEventSource(Protocol):
    """정규화된 MarketEvent를 read-only로 흘려보내는 비동기 소스 인터페이스다.

    RTM-1 단계에서는 network/transport 구현을 포함하지 않는다. fixture/fake
    source가 이 인터페이스를 구현하고, 실제 KIS WebSocket transport는 RTM-6로
    유보된다.
    """

    def events(self) -> AsyncIterator[MarketEvent]:
        """정규화된 MarketEvent를 순서대로 yield하는 async iterator를 반환한다."""
        ...


__all__ = ["MarketEventSource"]
