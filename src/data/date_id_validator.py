from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from domain._datetime import require_timezone_aware_datetime
from domain.decision import EvidenceRef
from domain.identifiers import DateId
from domain.staleness import StalenessPolicy
from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity
from data.date_id_store import SQLiteDateIdSourceStore

DATE_ID_VALIDATOR_VERSION = "1.0.0"
DATE_ID_VALIDATION_SCHEMA = "date_id_validation"


class DateIdValidator:
    """Date-ID existence/stale validation을 수행한다."""

    def __init__(
        self,
        store: SQLiteDateIdSourceStore,
        staleness_policy: StalenessPolicy,
    ) -> None:
        self._store = store
        self._staleness_policy = staleness_policy

    def validate_date_ids(
        self,
        date_ids: Iterable[DateId | str],
        *,
        now: datetime,
    ) -> ValidationResult:
        """unique Date-ID 기준으로 existence/stale validation을 수행한다."""
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        issues: list[ValidationIssue] = []
        unique_valid: dict[str, DateId] = {}

        for raw_date_id in date_ids:
            try:
                parsed = _parse_date_id_or_issue(raw_date_id)
            except ValueError as exc:
                raw_text = raw_date_id if isinstance(raw_date_id, str) else raw_date_id.value
                issues.append(
                    ValidationIssue(
                        code="DATE_ID_INVALID",
                        message=f"invalid date_id: {raw_text} ({exc})",
                        severity=ValidationSeverity.ERROR,
                    )
                )
                continue
            unique_valid.setdefault(parsed.value, parsed)

        for date_id in sorted(unique_valid.values(), key=lambda item: item.value):
            issues.extend(self._validate_single_date_id(date_id, now=aware_now))

        return _build_validation_result(issues)

    def validate_evidence_refs(
        self,
        evidence_refs: Iterable[EvidenceRef],
        *,
        now: datetime,
    ) -> ValidationResult:
        """EvidenceRef.date_id 기준으로 existence/stale validation을 수행한다."""
        date_ids = (ref.date_id for ref in evidence_refs)
        return self.validate_date_ids(date_ids, now=now)

    def validate_reason_date_ids(
        self,
        payload: dict[str, Any],
        *,
        now: datetime,
    ) -> ValidationResult:
        """payload 내 reasons[].date_id 구조와 existence/stale validation을 수행한다."""
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        structural_issues, valid_date_ids = _collect_reason_date_id_issues(payload)
        existence_issues: list[ValidationIssue] = []

        for date_id in sorted(valid_date_ids, key=lambda item: item.value):
            existence_issues.extend(self._validate_single_date_id(date_id, now=aware_now))

        issues = _merge_issues(structural_issues, existence_issues)
        return _build_validation_result(issues)

    def _validate_single_date_id(
        self,
        date_id: DateId,
        *,
        now: datetime,
    ) -> list[ValidationIssue]:
        record = self._store.get_record(date_id)
        if record is None:
            return [
                ValidationIssue(
                    code="DATE_ID_MISSING",
                    message=f"date_id not found in store: {date_id.value}",
                    severity=ValidationSeverity.ERROR,
                )
            ]

        try:
            self._staleness_policy.age(record, now)
        except ValueError:
            return [
                ValidationIssue(
                    code="DATE_ID_FUTURE_SOURCE",
                    message=(
                        f"date_id source_timestamp is in the future: {date_id.value} "
                        f"({record.source_timestamp.isoformat()})"
                    ),
                    severity=ValidationSeverity.ERROR,
                )
            ]

        if self._staleness_policy.is_stale(record, now=now):
            return [
                ValidationIssue(
                    code="DATE_ID_STALE",
                    message=(
                        f"date_id is stale: {date_id.value} "
                        f"(fact_type={record.fact_type.value})"
                    ),
                    severity=ValidationSeverity.ERROR,
                )
            ]

        return []


def extract_date_ids_from_reasons(payload: dict[str, Any]) -> tuple[DateId, ...]:
    """payload를 재귀 순회하며 reasons[].date_id를 추출한다."""
    extracted: list[DateId] = []
    _walk_payload_for_reason_date_ids(payload, extracted, path="")
    unique: dict[str, DateId] = {}
    for date_id in extracted:
        unique.setdefault(date_id.value, date_id)
    return tuple(unique[date_id] for date_id in sorted(unique))


def _parse_date_id_or_issue(raw_date_id: DateId | str) -> DateId:
    if isinstance(raw_date_id, DateId):
        return raw_date_id
    return DateId(raw_date_id)


