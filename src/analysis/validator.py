from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from analysis.models import ANALYSIS_DECISION_SCHEMA, AnalysisDecision
from analysis.rules import is_within_allocator_tolerance, tolerance_band
from data.date_id_validator import DateIdValidator
from domain._datetime import require_timezone_aware_datetime
from domain.identifiers import DateId, Percent
from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity

ANALYSIS_VALIDATOR_VERSION = "phase9"

ANALYSIS_SCHEMA_INVALID = "ANALYSIS_SCHEMA_INVALID"
ANALYSIS_DATE_ID_MISSING = "ANALYSIS_DATE_ID_MISSING"
ANALYSIS_DATE_ID_STALE = "ANALYSIS_DATE_ID_STALE"
ANALYSIS_DATE_ID_FUTURE_SOURCE = "ANALYSIS_DATE_ID_FUTURE_SOURCE"
ANALYSIS_ALLOCATOR_TOLERANCE_VIOLATION = "ANALYSIS_ALLOCATOR_TOLERANCE_VIOLATION"
ANALYSIS_CONFLICTING_PERSPECTIVES_UNSUPPORTED = "ANALYSIS_CONFLICTING_PERSPECTIVES_UNSUPPORTED"

_DATE_ID_CODE_MAP = {
    "DATE_ID_MISSING": ANALYSIS_DATE_ID_MISSING,
    "DATE_ID_STALE": ANALYSIS_DATE_ID_STALE,
    "DATE_ID_FUTURE_SOURCE": ANALYSIS_DATE_ID_FUTURE_SOURCE,
}

_BUSINESS_ISSUE_ORDER = (
    ANALYSIS_ALLOCATOR_TOLERANCE_VIOLATION,
    ANALYSIS_CONFLICTING_PERSPECTIVES_UNSUPPORTED,
)


def extract_date_ids_from_analysis_decision(decision: AnalysisDecision) -> tuple[DateId, ...]:
    """AnalysisDecision 내 모든 reason Date-ID를 occurrence-preserving tuple로 추출한다."""
    extracted: list[DateId] = []
    for reasons in (
        decision.reasons,
        decision.bear.reasons,
        decision.bull.reasons,
        decision.risk_manager.reasons,
        decision.fund_manager.reasons,
    ):
        for reason in reasons:
            extracted.append(reason.date_id)
    return tuple(extracted)


class AnalysisDecisionValidator:
    """AnalysisDecision schema/business rule/Date-ID validation을 수행한다."""

    def __init__(self, date_id_validator: DateIdValidator) -> None:
        self._date_id_validator = date_id_validator

    def validate(
        self,
        decision: AnalysisDecision,
        *,
        now: datetime,
        allocator_target_weight: Percent | None = None,
        tolerance_percent: Percent | None = None,
    ) -> ValidationResult:
        """typed AnalysisDecision에 대해 business rule + Date-ID validation을 수행한다."""
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        _require_complete_tolerance_context(allocator_target_weight, tolerance_percent)
        business_issues = _collect_business_rule_issues(
            decision,
            allocator_target_weight=allocator_target_weight,
            tolerance_percent=tolerance_percent,
        )
        date_id_result = self._date_id_validator.validate_date_ids(
            extract_date_ids_from_analysis_decision(decision),
            now=aware_now,
        )
        date_id_issues = _map_date_id_issues(date_id_result.issues)
        issues = _merge_issues(business_issues, date_id_issues)
        return _build_analysis_validation_result(issues)

    def validate_payload(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime,
        allocator_target_weight: Percent | None = None,
        tolerance_percent: Percent | None = None,
    ) -> tuple[AnalysisDecision | None, ValidationResult]:
        """raw payload를 AnalysisDecision으로 파싱한 뒤 validation을 수행한다."""
        require_timezone_aware_datetime(now, field_name="now")
        _require_complete_tolerance_context(allocator_target_weight, tolerance_percent)
        try:
            decision = AnalysisDecision.model_validate(dict(payload))
        except ValidationError as exc:
            return None, _schema_invalid_result(str(exc))

        result = self.validate(
            decision,
            now=now,
            allocator_target_weight=allocator_target_weight,
            tolerance_percent=tolerance_percent,
        )
        return decision, result


