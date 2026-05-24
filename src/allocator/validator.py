from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from allocator.models import ALLOCATOR_DECISION_SCHEMA, AllocatorDecision
from allocator.rules import (
    CASH_TARGET_MAX,
    CASH_TARGET_MIN,
    GOLD_EXCEPTION_MAX,
    GOLD_EXCEPTION_MIN,
    GOLD_NORMAL_MAX,
    GOLD_NORMAL_MIN,
    is_cash_target_in_band,
    is_gold_in_band,
    is_target_weights_sum_valid,
    percent_equal,
    target_weights_equal,
    target_weights_sum,
)
from data.date_id_validator import DateIdValidator
from domain._datetime import require_timezone_aware_datetime
from domain.identifiers import DateId
from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity

ALLOCATOR_VALIDATOR_VERSION = "phase8"

ALLOCATOR_SCHEMA_INVALID = "ALLOCATOR_SCHEMA_INVALID"
ALLOCATOR_DATE_ID_MISSING = "ALLOCATOR_DATE_ID_MISSING"
ALLOCATOR_DATE_ID_STALE = "ALLOCATOR_DATE_ID_STALE"
ALLOCATOR_DATE_ID_FUTURE_SOURCE = "ALLOCATOR_DATE_ID_FUTURE_SOURCE"
ALLOCATOR_TARGET_WEIGHTS_SUM_INVALID = "ALLOCATOR_TARGET_WEIGHTS_SUM_INVALID"
ALLOCATOR_GOLD_BAND_VIOLATION = "ALLOCATOR_GOLD_BAND_VIOLATION"
ALLOCATOR_CASH_TARGET_BAND_VIOLATION = "ALLOCATOR_CASH_TARGET_BAND_VIOLATION"
ALLOCATOR_TARGET_WEIGHTS_MISMATCH = "ALLOCATOR_TARGET_WEIGHTS_MISMATCH"
ALLOCATOR_CASH_TARGET_MISMATCH = "ALLOCATOR_CASH_TARGET_MISMATCH"
ALLOCATOR_CONSISTENCY_CHECK_FAILED = "ALLOCATOR_CONSISTENCY_CHECK_FAILED"

_DATE_ID_CODE_MAP = {
    "DATE_ID_MISSING": ALLOCATOR_DATE_ID_MISSING,
    "DATE_ID_STALE": ALLOCATOR_DATE_ID_STALE,
    "DATE_ID_FUTURE_SOURCE": ALLOCATOR_DATE_ID_FUTURE_SOURCE,
}

_BUSINESS_ISSUE_ORDER = (
    ALLOCATOR_TARGET_WEIGHTS_SUM_INVALID,
    ALLOCATOR_GOLD_BAND_VIOLATION,
    ALLOCATOR_CASH_TARGET_BAND_VIOLATION,
    ALLOCATOR_TARGET_WEIGHTS_MISMATCH,
    ALLOCATOR_CASH_TARGET_MISMATCH,
    ALLOCATOR_CONSISTENCY_CHECK_FAILED,
)


def extract_date_ids_from_allocator_decision(decision: AllocatorDecision) -> tuple[DateId, ...]:
    """AllocatorDecision 내 모든 reason Date-ID를 occurrence-preserving tuple로 추출한다."""
    extracted: list[DateId] = []
    for reasons in (
        decision.reasons,
        decision.signal_summary.reasons,
        decision.cash_manager.reasons,
        decision.asset_allocator.reasons,
        decision.consistency_checker.reasons,
        decision.cash_policy.reasons,
    ):
        for reason in reasons:
            extracted.append(reason.date_id)
    return tuple(extracted)


class AllocatorDecisionValidator:
    """AllocatorDecision schema/business rule/Date-ID validation을 수행한다."""

    def __init__(self, date_id_validator: DateIdValidator) -> None:
        self._date_id_validator = date_id_validator

    def validate(self, decision: AllocatorDecision, *, now: datetime) -> ValidationResult:
        """typed AllocatorDecision에 대해 business rule + Date-ID validation을 수행한다."""
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        business_issues = _collect_business_rule_issues(decision)
        date_id_result = self._date_id_validator.validate_date_ids(
            extract_date_ids_from_allocator_decision(decision),
            now=aware_now,
        )
        date_id_issues = _map_date_id_issues(date_id_result.issues)
        issues = _merge_issues(business_issues, date_id_issues)
        return _build_allocator_validation_result(issues)

    def validate_payload(
        self,
        payload: Mapping[str, Any],
        *,
        now: datetime,
    ) -> tuple[AllocatorDecision | None, ValidationResult]:
        """raw payload를 AllocatorDecision으로 파싱한 뒤 validation을 수행한다."""
        require_timezone_aware_datetime(now, field_name="now")
        try:
            decision = AllocatorDecision.model_validate(dict(payload))
        except ValidationError as exc:
            return None, _schema_invalid_result(str(exc))

        result = self.validate(decision, now=now)
        return decision, result


