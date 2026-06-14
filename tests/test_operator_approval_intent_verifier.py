"""RTM-7c.4q — standalone Operator approval-intent verifier tests."""

from __future__ import annotations

import copy
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.operator_approval_intent import (
    APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE,
    OPERATOR_APPROVAL_INTENT_FIELD_NAMES,
    OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
    OperatorApprovalIntentOutcome,
    build_operator_approval_intent,
    operator_approval_intent_hash_payload,
    validate_operator_approval_intent_object,
    validate_operator_approval_intent_scalars,
)
from composition.operator_approval_intent_verifier import (
    OperatorApprovalIntentVerificationOutcome,
    verify_operator_approval_intent_payload,
)
from decision.canonical_json import payload_sha256

import test_operator_approval_intent as intent_helper

_KST = timezone(timedelta(hours=9))
_DECL_AT = intent_helper._DECL_AT


def _valid_intent_payload() -> dict[str, Any]:
    result = intent_helper._build_intent()
    assert result.outcome is OperatorApprovalIntentOutcome.CREATED
    assert result.intent is not None
    return asdict(result.intent)


def _verify(payload: object) -> Any:
    return verify_operator_approval_intent_payload(payload)


def _recompute_hash(payload: dict[str, Any]) -> str:
    body = operator_approval_intent_hash_payload(
        declared_at=payload["declared_at"],
        evidence_schema_version=payload["evidence_schema_version"],
        evidence_sha256=payload["evidence_sha256"],
        market=payload["market"],
        symbol=payload["symbol"],
    )
    return payload_sha256(body)


# --- builder → verifier invariant ---


def test_builder_output_verifies_valid() -> None:
    payload = _valid_intent_payload()
    result = _verify(payload)
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.VALID
    assert result.reason_codes == ()
    assert result.schema_version == OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION
    assert result.approval_intent_sha256 == payload["approval_intent_sha256"]


def test_real_seeded_builder_output_verifies_valid(tmp_path: Path) -> None:
    import test_activation_candidate_freshness_preflight as fr_helper
    from composition.activation_candidate_evidence import (
        FreshnessQualifiedEvidenceOutcome,
        freshness_qualify_and_build_candidate_evidence,
    )
    from composition.receipt_freshness_policy import ReceiptFreshnessPolicy

    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)
    combined = freshness_qualify_and_build_candidate_evidence(
        settings=settings,
        receipt_payload=receipt,
        now=fr_helper._NOW,
        policy=ReceiptFreshnessPolicy(max_age_microseconds=1_000_000_000),
        base_dir=tmp_path,
    )
    assert combined.outcome is FreshnessQualifiedEvidenceOutcome.PASS
    built = build_operator_approval_intent(
        combined_result=combined,
        declared_at=fr_helper._NOW,
        operator_approval_declared=True,
        writers_stopped_manually_confirmed=True,
        live_orders_forbidden_confirmed=True,
    )
    assert built.intent is not None
    result = _verify(asdict(built.intent))
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.VALID


def test_validate_object_accepts_builder_intent() -> None:
    result = intent_helper._build_intent()
    assert result.intent is not None
    assert validate_operator_approval_intent_object(result.intent) is not None


# --- schema / field set ---


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="root_list"),
        pytest.param("intent", id="root_string"),
        pytest.param(None, id="root_null"),
    ],
)
def test_non_object_root_invalid(payload: object) -> None:
    result = _verify(payload)
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.INVALID
    assert result.reason_codes == ("approval_intent_not_object",)


def test_unknown_field_invalid() -> None:
    payload = _valid_intent_payload()
    payload["extra"] = True
    result = _verify(payload)
    assert result.reason_codes == ("approval_intent_unknown_field",)


@pytest.mark.parametrize("field", sorted(OPERATOR_APPROVAL_INTENT_FIELD_NAMES))
def test_missing_field_invalid(field: str) -> None:
    payload = _valid_intent_payload()
    del payload[field]
    result = _verify(payload)
    assert result.reason_codes == ("approval_intent_missing_field",)


