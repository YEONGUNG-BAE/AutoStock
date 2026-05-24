from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from decision.canonical_json import canonical_json_dumps
from logs.models import DebugEvent


class JsonlEventLog:
    """DebugEvent append-only JSONL 저장소."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: DebugEvent) -> None:
        """이벤트 한 건을 JSONL 끝에 append한다."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json_dumps(event.to_canonical_dict())
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")

    def list_events(self) -> tuple[DebugEvent, ...]:
        """저장된 모든 이벤트를 write order대로 반환한다."""
        return tuple(self.iter_events())

    def iter_events(self) -> Iterator[DebugEvent]:
        """저장된 이벤트를 write order대로 순회한다."""
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

                yield DebugEvent.model_validate(payload)
