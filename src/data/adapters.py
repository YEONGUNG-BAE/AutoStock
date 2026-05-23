from __future__ import annotations

from datetime import datetime
from typing import Protocol

from data.market_data import DisclosureRecord, MacroDataPoint, MarketDataPoint


class MarketDataAdapter(Protocol):
    """시장 가격 read-only adapter protocol. 주문/브로커 호출을 하지 않는다."""

    def fetch_latest_price(self, symbol: str, *, as_of: datetime) -> MarketDataPoint:
        """symbol의 최신 가격을 read-only로 조회한다."""
        ...


class MacroDataAdapter(Protocol):
    """FRED macro observation read-only adapter protocol."""

    def fetch_latest_observation(self, series_id: str, *, as_of: datetime) -> MacroDataPoint:
        """series_id의 최신 macro observation을 read-only로 조회한다."""
        ...


class DisclosureDataAdapter(Protocol):
    """DART 공시 read-only adapter protocol."""

    def fetch_recent_disclosures(
        self,
        symbol: str,
        *,
        as_of: datetime,
        limit: int = 10,
    ) -> tuple[DisclosureRecord, ...]:
        """symbol의 최근 공시 목록을 read-only로 조회한다."""
        ...