@pytest.mark.parametrize(
    "schema_value,expected",
    [
        pytest.param(2, "approval_intent_unsupported_schema", id="schema_2"),
        pytest.param(True, "approval_intent_invalid_field", id="schema_bool"),
        pytest.param(1.0, "approval_intent_invalid_field", id="schema_float"),
        pytest.param("1", "approval_intent_invalid_field", id="schema_string"),
    ],
)
def test_schema_version_tamper(schema_value: object, expected: str) -> None:
    payload = _valid_intent_payload()
    payload["schema_version"] = schema_value
    result = _verify(payload)
    assert result.reason_codes == (expected,)


# --- declared_at ---


@pytest.mark.parametrize(
    "bad,expected",
    [
        pytest.param("not-iso", "approval_intent_invalid_declared_at", id="malformed"),
        pytest.param("2026-06-14T12:00:00", "approval_intent_invalid_declared_at", id="naive"),
        pytest.param(123, "approval_intent_invalid_declared_at", id="non_string"),
        pytest.param(
            datetime(2026, 6, 14, 12, 0, 0, tzinfo=_KST),
            "approval_intent_not_object",
            id="datetime_object",
        ),
    ],
)
def test_declared_at_tamper(bad: object, expected: str) -> None:
    if expected == "approval_intent_not_object":
        result = _verify(bad)
    else:
        payload = _valid_intent_payload()
        payload["declared_at"] = bad
        result = _verify(payload)
    assert result.reason_codes == (expected,)


# --- evidence binding ---


@pytest.mark.parametrize(
    "field,value,expected",
    [
        pytest.param("evidence_schema_version", 1, "approval_intent_invalid_evidence_binding", id="schema_mismatch"),
        pytest.param("evidence_schema_version", True, "approval_intent_invalid_evidence_binding", id="schema_bool"),
        pytest.param("evidence_sha256", "NOT_HEX", "approval_intent_invalid_evidence_binding", id="hash_malformed"),
        pytest.param("evidence_sha256", "A" * 64, "approval_intent_invalid_evidence_binding", id="hash_uppercase"),
        pytest.param("evidence_sha256", 0, "approval_intent_invalid_evidence_binding", id="hash_non_string"),
    ],
)
def test_evidence_binding_tamper(field: str, value: object, expected: str) -> None:
    payload = _valid_intent_payload()
    payload[field] = value
    result = _verify(payload)
    assert result.reason_codes == (expected,)


# --- identity / scope ---


@pytest.mark.parametrize(
    "field,value,expected",
    [
        pytest.param("market", "US", "approval_intent_invalid_field", id="market_us"),
        pytest.param("market", "", "approval_intent_invalid_field", id="market_empty"),
        pytest.param("symbol", "12345", "approval_intent_invalid_field", id="symbol_short"),
        pytest.param("symbol", "1234567", "approval_intent_invalid_field", id="symbol_long"),
        pytest.param("symbol", "ABC123", "approval_intent_invalid_field", id="symbol_alpha"),
        pytest.param("symbol", "００５９３０", "approval_intent_invalid_field", id="symbol_fullwidth"),
        pytest.param("symbol", "٠٠٥٩٣٠", "approval_intent_invalid_field", id="symbol_arabic_indic"),
        pytest.param("symbol", "00593O", "approval_intent_invalid_field", id="symbol_mixed"),
        pytest.param("approval_scope", "other_scope", "approval_intent_invalid_scope", id="scope_mismatch"),
    ],
)
def test_identity_scope_tamper(field: str, value: object, expected: str) -> None:
    payload = _valid_intent_payload()
    payload[field] = value
    result = _verify(payload)
    assert result.reason_codes == (expected,)


class _StrSub(str):
    pass


def test_market_str_subclass_invalid() -> None:
    payload = _valid_intent_payload()
    payload["market"] = _StrSub("KR")
    result = _verify(payload)
    assert result.reason_codes == ("approval_intent_invalid_field",)


