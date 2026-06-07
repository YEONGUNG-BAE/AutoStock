from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from market_data.models import MarketEvent
from market_data.protocols import MarketEventSource

__all__ = ["ReplayMarketEventSource"]


class ReplayMarketEventSource(MarketEventSource):
    """이미 정규화·로드된 MarketEvent 시퀀스를 순서대로 재생하는 결정론적 소스.

    filesystem이나 network를 직접 열지 않는다. 파일 I/O는 ops CLI가 담당하고,
    이 클래스는 메모리에 적재된 이벤트 iterable만 받는다. 시퀀스를 모두 소진하면
    async iterator가 정상 종료(EOF)되어 monitor가 graceful stop으로 처리한다.
    매 호출마다 새 iterator를 반환하므로 재접속(reconnect) 시 fresh epoch로 재생된다.
    """

    def __init__(self, events: Iterable[MarketEvent]) -> None:
        self._events: tuple[MarketEvent, ...] = tuple(events)

    async def events(self) -> AsyncIterator[MarketEvent]:
        for event in self._events:
            yield event