def _require_complete_tolerance_context(
    allocator_target_weight: Percent | None,
    tolerance_percent: Percent | None,
) -> None:
    if (allocator_target_weight is None) != (tolerance_percent is None):
        raise ValueError(
            "allocator_target_weight and tolerance_percent must both be provided or both omitted."
        )


def _collect_business_rule_issues(
    decision: AnalysisDecision,
    *,
    allocator_target_weight: Percent | None,
    tolerance_percent: Percent | None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if allocator_target_weight is not None and tolerance_percent is not None:
        target = decision.fund_manager.target_weight_percent
        if not is_within_allocator_tolerance(
            target,
            allocator_target_weight,
            tolerance_percent,
        ):
            lower, upper = tolerance_band(allocator_target_weight, tolerance_percent)
            issues.append(
                ValidationIssue(
                    code=ANALYSIS_ALLOCATOR_TOLERANCE_VIOLATION,
                    message=(
                        "fund_manager.target_weight_percent must be within "
                        f"allocator_target_weight ± tolerance_percent: "
                        f"target={target.value}, "
                        f"allocator_target={allocator_target_weight.value}, "
                        f"tolerance={tolerance_percent.value}, "
                        f"allowed_band={lower}~{upper}"
                    ),
                    severity=ValidationSeverity.ERROR,
                )
            )

    return issues


def _map_date_id_issues(issues: tuple[ValidationIssue, ...]) -> tuple[ValidationIssue, ...]:
    mapped: list[ValidationIssue] = []
    for issue in issues:
        mapped_code = _DATE_ID_CODE_MAP.get(issue.code)
        if mapped_code is None:
            continue
        mapped.append(
            ValidationIssue(
                code=mapped_code,
                message=issue.message,
                severity=issue.severity,
                path=issue.path,
            )
        )
    return tuple(mapped)


def _sort_business_issues(issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    order_index = {code: index for index, code in enumerate(_BUSINESS_ISSUE_ORDER)}
    return sorted(
        issues,
        key=lambda issue: (order_index.get(issue.code, len(_BUSINESS_ISSUE_ORDER)), issue.message),
    )


def _sort_date_id_issues(issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    return sorted(
        issues,
        key=lambda issue: (issue.code, issue.path or "", issue.message),
    )


def _merge_issues(
    business_issues: list[ValidationIssue],
    date_id_issues: tuple[ValidationIssue, ...],
) -> tuple[ValidationIssue, ...]:
    ordered = _sort_business_issues(business_issues) + _sort_date_id_issues(date_id_issues)
    return tuple(ordered)


def _build_analysis_validation_result(issues: tuple[ValidationIssue, ...]) -> ValidationResult:
    if not issues:
        return ValidationResult(
            passed=True,
            issues=(),
            schema_name=ANALYSIS_DECISION_SCHEMA,
            validator_version=ANALYSIS_VALIDATOR_VERSION,
        )
    return ValidationResult(
        passed=False,
        issues=issues,
        schema_name=ANALYSIS_DECISION_SCHEMA,
        validator_version=ANALYSIS_VALIDATOR_VERSION,
    )


def _schema_invalid_result(message: str) -> ValidationResult:
    return ValidationResult(
        passed=False,
        issues=(
            ValidationIssue(
                code=ANALYSIS_SCHEMA_INVALID,
                message=message,
                severity=ValidationSeverity.ERROR,
            ),
        ),
        schema_name=ANALYSIS_DECISION_SCHEMA,
        validator_version=ANALYSIS_VALIDATOR_VERSION,
    )
