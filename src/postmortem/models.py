from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from decision.canonical_json import canonicalize_payload, payload_sha256
from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.identifiers import DateId, DecisionId
from postmortem.error_tags import validate_postmortem_error_tags

# ISO week period: YYYY-Www (zero-padded week)
_WEEKLY_PERIOD_PATTERN = re.compile(r"^(\d{4})-W(\d{2})$")
# Monthly period: YYYY-MM (zero-padded month)
_MONTHLY_PERIOD_PATTERN = re.compile(r"^(\d{4})-(\d{2})$")

_TOP_ERROR_TAGS_LIMIT = 3


class PostmortemMarket(StrEnum):
    """Postmortem 시장 구분."""

    KR = "KR"
    US = "US"


class PostmortemKind(StrEnum):
    """Postmortem 주기 구분."""

    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class PostmortemSource(StrEnum):
    """Postmortem tag summary source."""

    WEEKLY_POSTMORTEM = "WeeklyPostmortem"
    MONTHLY_POSTMORTEM = "MonthlyPostmortem"


def parse_weekly_period(period: str) -> tuple[int, int]:
    """ISO week period 문자열을 (iso_year, iso_week)로 파싱한다."""
    match = _WEEKLY_PERIOD_PATTERN.match(period)
    if match is None:
        raise ValueError(f"invalid weekly period format: {period!r}")

    iso_year = int(match.group(1))
    iso_week = int(match.group(2))
    if iso_week < 1 or iso_week > 53:
        raise ValueError(f"invalid ISO week number in period: {period!r}")

    # fromisocalendar로 유효성 검증 (예: W00, 존재하지 않는 week reject)
    try:
        date.fromisocalendar(iso_year, iso_week, 1)
    except ValueError as exc:
        raise ValueError(f"invalid ISO week in period: {period!r}") from exc

    return iso_year, iso_week


def parse_monthly_period(period: str) -> tuple[int, int]:
    """Monthly period 문자열을 (year, month)로 파싱한다."""
    match = _MONTHLY_PERIOD_PATTERN.match(period)
    if match is None:
        raise ValueError(f"invalid monthly period format: {period!r}")

    year = int(match.group(1))
    month = int(match.group(2))
    if month < 1 or month > 12:
        raise ValueError(f"invalid month in period: {period!r}")

    return year, month


def derive_top_error_tags(error_tags: dict[str, int], *, limit: int = _TOP_ERROR_TAGS_LIMIT) -> tuple[str, ...]:
    """error_tags count 내림차순, 동률 시 tag 문자열 오름차순으로 top N tag를 반환한다."""
    if limit < 1:
        raise ValueError("limit must be >= 1.")

    ranked = sorted(error_tags.items(), key=lambda item: (-item[1], item[0]))
    return tuple(tag for tag, _ in ranked[:limit])


def build_postmortem_id(*, kind: PostmortemKind, market: PostmortemMarket, period: str) -> str:
    """deterministic postmortem_id를 생성한다."""
    return f"postmortem-{kind.value.lower()}-{market.value.lower()}-{period}"


def _iso_week_of(value: date) -> tuple[int, int]:
    """date의 ISO (year, week)를 반환한다."""
    iso = value.isocalendar()
    return iso.year, iso.week


def _validate_weekly_period_dates(
    *,
    period: str,
    evaluated_start_date: date,
    evaluated_end_date: date,
) -> None:
    """weekly period와 evaluated date range 일관성을 검증한다."""
    iso_year, iso_week = parse_weekly_period(period)
    expected_start = date.fromisocalendar(iso_year, iso_week, 1)
    expected_end = date.fromisocalendar(iso_year, iso_week, 7)

    if evaluated_start_date > evaluated_end_date:
        raise ValueError("evaluated_start_date must be <= evaluated_end_date.")

    if evaluated_start_date != expected_start:
        raise ValueError(
            f"evaluated_start_date must be Monday of ISO week {period}: "
            f"expected {expected_start.isoformat()}."
        )

    if evaluated_end_date != expected_end:
        raise ValueError(
            f"evaluated_end_date must be Sunday of ISO week {period}: "
            f"expected {expected_end.isoformat()}."
        )

    start_week = _iso_week_of(evaluated_start_date)
    end_week = _iso_week_of(evaluated_end_date)
    if start_week != (iso_year, iso_week) or end_week != (iso_year, iso_week):
        raise ValueError(
            f"evaluated date range must fall within ISO week {period}."
        )


