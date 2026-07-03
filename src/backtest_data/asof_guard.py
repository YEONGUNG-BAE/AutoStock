"""Phase 2a as-of look-ahead guard.

The as-of guard is a backtest-only read-only view. It does not modify
ScoutInputBuilder, SQLiteDateIdSourceStore, or runtime scout behavior.

Phase 2a is strategy-agnostic. It preserves original symbols, markets,
timestamps, source names, and value fields. It does not implement LLM input
masking, strategy execution, benchmark scoring, derived feature fitting, or
normalization. A later Phase 2c LLM input adapter may create anonymized or
feature-rich masked views from these preserved original fields.

Given a decision time ``d``, the view exposes only records with

    record.source_timestamp <= d

The boundary is INCLUSIVE: a record with ``source_timestamp == d`` is
included; a record one instant after ``d`` is excluded. This prevents a
future walk-forward engine from passing future-dated records into the
existing scout layer. The view satisfies the ``DateIdSourceStoreReader``
protocol shape consumed by the unmodified ``ScoutInputBuilder``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol, runtime_checkable

from domain._datetime import require_timezone_aware_datetime
from domain.source import DateIdSourceRecord, FactType


@runtime_checkable
class _SourceReader(Protocol):
    """list_records()를 노출하는 read-only source. 수정하지 않고 읽기만 한다."""

    def list_records(self, fact_type: FactType | None = None) -> tuple[DateIdSourceRecord, ...]: ...


class AsOfFilteredSourceView:
    """decision_time 기준 look-ahead-safe read-only view (backtest 전용).

    Accepts either a reader exposing ``list_records()`` (e.g. the existing
    SQLiteDateIdSourceStore, read-only) or a plain iterable of
    ``DateIdSourceRecord``. The underlying store/collection is never
    mutated; records are snapshotted at construction for deterministic,
    current-time-independent behavior. Records are kept in deterministic
    ascending ``(source_timestamp, date_id)`` order internally, but
    correctness does not depend on it: ScoutInputBuilder re-sorts records.
    """

    def __init__(
        self,
        source: _SourceReader | Iterable[DateIdSourceRecord],
        *,
        decision_time: datetime,
    ) -> None:
        self._decision_time = require_timezone_aware_datetime(
            decision_time, field_name="decision_time"
        )
        if isinstance(source, _SourceReader):
            records: tuple[DateIdSourceRecord, ...] = tuple(source.list_records())
        else:
            records = tuple(source)
        for record in records:
            if not isinstance(record, DateIdSourceRecord):
                raise ValueError("source must contain DateIdSourceRecord objects only.")
        self._records = tuple(
            sorted(
                records,
                key=lambda record: (record.source_timestamp, record.date_id.value),
            )
        )

    @property
    def decision_time(self) -> datetime:
        return self._decision_time

    def list_records(self, fact_type: FactType | None = None) -> tuple[DateIdSourceRecord, ...]:
        """source_timestamp <= decision_time (inclusive)인 record만 반환한다.

        The optional ``fact_type`` filter mirrors the existing
        ``DateIdSourceStoreReader`` protocol shape.
        """
        return tuple(
            record
            for record in self._records
            if record.source_timestamp <= self._decision_time
            and (fact_type is None or record.fact_type == fact_type)
        )
