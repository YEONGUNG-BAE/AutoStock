from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decision.canonical_json import canonical_json_dumps, canonicalize_payload, payload_sha256
from domain import (
    DateId,
    DecisionId,
    DecisionSnapshot,
    EvidenceRef,
    Percent,
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
)


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def test_percent_accepts_valid_values() -> None:
    assert Percent("0").value == Decimal("0")
    assert Percent("100").value == Decimal("100")
    assert Percent("12.5").value == Decimal("12.5")
    assert Percent(Decimal("12.5")).value == Decimal("12.5")


@pytest.mark.parametrize(
    "value, match",
    [
        ("-1", "between 0 and 100"),
        ("100.0001", "between 0 and 100"),
        ("NaN", "finite decimal"),
        ("Infinity", "finite decimal"),
        ("", "valid decimal"),
        (True, "not bool"),
    ],
)
def test_percent_rejects_invalid_values(value: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Percent(value)


def test_date_id_accepts_valid_values() -> None:
    date_id = DateId("260522-1")
    assert date_id.value == "260522-1"
    assert DateId("260522-01").value == "260522-1"
    assert DateId.from_token("[260522-1]").value == "260522-1"
    assert DateId.from_token("260522-1").value == "260522-1"


@pytest.mark.parametrize(
    "value, match",
    [
        ("260230-1", "valid calendar date"),
        ("260522-0", "at least 1"),
        ("260522", "canonical format"),
        ("", "must not be blank"),
        ("[260522-1]", "without brackets"),
    ],
)
def test_date_id_rejects_invalid_values(value: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        DateId(value)


def test_decision_id_accepts_valid_values() -> None:
    assert DecisionId("decision-260522-001").value == "decision-260522-001"
    assert DecisionId("allocator.v1-abc123").value == "allocator.v1-abc123"


@pytest.mark.parametrize(
    "value, match",
    [
        ("", "must not be blank"),
        ("decision 001", "whitespace"),
        ("decision/001", "letters, digits"),
    ],
)
def test_decision_id_rejects_invalid_values(value: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        DecisionId(value)


def test_decision_id_from_hash_is_deterministic() -> None:
    first = DecisionId.from_hash("allocator.v1", "abc123")
    second = DecisionId.from_hash("allocator.v1", "abc123")
    assert first == second
    assert first.value == "allocator.v1-abc123"


def test_evidence_ref_roundtrip() -> None:
    evidence = EvidenceRef(
        reason="금 ETF 비중 유지",
        date_id=DateId("260522-1"),
        source_name="DailySummary",
        source_url="https://example.com/news/1",
        quote="금 가격 안정",
    )
    restored = EvidenceRef.model_validate(evidence.model_dump())
    assert restored == evidence


def test_evidence_ref_rejects_blank_reason() -> None:
    with pytest.raises(ValidationError, match="reason must not be blank"):
        EvidenceRef(reason=" ", date_id=DateId("260522-1"))


def test_evidence_ref_rejects_invalid_date_id() -> None:
    with pytest.raises(ValidationError):
        EvidenceRef(reason="ok", date_id="260230-1")


def test_evidence_ref_rejects_blank_optional_fields() -> None:
    with pytest.raises(ValidationError, match="source_name must not be blank"):
        EvidenceRef(reason="ok", date_id=DateId("260522-1"), source_name=" ")


def test_validation_result_passed_true_without_issues() -> None:
    result = ValidationResult(passed=True, issues=())
    assert result.passed is True
    assert result.issues == ()


def test_validation_result_passed_true_with_info_warning() -> None:
    result = ValidationResult(
        passed=True,
        issues=(
            ValidationIssue(code="INFO_1", message="info", severity=ValidationSeverity.INFO),
            ValidationIssue(code="WARN_1", message="warn", severity=ValidationSeverity.WARNING),
        ),
    )
    assert result.passed is True


def test_validation_result_passed_true_with_error_rejects() -> None:
    with pytest.raises(ValidationError, match="must not contain ERROR issues"):
        ValidationResult(
            passed=True,
            issues=(
                ValidationIssue(code="ERR_1", message="error", severity=ValidationSeverity.ERROR),
            ),
        )


def test_validation_result_passed_false_with_error() -> None:
    result = ValidationResult(
        passed=False,
        issues=(
            ValidationIssue(code="ERR_1", message="error", severity=ValidationSeverity.ERROR),
        ),
    )
    assert result.passed is False


def test_validation_result_passed_false_without_issues_rejects() -> None:
    with pytest.raises(ValidationError, match="must contain at least one issue"):
        ValidationResult(passed=False, issues=())


def test_validation_result_rejects_blank_fields() -> None:
    with pytest.raises(ValidationError, match="code must not be blank"):
        ValidationIssue(code=" ", message="msg", severity=ValidationSeverity.INFO)

    with pytest.raises(ValidationError, match="path must not be blank"):
        ValidationIssue(
            code="CODE",
            message="msg",
            severity=ValidationSeverity.INFO,
            path=" ",
        )


def test_validation_result_preserves_issue_order_in_canonical_dict() -> None:
    result = ValidationResult(
        passed=False,
        issues=(
            ValidationIssue(code="A", message="first", severity=ValidationSeverity.ERROR),
            ValidationIssue(code="B", message="second", severity=ValidationSeverity.WARNING),
        ),
    )
    canonical = result.to_canonical_dict()
    assert [issue["code"] for issue in canonical["issues"]] == ["A", "B"]


def test_canonical_json_sorts_dict_keys() -> None:
    first = {"b": 1, "a": 2}
    second = {"a": 2, "b": 1}
    assert canonical_json_dumps(first) == canonical_json_dumps(second)
    assert payload_sha256(first) == payload_sha256(second)


def test_canonical_json_nested_dict_key_order() -> None:
    first = {"b": 1, "a": {"y": "2", "x": "1"}}
    second = {"a": {"x": "1", "y": "2"}, "b": 1}
    assert payload_sha256(first) == payload_sha256(second)


def test_canonical_json_list_order_matters() -> None:
    assert payload_sha256([1, 2]) != payload_sha256([2, 1])


def test_canonical_json_decimal_serialization() -> None:
    assert canonicalize_payload(Decimal("1.20")) == "1.20"


def test_canonical_json_datetime_requires_timezone() -> None:
    aware = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    assert canonicalize_payload(aware) == aware.isoformat()

    with pytest.raises(ValueError, match="timezone-aware datetime"):
        canonicalize_payload(NAIVE_NOW)


def test_canonical_json_rejects_float_set_and_non_string_keys() -> None:
    with pytest.raises(ValueError, match="float values are not allowed"):
        canonicalize_payload(1.5)

    with pytest.raises(ValueError, match="set values are not allowed"):
        canonicalize_payload({1})

    with pytest.raises(ValueError, match="dict keys must be strings"):
        canonicalize_payload({1: "a"})


def test_decision_snapshot_create_normalizes_payload_and_hash() -> None:
    raw_payload_a = {"b": 1, "a": {"y": "2", "x": "1"}}
    raw_payload_b = {"a": {"x": "1", "y": "2"}, "b": 1}
    validation_result = ValidationResult(passed=True, issues=())

    snapshot_a = DecisionSnapshot.create(
        decision_id=DecisionId("decision-001"),
        created_at=NOW,
        schema_name="allocator.v1",
        raw_payload=raw_payload_a,
        validation_result=validation_result,
    )
    snapshot_b = DecisionSnapshot.create(
        decision_id=DecisionId("decision-002"),
        created_at=NOW,
        schema_name="allocator.v1",
        raw_payload=raw_payload_b,
        validation_result=validation_result,
    )

    assert snapshot_a.normalized_payload == snapshot_b.normalized_payload
    assert snapshot_a.payload_hash == snapshot_b.payload_hash


def test_decision_snapshot_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="created_at must be a timezone-aware datetime"):
        DecisionSnapshot.create(
            decision_id=DecisionId("decision-001"),
            created_at=NAIVE_NOW,
            schema_name="allocator.v1",
            raw_payload={"a": 1},
            validation_result=ValidationResult(passed=True, issues=()),
        )


def test_decision_snapshot_rejects_blank_schema_name() -> None:
    with pytest.raises(ValidationError, match="schema_name must not be blank"):
        DecisionSnapshot.create(
            decision_id=DecisionId("decision-001"),
            created_at=NOW,
            schema_name=" ",
            raw_payload={"a": 1},
            validation_result=ValidationResult(passed=True, issues=()),
        )


def test_decision_snapshot_rejects_blank_order_intent_id() -> None:
    with pytest.raises(ValidationError, match="order_intent_ids\\[0\\] must not be blank"):
        DecisionSnapshot.create(
            decision_id=DecisionId("decision-001"),
            created_at=NOW,
            schema_name="allocator.v1",
            raw_payload={"a": 1},
            validation_result=ValidationResult(passed=True, issues=()),
            order_intent_ids=(" ",),
        )


def test_decision_snapshot_rejects_non_json_replay_metadata() -> None:
    with pytest.raises(ValueError, match="set values are not allowed"):
        DecisionSnapshot.create(
            decision_id=DecisionId("decision-001"),
            created_at=NOW,
            schema_name="allocator.v1",
            raw_payload={"a": 1},
            validation_result=ValidationResult(passed=True, issues=()),
            replay_metadata={"bad": {1, 2}},  # type: ignore[arg-type]
        )


def test_decision_snapshot_payload_hash_is_deterministic() -> None:
    snapshot = DecisionSnapshot.create(
        decision_id=DecisionId("decision-001"),
        created_at=NOW,
        schema_name="allocator.v1",
        raw_payload={"z": Decimal("1.20"), "a": 1},
        validation_result=ValidationResult(passed=True, issues=()),
    )
    assert snapshot.payload_hash == payload_sha256(snapshot.normalized_payload)
