from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from decision.canonical_json import canonical_json_dumps
from logs.models import DebugEvent

KST = ZoneInfo("Asia/Seoul")


class DebugMarkdownWriter:
    """docs/DEBUG_EVENT_CODES.md canonical format으로 Debug.md를 append-only 작성한다."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def append_event(self, event: DebugEvent) -> None:
        """DebugEvent 한 건을 Debug.md 끝에 append한다."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        rendered = self.render_event(event)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(rendered)
            if not rendered.endswith("\n"):
                handle.write("\n")

    def render_event(self, event: DebugEvent) -> str:
        """DebugEvent를 canonical Debug.md entry 문자열로 렌더링한다."""
        lines = [
            f"## {event.event_id}",
            f"- timestamp_kst: {_format_timestamp_kst(event.timestamp_kst)}",
            f"- source: {event.source.value}",
            f"- severity: {event.severity.value}",
            f"- event_code: {event.event_code}",
            f"- detail: {event.detail}",
        ]

        optional_bullets = (
            ("action_taken", event.action_taken),
            ("fallback", event.fallback),
            ("related_file", event.related_file),
            ("human_note", event.human_note),
            ("run_id", event.run_id),
            ("decision_id", event.decision_id.value if event.decision_id else None),
            ("order_id", event.order_id),
            ("symbol", event.symbol),
            ("market", event.market),
            ("exception_type", event.exception_type),
        )
        for key, value in optional_bullets:
            if value is not None:
                lines.append(f"- {key}: {value}")

        if event.validation_issue_codes:
            codes_json = canonical_json_dumps(list(event.validation_issue_codes))
            lines.append(f"- validation_issue_codes: {codes_json}")

        if event.metadata:
            lines.append(f"- metadata: {canonical_json_dumps(event.metadata)}")

        return "\n".join(lines) + "\n"


def _format_timestamp_kst(value: datetime) -> str:
    """timezone-aware datetime을 KST ISO8601 문자열로 변환한다."""
    return value.astimezone(KST).isoformat()