# --- declarations / posture ---


@pytest.mark.parametrize(
    "field,bad",
    [
        pytest.param("operator_approval_declared", False, id="approval_false"),
        pytest.param("operator_approval_declared", 0, id="approval_zero"),
        pytest.param("operator_approval_declared", 1, id="approval_one"),
        pytest.param("operator_approval_declared", None, id="approval_none"),
        pytest.param("operator_approval_declared", "true", id="approval_string"),
        pytest.param("writers_stopped_manually_confirmed", False, id="writers_false"),
        pytest.param("live_orders_forbidden_confirmed", False, id="live_false"),
        pytest.param("activation_authorized", True, id="activation_true"),
        pytest.param("activation_authorized", 0, id="activation_zero"),
        pytest.param("activation_authorized", 1, id="activation_one"),
        pytest.param("activation_authorized", None, id="activation_none"),
        pytest.param("runtime_activation_outcome", "go", id="runtime_go"),
        pytest.param("runtime_activation_outcome", 0, id="runtime_zero"),
        pytest.param("runtime_activation_outcome", _StrSub("no_go"), id="runtime_subclass"),
    ],
)
def test_declaration_posture_tamper(field: str, bad: object) -> None:
    payload = _valid_intent_payload()
    payload[field] = bad
    result = _verify(payload)
    if field.startswith("operator") or "writers" in field or "live_orders" in field:
        assert result.reason_codes == ("approval_intent_invalid_declaration",)
    else:
        assert result.reason_codes == ("approval_intent_invalid_activation_posture",)


# --- hash ---


def test_malformed_own_hash_invalid() -> None:
    payload = _valid_intent_payload()
    payload["approval_intent_sha256"] = "NOT_HEX"
    result = _verify(payload)
    assert result.reason_codes == ("approval_intent_invalid_field",)


def test_field_tamper_with_stale_hash_invalid() -> None:
    payload = _valid_intent_payload()
    payload["symbol"] = "000660"
    result = _verify(payload)
    assert result.reason_codes == ("approval_intent_hash_mismatch",)
    assert result.evidence_sha256 == payload["evidence_sha256"]


def test_semantic_invalid_with_recomputed_hash_still_invalid() -> None:
    payload = _valid_intent_payload()
    payload["operator_approval_declared"] = False
    payload["approval_intent_sha256"] = _recompute_hash(payload)
    result = _verify(payload)
    assert result.reason_codes == ("approval_intent_invalid_declaration",)


def test_valid_field_change_with_recomputed_hash_valid() -> None:
    payload = _valid_intent_payload()
    payload["declared_at"] = (_DECL_AT + timedelta(seconds=1)).isoformat()
    payload["approval_intent_sha256"] = _recompute_hash(payload)
    result = _verify(payload)
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.VALID


def test_hash_mismatch_exposes_verified_digests_only() -> None:
    payload = _valid_intent_payload()
    payload["approval_intent_sha256"] = "a" * 64
    result = _verify(payload)
    assert result.reason_codes == ("approval_intent_hash_mismatch",)
    assert result.schema_version == OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION
    assert result.evidence_sha256 == payload["evidence_sha256"]
    assert result.approval_intent_sha256 == "a" * 64


# --- shared scalar validator ---


def test_shared_scalars_reject_wrong_types() -> None:
    good = _valid_intent_payload()
    assert validate_operator_approval_intent_scalars(**good) is not None
    bad = dict(good)
    bad["market"] = "US"
    assert validate_operator_approval_intent_scalars(**bad) is None


def test_verifier_never_raises_on_poison_payload(capsys: pytest.CaptureFixture[str]) -> None:
    class _Poison:
        def __getitem__(self, key: str) -> object:
            raise RuntimeError("POISON")

    result = _verify(_Poison())  # type: ignore[arg-type]
    captured = capsys.readouterr()
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.INVALID
    combined = captured.out + captured.err
    assert "POISON" not in combined
    assert "Traceback" not in combined
