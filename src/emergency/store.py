from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from decision.canonical_json import canonical_json_dumps
from domain.enums import Market
from emergency.models import EmergencyTriggerEvent, EmergencyTriggerStatus, EmergencyTriggerType


class EmergencyEventStore:
    """EmergencyTriggerEvent append-only JSONL 저장소. duplicate event_id는 거부한다."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def save(self, event: EmergencyTriggerEvent) -> None:
        """EmergencyTriggerEvent 한 건을 append한다. duplicate event_id는 ValueError."""
        existing = self.get(event.event_id)
        if existing is not None:
            raise ValueError(f"duplicate event_id: {event.event_id}")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json_dumps(event.to_canonical_dict())
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")

    def get(self, event_id: str) -> EmergencyTriggerEvent | None:
        """event_id로 저장된 EmergencyTriggerEvent를 조회한다."""
        for event in self.iter_events():
            if event.event_id == event_id:
                return event
        return None

    def iter_events(self) -> Iterator[EmergencyTriggerEvent]:
        """저장된 EmergencyTriggerEvent를 write order대로 순회한다."""
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

                yield EmergencyTriggerEvent.model_validate(payload)

    def list_events(
        self,
        *,
        trigger_type: EmergencyTriggerType | None = None,
        market: Market | None = None,
        symbol: str | None = None,
        status: EmergencyTriggerStatus | None = None,
    ) -> tuple[EmergencyTriggerEvent, ...]:
        """저장된 EmergencyTriggerEvent를 write order대로 반환한다. optional filter 지원."""
        events: list[EmergencyTriggerEvent] = []
        for event in self.iter_events():
            payload = event.payload
            if trigger_type is not None and payload.trigger_type != trigger_type:
                continue
            if market is not None and payload.market != market:
                continue
            if symbol is not None and payload.symbol != symbol:
                continue
            if status is not None and payload.status != status:
                continue
            events.append(event)
        return tuple(events)


class MddEventLog(EmergencyEventStore):
    """MDD event log foundation. EmergencyEventStore와 동일한 JSONL semantics."""

    def list_mdd_events(self) -> tuple[EmergencyTriggerEvent, ...]:
        """MDD_KILLSWITCH event만 반환한다."""
        return self.list_events(trigger_type=EmergencyTriggerType.MDD_KILLSWITCH)