def _collect_business_rule_issues(decision: AllocatorDecision) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if not is_target_weights_sum_valid(decision.target_weights):
        total = target_weights_sum(decision.target_weights)
        issues.append(
            ValidationIssue(
                code=ALLOCATOR_TARGET_WEIGHTS_SUM_INVALID,
                message=(
                    f"target_weights sum must equal 100: "
                    f"kr={decision.target_weights.kr.value}, "
                    f"us={decision.target_weights.us.value}, "
                    f"gold={decision.target_weights.gold.value}, "
                    f"sum={total}"
                ),
                severity=ValidationSeverity.ERROR,
            )
        )

    if not is_gold_in_band(decision.target_weights.gold, decision.gold_policy_mode):
        if decision.gold_policy_mode.value == "normal":
            band_text = f"{GOLD_NORMAL_MIN}~{GOLD_NORMAL_MAX}"
        else:
            band_text = f"{GOLD_EXCEPTION_MIN}~{GOLD_EXCEPTION_MAX}"
        issues.append(
            ValidationIssue(
                code=ALLOCATOR_GOLD_BAND_VIOLATION,
                message=(
                    f"gold target violates {decision.gold_policy_mode.value} band "
                    f"({band_text}): gold={decision.target_weights.gold.value}"
                ),
                severity=ValidationSeverity.ERROR,
            )
        )

    if not is_cash_target_in_band(decision.cash_policy.cash_target_percent):
        issues.append(
            ValidationIssue(
                code=ALLOCATOR_CASH_TARGET_BAND_VIOLATION,
                message=(
                    f"cash_target_percent must be between {CASH_TARGET_MIN} and {CASH_TARGET_MAX}: "
                    f"cash_target_percent={decision.cash_policy.cash_target_percent.value}"
                ),
                severity=ValidationSeverity.ERROR,
            )
        )

    if not target_weights_equal(decision.target_weights, decision.asset_allocator.target_weights):
        issues.append(
            ValidationIssue(
                code=ALLOCATOR_TARGET_WEIGHTS_MISMATCH,
                message=(
                    "target_weights must match asset_allocator.target_weights: "
                    f"top_level={decision.target_weights.kr.value}/"
                    f"{decision.target_weights.us.value}/"
                    f"{decision.target_weights.gold.value}, "
                    f"asset_allocator={decision.asset_allocator.target_weights.kr.value}/"
                    f"{decision.asset_allocator.target_weights.us.value}/"
                    f"{decision.asset_allocator.target_weights.gold.value}"
                ),
                severity=ValidationSeverity.ERROR,
            )
        )

    if not percent_equal(
        decision.cash_policy.cash_target_percent,
        decision.cash_manager.recommended_cash_percent,
    ):
        issues.append(
            ValidationIssue(
                code=ALLOCATOR_CASH_TARGET_MISMATCH,
                message=(
                    "cash_policy.cash_target_percent must match "
                    "cash_manager.recommended_cash_percent: "
                    f"cash_policy={decision.cash_policy.cash_target_percent.value}, "
                    f"cash_manager={decision.cash_manager.recommended_cash_percent.value}"
                ),
                severity=ValidationSeverity.ERROR,
            )
        )

    if not decision.consistency_checker.passed:
        issues.append(
            ValidationIssue(
                code=ALLOCATOR_CONSISTENCY_CHECK_FAILED,
                message="consistency_checker.passed must be true.",
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


def _build_allocator_validation_result(issues: tuple[ValidationIssue, ...]) -> ValidationResult:
    if not issues:
        return ValidationResult(
            passed=True,
            issues=(),
            schema_name=ALLOCATOR_DECISION_SCHEMA,
            validator_version=ALLOCATOR_VALIDATOR_VERSION,
        )
    return ValidationResult(
        passed=False,
        issues=issues,
        schema_name=ALLOCATOR_DECISION_SCHEMA,
        validator_version=ALLOCATOR_VALIDATOR_VERSION,
    )


def _schema_invalid_result(message: str) -> ValidationResult:
    return ValidationResult(
        passed=False,
        issues=(
            ValidationIssue(
                code=ALLOCATOR_SCHEMA_INVALID,
                message=message,
                severity=ValidationSeverity.ERROR,
            ),
        ),
        schema_name=ALLOCATOR_DECISION_SCHEMA,
        validator_version=ALLOCATOR_VALIDATOR_VERSION,
    )
