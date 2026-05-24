from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from data.date_id_validator import DateIdValidator
from domain._datetime import require_timezone_aware_datetime
from domain.identifiers import DateId
from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity
from scout.models import ScoutSummary

SCOUT_SUMMARY_SCHEMA = "scout_summary.v1"
SCOUT_SUMMARY_VALIDATOR_VERSION = "phase7"
SCOUT_SCHEMA_INVALID = "SCOUT_SCHEMA_INVALID"


def extract_date_ids_from_scout_summary(summary: ScoutSummary) -> tuple[DateId, ...]:
    """ScoutSummary 내 모든 reason Date-ID를 occurrence-preserving tuple로 추출한다.

    group 순서: positive → negative → neutral.
    factor/reason 순서는 입력 그대로 보존한다.
    """
    extracted: list[DateId] = []
    for factor_group in (
        summary.positive_factors,
        summary.negative_factors,
        summary.neutral_factors,
    ):
        for factor in factor_group:
            for reason in factor.reasons:
                extracted.append(reason.date_id)
    return tuple(extracted)


class ScoutSummaryValidator:
    """ScoutSummary schema validation과 Date-ID existence/stale validation을 수행한다."""

    def __init__(self, date_id_validator: DateIdValidator) -> None:
        self._date_id_validator = date_id_validator

    def validate(self, summary: ScoutSummary, *, now: datetime) -> ValidationResult:
        """typed ScoutSummary에 대해 Date-ID validation을 수행한다."""
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        date_ids = extract_date_ids_from_scout_summary(summary)
        date_id_result = self._date_id_validator.validate_date_ids(date_ids, now=aware_now)
        return _build_scout_validation_result(date_id_result.issues)

    def validate_payload(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime,
    ) -> tuple[ScoutSummary | None, ValidationResult]:
        """raw payload를 ScoutSummary로 파싱한 뒤 schema/Date-ID validation을 수행한다."""
        require_timezone_aware_datetime(now, field_name="now")
        try:
            summary = ScoutSummary.model_validate(dict(payload))
        except ValidationError as exc:
            return None, _schema_invalid_result(str(exc))

        result = self.validate(summary, now=now)
        return summary, result


def _build_scout_validation_result(issues: tuple[ValidationIssue, ...]) -> ValidationResult:
    if not issues:
        return ValidationResult(
            passed=True,
            issues=(),
            schema_name=SCOUT_SUMMARY_SCHEMA,
            validator_version=SCOUT_SUMMARY_VALIDATOR_VERSION,
        )
    return ValidationResult(
        passed=False,
        issues=issues,
        schema_name=SCOUT_SUMMARY_SCHEMA,
        validator_version=SCOUT_SUMMARY_VALIDATOR_VERSION,
    )


def _schema_invalid_result(message: str) -> ValidationResult:
    return ValidationResult(
        passed=False,
        issues=(
            ValidationIssue(
                code=SCOUT_SCHEMA_INVALID,
                message=message,
                severity=ValidationSeverity.ERROR,
            ),
        ),
        schema_name=SCOUT_SUMMARY_SCHEMA,
        validator_version=SCOUT_SUMMARY_VALIDATOR_VERSION,
    )
