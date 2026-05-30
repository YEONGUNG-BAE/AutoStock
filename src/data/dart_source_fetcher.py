from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.identifiers import DateId
from domain.source import DateIdSourceRecord, FactType
from data.dart_adapter import DartDisclosureAdapter
from data.date_id_generator import DateIdGenerator
from data.date_id_store import SQLiteDateIdSourceStore
from data.market_data import DisclosureRecord, disclosure_record_to_source_record


class DartSnapshotReplayClient:
    """DART disclosure snapshot replay client. network/API key 호출 없음."""

    def __init__(self, snapshot_path: Path) -> None:
        self._snapshot = _load_snapshot_object(snapshot_path)
        self._snapshot_symbol = _require_snapshot_symbol(self._snapshot)
        _require_snapshot_source_key(self._snapshot)
        self._disclosures = _require_disclosures_list(self._snapshot)

    def get_recent_disclosures(self, symbol: str, limit: int) -> tuple[Mapping[str, Any], ...]:
        requested_symbol = normalize_required_string(symbol, field_name="symbol")
        if requested_symbol != self._snapshot_symbol:
            raise ValueError(
                "snapshot symbol mismatch: "
                f"snapshot has {self._snapshot_symbol!r}, requested {requested_symbol!r}"
            )
        if limit <= 0:
            raise ValueError("limit must be greater than 0")

        selected = self._disclosures[:limit]
        return tuple(_coerce_disclosure_item(item) for item in selected)


class DartDisclosureSnapshotReplayFetcher:
    """DART-like local snapshot → DateIdSourceRecord replay fetcher (3A). store write 금지."""

    source_key = "dart"

    @property
    def fact_types(self) -> tuple[FactType, ...]:
        return (FactType.DISCLOSURE,)

    def normalize_snapshot(
        self,
        snapshot_path: Path,
        *,
        symbol: str,
        as_of: datetime,
        store: SQLiteDateIdSourceStore,
        limit: int = 10,
    ) -> list[DateIdSourceRecord]:
        """local DART snapshot JSON을 DisclosureRecord 경유 DateIdSourceRecord 목록으로 변환한다."""
        if limit <= 0:
            raise ValueError("limit must be greater than 0")

        aware_as_of = require_timezone_aware_datetime(as_of, field_name="as_of")
        adapter = DartDisclosureAdapter(DartSnapshotReplayClient(snapshot_path))
        disclosure_records = adapter.fetch_recent_disclosures(
            symbol,
            as_of=aware_as_of,
            limit=limit,
        )
        if not disclosure_records:
            return []

        date_ids = allocate_date_ids_for_records(disclosure_records, store=store)
        return [
            disclosure_record_to_source_record(record, date_id)
            for record, date_id in zip(disclosure_records, date_ids, strict=True)
        ]


def allocate_date_ids_for_records(
    records: Sequence[DisclosureRecord],
    *,
    store: SQLiteDateIdSourceStore,
) -> list[DateId]:
    """store-seeded DateIdGenerator + in-memory batch reservation으로 고유 Date-ID를 할당한다."""
    generator = DateIdGenerator(store)
    next_sequence_by_prefix: dict[str, int] = {}
    allocated: list[DateId] = []

    for record in records:
        seed = generator.next_id(record.source_timestamp)
        prefix, _, sequence_text = seed.value.partition("-")
        if not sequence_text.isdigit():
            raise ValueError(f"invalid seed Date-ID sequence: {seed.value!r}")
        seed_sequence = int(sequence_text)

        if prefix not in next_sequence_by_prefix:
            next_sequence_by_prefix[prefix] = seed_sequence
        else:
            next_sequence_by_prefix[prefix] += 1

        sequence = next_sequence_by_prefix[prefix]
        allocated.append(DateId(f"{prefix}-{sequence}"))

    return allocated


def _load_snapshot_object(snapshot_path: Path) -> dict[str, Any]:
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"snapshot not found: {snapshot_path}")
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid snapshot JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("snapshot root must be a JSON object")
    return payload


def _require_snapshot_source_key(snapshot: Mapping[str, Any]) -> None:
    source_key = snapshot.get("source_key")
    if source_key is None:
        raise ValueError("snapshot source_key is required")
    normalized = normalize_required_string(source_key, field_name="source_key")
    if normalized != "dart":
        raise ValueError(f"snapshot source_key must be 'dart', got {normalized!r}")


def _require_snapshot_symbol(snapshot: Mapping[str, Any]) -> str:
    symbol = snapshot.get("symbol")
    if symbol is None:
        raise ValueError("snapshot symbol is required")
    return normalize_required_string(symbol, field_name="symbol")


def _require_disclosures_list(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    disclosures = snapshot.get("disclosures")
    if disclosures is None:
        raise ValueError("snapshot disclosures is required")
    if not isinstance(disclosures, list):
        raise ValueError("snapshot disclosures must be a JSON array")
    normalized: list[Mapping[str, Any]] = []
    for index, item in enumerate(disclosures):
        if not isinstance(item, dict):
            raise ValueError(f"snapshot disclosures[{index}] must be a JSON object")
        normalized.append(item)
    return normalized


def _coerce_disclosure_item(item: Mapping[str, Any]) -> dict[str, object]:
    """snapshot disclosure JSON을 DartDisclosureAdapter가 기대하는 mapping 형태로 변환한다."""
    coerced: dict[str, object] = dict(item)
    source_timestamp = coerced.get("source_timestamp")
    if source_timestamp is not None and not isinstance(source_timestamp, datetime):
        coerced["source_timestamp"] = parse_timezone_aware_datetime(
            source_timestamp,
            field_name="source_timestamp",
        )
    return coerced
