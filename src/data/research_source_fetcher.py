from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from decision.canonical_json import canonical_json_dumps
from domain.source import DateIdSourceRecord, FactType


class SourceFetcher(Protocol):
    """read-only research source snapshot normalizer contract (replay-only in 1A)."""

    source_key: str
    fact_types: tuple[FactType, ...]

    def normalize_snapshot(
        self,
        snapshot_path: Path,
        *,
        series_id: str,
        as_of: datetime,
        date_id: str,
    ) -> list[DateIdSourceRecord]:
        """snapshot 파일만 읽어 DateIdSourceRecord 목록을 반환한다. network/store/Date.md write 금지."""
        ...


class UnsupportedSourceError(ValueError):
    """registry에 없는 source_key 요청 시 발생한다."""


def get_source_fetcher(source_key: str) -> SourceFetcher:
    """지원 source_key에 대한 replay fetcher를 반환한다. dynamic plugin 없음."""
    normalized = source_key.strip().lower()
    if normalized == "fred":
        from data.fred_source_fetcher import FredSnapshotReplayFetcher

        return FredSnapshotReplayFetcher()
    raise UnsupportedSourceError(f"unsupported source: {source_key!r}")


def date_id_source_record_to_jsonl_line(record: DateIdSourceRecord) -> str:
    """DateIdSourceRecord를 8B intake와 동일한 canonical JSONL 한 줄로 직렬화한다."""
    return canonical_json_dumps(record.model_dump(mode="json"))


def write_date_id_source_records_jsonl(
    output_path: Path,
    records: list[DateIdSourceRecord],
    *,
    force: bool,
) -> None:
    """DateIdSourceRecord JSONL을 deterministic canonical 형식으로 기록한다."""
    if output_path.exists() and not force:
        raise FileExistsError(f"output JSONL already exists: {output_path} (use --force to overwrite)")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        output_path.write_text("", encoding="utf-8")
        return
    body = "\n".join(date_id_source_record_to_jsonl_line(record) for record in records)
    output_path.write_text(body + "\n", encoding="utf-8")
