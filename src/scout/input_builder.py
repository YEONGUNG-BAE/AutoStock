from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Protocol

from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.source import DateIdSourceRecord, FactType
from scout.models import ScoutInput, ScoutInputRecord


class DateIdSourceStoreReader(Protocol):
    """ScoutInputBuilder가 Date-ID source layer를 read-only로 읽기 위한 protocol."""

    def list_records(self, fact_type: FactType | None = None) -> tuple[DateIdSourceRecord, ...]:
        """저장된 DateIdSourceRecord 목록을 반환한다."""
        ...


class ScoutInputBuilder:
    """DateIdSourceRecord를 deterministic Scout 입력 payload로 조립한다."""

    def __init__(self, store: DateIdSourceStoreReader) -> None:
        self._store = store

    def build_input(
        self,
        *,
        universe: str,
        now: datetime,
        fact_types: Iterable[FactType] | None = None,
        symbols: Iterable[str] | None = None,
        max_records: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ScoutInput:
        """store의 DateIdSourceRecord를 ScoutInput으로 조립한다. read-only, stale 판정 없음."""
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        normalized_universe = normalize_required_string(universe, field_name="universe")

        if max_records is not None:
            if not isinstance(max_records, int) or isinstance(max_records, bool):
                raise ValueError("max_records must be a positive integer.")
            if max_records < 1:
                raise ValueError("max_records must be a positive integer.")

        fact_type_filter = _normalize_fact_type_filter(fact_types)
        symbol_filter = _normalize_symbol_filter(symbols)

        records = self._store.list_records()
        filtered = _filter_records(records, fact_type_filter=fact_type_filter, symbol_filter=symbol_filter)
        sorted_records = _sort_records_deterministic(filtered)

        if max_records is not None:
            sorted_records = sorted_records[:max_records]

        input_records = tuple(_source_record_to_input_record(record) for record in sorted_records)
        input_metadata = {} if metadata is None else metadata

        return ScoutInput(
            created_at=aware_now,
            universe=normalized_universe,
            records=input_records,
            metadata=input_metadata,
        )


def _normalize_fact_type_filter(
    fact_types: Iterable[FactType] | None,
) -> frozenset[FactType] | None:
    if fact_types is None:
        return None
    normalized = frozenset(fact_types)
    if not normalized:
        raise ValueError("fact_types filter must not be empty when provided.")
    return normalized


def _normalize_symbol_filter(symbols: Iterable[str] | None) -> frozenset[str] | None:
    """symbol 필터는 strip 후 exact case-sensitive 비교를 사용한다."""
    if symbols is None:
        return None
    normalized: set[str] = set()
    for raw_symbol in symbols:
        if not isinstance(raw_symbol, str):
            raise ValueError("symbol filter values must be strings.")
        stripped = raw_symbol.strip()
        if not stripped:
            raise ValueError("symbol filter must not contain blank values.")
        normalized.add(stripped)
    if not normalized:
        raise ValueError("symbols filter must not be empty when provided.")
    return frozenset(normalized)


def _filter_records(
    records: tuple[DateIdSourceRecord, ...],
    *,
    fact_type_filter: frozenset[FactType] | None,
    symbol_filter: frozenset[str] | None,
) -> tuple[DateIdSourceRecord, ...]:
    filtered: list[DateIdSourceRecord] = []
    for record in records:
        if fact_type_filter is not None and record.fact_type not in fact_type_filter:
            continue
        if symbol_filter is not None:
            if record.symbol is None or record.symbol not in symbol_filter:
                continue
        filtered.append(record)
    return tuple(filtered)


def _sort_records_deterministic(
    records: tuple[DateIdSourceRecord, ...],
) -> tuple[DateIdSourceRecord, ...]:
    """source_timestamp DESC, date_id ASC 순으로 deterministic 정렬한다."""
    return tuple(
        sorted(
            records,
            key=lambda record: (-record.source_timestamp.timestamp(), record.date_id.value),
        )
    )


def _source_record_to_input_record(record: DateIdSourceRecord) -> ScoutInputRecord:
    return ScoutInputRecord(
        date_id=record.date_id,
        fact_type=record.fact_type,
        source_name=record.source_name,
        source_timestamp=record.source_timestamp,
        summary=record.summary,
        symbol=record.symbol,
        market=record.market,
        source_url=record.source_url,
        payload=record.payload,
    )