def _validate_monthly_period_dates(
    *,
    period: str,
    evaluated_start_date: date,
    evaluated_end_date: date,
) -> None:
    """monthly period와 evaluated date range 일관성을 검증한다."""
    year, month = parse_monthly_period(period)
    expected_start = date(year, month, 1)
    _, last_day = calendar.monthrange(year, month)
    expected_end = date(year, month, last_day)

    if evaluated_start_date > evaluated_end_date:
        raise ValueError("evaluated_start_date must be <= evaluated_end_date.")

    if evaluated_start_date != expected_start:
        raise ValueError(
            f"evaluated_start_date must be first day of month {period}: "
            f"expected {expected_start.isoformat()}."
        )

    if evaluated_end_date != expected_end:
        raise ValueError(
            f"evaluated_end_date must be last day of month {period}: "
            f"expected {expected_end.isoformat()}."
        )

    if (
        evaluated_start_date.year != year
        or evaluated_start_date.month != month
        or evaluated_end_date.year != year
        or evaluated_end_date.month != month
    ):
        raise ValueError(f"evaluated date range must fall within month {period}.")


class PostmortemEvaluation(BaseModel):
    """rule 08 evaluation criteria를 구조화한다. 자동 error tag 생성은 하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    price_result: str
    benchmark_relative_result: str
    evidence_validity: str
    date_id_interpretation_accuracy: str
    reasoning_action_consistency: str
    python_rule_outcome: str
    thesis_validity: str

    @field_validator(
        "price_result",
        "benchmark_relative_result",
        "evidence_validity",
        "date_id_interpretation_accuracy",
        "reasoning_action_consistency",
        "python_rule_outcome",
        "thesis_validity",
        mode="before",
    )
    @classmethod
    def validate_required_text(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)


class PostmortemTagSummary(BaseModel):
    """Postmortem markdown 끝 machine-readable tag summary block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: PostmortemMarket
    period: str
    source: PostmortemSource
    error_tags: dict[str, int]
    top_error_tags: tuple[str, ...] = ()

    @field_validator("period", mode="before")
    @classmethod
    def validate_period_string(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("period must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValueError("period must not be blank.")
        if normalized != value:
            raise ValueError("period must not contain leading or trailing whitespace.")
        return normalized

    @field_validator("error_tags", mode="before")
    @classmethod
    def validate_error_tags(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            raise ValueError("error_tags must be a mapping of tag -> count.")
        return validate_postmortem_error_tags(value)

    @field_validator("top_error_tags", mode="before")
    @classmethod
    def validate_top_error_tags_input(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("top_error_tags must be a sequence of strings.")

        normalized: list[str] = []
        for index, item in enumerate(value):
            normalized.append(
                normalize_required_string(item, field_name=f"top_error_tags[{index}]")
            )
        return tuple(normalized)

    @model_validator(mode="before")
    @classmethod
    def inject_missing_top_error_tags(cls, data: Any) -> Any:
        """top_error_tags가 없으면 error_tags에서 deterministic derivation을 주입한다."""
        if not isinstance(data, dict):
            return data

        error_tags = data.get("error_tags")
        top_error_tags = data.get("top_error_tags")
        if error_tags is not None and not top_error_tags:
            validated_tags = validate_postmortem_error_tags(error_tags)
            data["top_error_tags"] = list(derive_top_error_tags(validated_tags))

        return data

    @model_validator(mode="after")
    def validate_period_source_and_top_tags(self) -> Self:
        # period 형식은 source(kind)에 맞아야 한다.
        if self.source == PostmortemSource.WEEKLY_POSTMORTEM:
            parse_weekly_period(self.period)
        elif self.source == PostmortemSource.MONTHLY_POSTMORTEM:
            parse_monthly_period(self.period)
        else:
            raise ValueError(f"unsupported postmortem source: {self.source!r}")

        if len(self.top_error_tags) > _TOP_ERROR_TAGS_LIMIT:
            raise ValueError(f"top_error_tags must contain at most {_TOP_ERROR_TAGS_LIMIT} tags.")

        derived = derive_top_error_tags(self.error_tags)
        if self.top_error_tags != derived:
            raise ValueError(
                "top_error_tags must match deterministic derivation from error_tags."
            )

        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic canonical dict 표현을 반환한다."""
        payload: dict[str, Any] = {
            "market": self.market.value,
            "period": self.period,
            "source": self.source.value,
            "error_tags": dict(self.error_tags),
            "top_error_tags": list(self.top_error_tags),
        }
        return canonicalize_payload(payload)


class PostmortemRecord(BaseModel):
    """Weekly/Monthly Postmortem 저장 레코드."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    postmortem_id: str
    market: PostmortemMarket
    kind: PostmortemKind
    period: str
    created_at: datetime
    evaluated_start_date: date
    evaluated_end_date: date
    summary: str
    evaluation: PostmortemEvaluation
    findings: tuple[str, ...]
    lessons: tuple[str, ...]
    tag_summary: PostmortemTagSummary
    daily_summary_ids: tuple[str, ...] = ()
    date_ids_used: tuple[DateId, ...] = ()
    decision_snapshot_ids: tuple[DecisionId, ...] = ()
    fill_ids: tuple[str, ...] = ()
    nav_snapshot_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("postmortem_id", "summary", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("period", mode="before")
    @classmethod
    def validate_period(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("period must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValueError("period must not be blank.")
        if normalized != value:
            raise ValueError("period must not contain leading or trailing whitespace.")
        return normalized

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return require_timezone_aware_datetime(value, field_name="created_at")

    @field_validator("evaluated_start_date", "evaluated_end_date", mode="before")
    @classmethod
    def validate_evaluated_dates(cls, value: Any, info) -> date:
        if isinstance(value, str):
            return date.fromisoformat(value)
        if isinstance(value, date):
            return value
        raise ValueError(f"{info.field_name} must be a date.")

    @field_validator("findings", "lessons", mode="before")
    @classmethod
    def validate_text_sequences(cls, value: Any, info) -> tuple[str, ...]:
        if value is None:
            raise ValueError(f"{info.field_name} must not be empty.")
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{info.field_name} must be a sequence of strings.")
        if len(value) == 0:
            raise ValueError(f"{info.field_name} must not be empty.")

        normalized: list[str] = []
        for index, item in enumerate(value):
            normalized.append(
                normalize_required_string(item, field_name=f"{info.field_name}[{index}]")
            )
        return tuple(normalized)

    @field_validator("daily_summary_ids", "fill_ids", "nav_snapshot_ids", mode="before")
    @classmethod
    def validate_id_sequences(cls, value: Any, info) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{info.field_name} must be a sequence of strings.")

        normalized: list[str] = []
        for index, item in enumerate(value):
            normalized.append(
                normalize_required_string(item, field_name=f"{info.field_name}[{index}]")
            )
        return tuple(normalized)

    @field_validator("date_ids_used", mode="before")
    @classmethod
    def validate_date_ids_used(cls, value: Any) -> tuple[DateId, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("date_ids_used must be a sequence of DateId.")

        normalized: list[DateId] = []
        for item in value:
            if isinstance(item, DateId):
                normalized.append(item)
            else:
                normalized.append(DateId(str(item)))
        unique_by_value = {item.value: item for item in normalized}
        return tuple(unique_by_value[item] for item in sorted(unique_by_value))

    @field_validator("decision_snapshot_ids", mode="before")
    @classmethod
    def validate_decision_snapshot_ids(cls, value: Any) -> tuple[DecisionId, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("decision_snapshot_ids must be a sequence of DecisionId.")

        normalized: list[DecisionId] = []
        for item in value:
            if isinstance(item, DecisionId):
                normalized.append(item)
            else:
                normalized.append(DecisionId(str(item)))
        unique_by_value = {item.value: item for item in normalized}
        return tuple(unique_by_value[item] for item in sorted(unique_by_value))

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value

    @model_validator(mode="after")
    def validate_record_consistency(self) -> Self:
        # metadata canonical form
        expected_metadata = canonicalize_payload(self.metadata)
        if self.metadata != expected_metadata:
            raise ValueError("metadata must be in canonical JSON-compatible form.")

        if self.evaluated_start_date > self.evaluated_end_date:
            raise ValueError("evaluated_start_date must be <= evaluated_end_date.")

        # period 형식은 kind에 맞아야 한다.
        if self.kind == PostmortemKind.WEEKLY:
            parse_weekly_period(self.period)
            _validate_weekly_period_dates(
                period=self.period,
                evaluated_start_date=self.evaluated_start_date,
                evaluated_end_date=self.evaluated_end_date,
            )
            expected_source = PostmortemSource.WEEKLY_POSTMORTEM
        elif self.kind == PostmortemKind.MONTHLY:
            parse_monthly_period(self.period)
            _validate_monthly_period_dates(
                period=self.period,
                evaluated_start_date=self.evaluated_start_date,
                evaluated_end_date=self.evaluated_end_date,
            )
            expected_source = PostmortemSource.MONTHLY_POSTMORTEM
        else:
            raise ValueError(f"unsupported postmortem kind: {self.kind!r}")

        # tag_summary 일관성
        if self.tag_summary.market != self.market:
            raise ValueError("tag_summary.market must match record market.")

        if self.tag_summary.period != self.period:
            raise ValueError("tag_summary.period must match record period.")

        if self.tag_summary.source != expected_source:
            raise ValueError("tag_summary.source must match record kind.")

        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic replay/저장을 위한 canonical dict 표현을 반환한다."""
        payload: dict[str, Any] = {
            "postmortem_id": self.postmortem_id,
            "market": self.market.value,
            "kind": self.kind.value,
            "period": self.period,
            "created_at": self.created_at.isoformat(),
            "evaluated_start_date": self.evaluated_start_date.isoformat(),
            "evaluated_end_date": self.evaluated_end_date.isoformat(),
            "summary": self.summary,
            "evaluation": {
                "price_result": self.evaluation.price_result,
                "benchmark_relative_result": self.evaluation.benchmark_relative_result,
                "evidence_validity": self.evaluation.evidence_validity,
                "date_id_interpretation_accuracy": self.evaluation.date_id_interpretation_accuracy,
                "reasoning_action_consistency": self.evaluation.reasoning_action_consistency,
                "python_rule_outcome": self.evaluation.python_rule_outcome,
                "thesis_validity": self.evaluation.thesis_validity,
            },
            "findings": list(self.findings),
            "lessons": list(self.lessons),
            "tag_summary": self.tag_summary.to_canonical_dict(),
        }

        if self.daily_summary_ids:
            payload["daily_summary_ids"] = list(self.daily_summary_ids)
        if self.date_ids_used:
            payload["date_ids_used"] = [item.value for item in self.date_ids_used]
        if self.decision_snapshot_ids:
            payload["decision_snapshot_ids"] = [
                item.value for item in self.decision_snapshot_ids
            ]
        if self.fill_ids:
            payload["fill_ids"] = list(self.fill_ids)
        if self.nav_snapshot_ids:
            payload["nav_snapshot_ids"] = list(self.nav_snapshot_ids)
        if self.metadata:
            payload["metadata"] = canonicalize_payload(self.metadata)

        return canonicalize_payload(payload)

    def payload_hash(self) -> str:
        """canonical payload sha256 hex digest를 반환한다."""
        return payload_sha256(self.to_canonical_dict())
