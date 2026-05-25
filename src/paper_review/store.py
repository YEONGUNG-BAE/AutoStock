from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from decision.canonical_json import canonical_json_dumps
from paper_review.models import PaperReviewReport


class PaperReviewReportStore:
    """PaperReviewReport append-only JSONL 저장소. duplicate review_id는 거부한다."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def save(self, report: PaperReviewReport) -> None:
        """PaperReviewReport 한 건을 append한다. duplicate review_id는 ValueError."""
        existing = self.get(report.review_id)
        if existing is not None:
            raise ValueError(f"duplicate review_id: {report.review_id}")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json_dumps(report.to_canonical_dict())
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")

    def get(self, review_id: str) -> PaperReviewReport | None:
        """review_id로 저장된 PaperReviewReport를 조회한다."""
        for report in self.iter_reports():
            if report.review_id == review_id:
                return report
        return None

    def iter_reports(self) -> Iterator[PaperReviewReport]:
        """저장된 PaperReviewReport를 write order대로 순회한다."""
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

                yield _report_from_canonical_dict(payload)

    def list_reports(self) -> tuple[PaperReviewReport, ...]:
        """저장된 PaperReviewReport를 write order대로 반환한다."""
        return tuple(self.iter_reports())


def _report_from_canonical_dict(payload: dict[str, object]) -> PaperReviewReport:
    """canonical dict에서 PaperReviewReport를 복원한다."""
    return PaperReviewReport.model_validate(payload)