def _parse_date_id_from_reason_value(
    raw_value: Any,
    *,
    path: str,
) -> tuple[DateId | None, ValidationIssue | None]:
    if raw_value is None:
        return None, ValidationIssue(
            code="DATE_ID_MISSING_FIELD",
            message=f"reason item missing date_id field: {path}",
            severity=ValidationSeverity.ERROR,
            path=path,
        )

    try:
        if isinstance(raw_value, DateId):
            return raw_value, None
        if not isinstance(raw_value, str):
            raise ValueError("date_id must be a string.")
        return DateId(raw_value), None
    except ValueError as exc:
        return None, ValidationIssue(
            code="DATE_ID_INVALID",
            message=f"invalid date_id at {path}: {exc}",
            severity=ValidationSeverity.ERROR,
            path=path,
        )


def _collect_reason_date_id_issues(
    payload: dict[str, Any],
) -> tuple[tuple[ValidationIssue, ...], tuple[DateId, ...]]:
    issues: list[ValidationIssue] = []
    valid_date_ids: list[DateId] = []
    _walk_payload_for_reason_validation(payload, issues, valid_date_ids, path="")
    unique_valid: dict[str, DateId] = {}
    for date_id in valid_date_ids:
        unique_valid.setdefault(date_id.value, date_id)
    return tuple(_sort_issues(issues)), tuple(
        unique_valid[date_id] for date_id in sorted(unique_valid)
    )


def _walk_payload_for_reason_date_ids(payload: Any, extracted: list[DateId], *, path: str) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            child_path = f"{path}.{key}" if path else key
            if key == "reasons" and isinstance(value, list):
                for index, item in enumerate(value):
                    item_path = f"{child_path}[{index}]"
                    if isinstance(item, dict) and "date_id" in item:
                        try:
                            parsed = _parse_date_id_or_issue(item["date_id"])
                        except ValueError:
                            continue
                        extracted.append(parsed)
            _walk_payload_for_reason_date_ids(value, extracted, path=child_path)
        return

    if isinstance(payload, list):
        for index, item in enumerate(payload):
            _walk_payload_for_reason_date_ids(item, extracted, path=f"{path}[{index}]")


def _walk_payload_for_reason_validation(
    payload: Any,
    issues: list[ValidationIssue],
    valid_date_ids: list[DateId],
    *,
    path: str,
) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            child_path = f"{path}.{key}" if path else key
            if key == "reasons":
                if not isinstance(value, list):
                    issues.append(
                        ValidationIssue(
                            code="DATE_ID_INVALID",
                            message=f"reasons must be a list: {child_path}",
                            severity=ValidationSeverity.ERROR,
                            path=child_path,
                        )
                    )
                else:
                    for index, item in enumerate(value):
                        item_path = f"{child_path}[{index}]"
                        if not isinstance(item, Mapping):
                            issues.append(
                                ValidationIssue(
                                    code="DATE_ID_INVALID",
                                    message=f"reason item must be a dict: {item_path}",
                                    severity=ValidationSeverity.ERROR,
                                    path=item_path,
                                )
                            )
                            continue

                        if "date_id" not in item:
                            issues.append(
                                ValidationIssue(
                                    code="DATE_ID_MISSING_FIELD",
                                    message=f"reason item missing date_id field: {item_path}",
                                    severity=ValidationSeverity.ERROR,
                                    path=item_path,
                                )
                            )
                            continue

                        parsed, issue = _parse_date_id_from_reason_value(
                            item["date_id"],
                            path=item_path,
                        )
                        if issue is not None:
                            issues.append(issue)
                        elif parsed is not None:
                            valid_date_ids.append(parsed)
            _walk_payload_for_reason_validation(value, issues, valid_date_ids, path=child_path)
        return

    if isinstance(payload, list):
        for index, item in enumerate(payload):
            _walk_payload_for_reason_validation(
                item,
                issues,
                valid_date_ids,
                path=f"{path}[{index}]",
            )


def _sort_issues(issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    return sorted(
        issues,
        key=lambda issue: (issue.code, issue.path or "", issue.message),
    )


def _merge_issues(
    structural_issues: Iterable[ValidationIssue],
    existence_issues: Iterable[ValidationIssue],
) -> tuple[ValidationIssue, ...]:
    merged = list(structural_issues) + list(existence_issues)
    return tuple(_sort_issues(merged))


def _build_validation_result(issues: list[ValidationIssue]) -> ValidationResult:
    if not issues:
        return ValidationResult(
            passed=True,
            issues=(),
            schema_name=DATE_ID_VALIDATION_SCHEMA,
            validator_version=DATE_ID_VALIDATOR_VERSION,
        )
    return ValidationResult(
        passed=False,
        issues=tuple(_sort_issues(issues)),
        schema_name=DATE_ID_VALIDATION_SCHEMA,
        validator_version=DATE_ID_VALIDATOR_VERSION,
    )

