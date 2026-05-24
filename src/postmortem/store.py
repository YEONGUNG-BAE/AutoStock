from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from decision.canonical_json import canonical_json_dumps
from postmortem.models import (
    PostmortemKind,
    PostmortemMarket,
    PostmortemRecord,
    PostmortemTagSummary,
)


class PostmortemRecordStore:
    """PostmortemRecord append-only JSONL 저장소. duplicate postmortem_id는 거부한다."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def save(self, record: PostmortemRecord) -> None:
        """PostmortemRecord 한 건을 append한다. duplicate postmortem_id는 ValueError."""
        existing = self.get(record.postmortem_id)
        if existing is not None:
            raise ValueError(f"duplicate postmortem_id: {record.postmortem_id}")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json_dumps(record.to_canonical_dict())
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")

    def get(self, postmortem_id: str) -> PostmortemRecord | None:
        """postmortem_id로 저장된 PostmortemRecord를 조회한다."""
        for record in self.iter_records():
            if record.postmortem_id == postmortem_id:
                return record
        return None

    def iter_records(self) -> Iterator[PostmortemRecord]:
        """저장된 PostmortemRecord를 write order대로 순회한다."""
        if not self._path.exists():
            return

        with self._path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid JSONL row at line {line_number} in {self._path}"
                    ) from exc

                if not isinstance(payload, dict):
                    raise ValueError(
                        f"invalid JSONL row at line {line_number} in {self._path}: "
                        "row must be a JSON object."
                    )

                yield PostmortemRecord.model_validate(payload)

    def list_records(
        self,
        *,
        market: PostmortemMarket | None = None,
        kind: PostmortemKind | None = None,
        period: str | None = None,
    ) -> tuple[PostmortemRecord, ...]:
        """저장된 PostmortemRecord를 write order대로 반환한다. optional filter 지원."""
        records: list[PostmortemRecord] = []
        for record in self.iter_records():
            if market is not None and record.market != market:
                continue
            if kind is not None and record.kind != kind:
                continue
            if period is not None and record.period != period:
                continue
            records.append(record)
        return tuple(records)

    def list_tag_summaries(
        self,
        *,
        market: PostmortemMarket | None = None,
        kind: PostmortemKind | None = None,
        period: str | None = None,
    ) -> tuple[PostmortemTagSummary, ...]:
        """저장된 PostmortemRecord의 tag_summary를 write order대로 반환한다."""
        return tuple(
            record.tag_summary
            for record in self.list_records(market=market, kind=kind, period=period)
        )
