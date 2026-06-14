"""Standalone Operator approval-intent verification (RTM-7c.4q).

Pure verifier over untrusted JSON-decoded approval-intent payloads. Validates strict schema,
semantic posture, and ``approval_intent_sha256`` against a canonical payload recomputation.
Does **not** authenticate Operator identity, verify signatures, revalidate evidence contents,
consume approval, or authorize runtime activation.

Duplicate object key detection is the CLI stdin JSON layer's responsibility — not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from composition.activation_candidate_evidence import ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION
from composition.operator_approval_intent import (
    APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE,
    OPERATOR_APPROVAL_INTENT_FIELD_NAMES,
    OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
    operator_approval_intent_hash_payload,
    validate_operator_approval_intent_scalars,
)
from composition.precheck_receipt_schema import checked_at_valid, is_hex64, market_valid, symbol_valid
from decision.canonical_json import payload_sha256

__all__ = [
    "OperatorApprovalIntentVerification",
    "OperatorApprovalIntentVerificationOutcome",
    "verify_operator_approval_intent_payload",
]


class OperatorApprovalIntentVerificationOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class OperatorApprovalIntentVerification:
    """Approval-intent verification verdict — 원문 intent payload 미보관."""

    outcome: OperatorApprovalIntentVerificationOutcome
    schema_version: int | None
    evidence_schema_version: int | None
    evidence_sha256: str | None
    approval_intent_sha256: str | None
    reason_codes: tuple[str, ...]


def verify_operator_approval_intent_payload(
    payload: object,
) -> OperatorApprovalIntentVerification:
    """Untrusted approval-intent JSON object의 strict schema·semantic·hash 일치를 검증한다.

    VALID는 schema 준수 + canonical hash 일치만 의미한다. Operator identity, signature/HMAC,
    evidence 내용 재검증, approval consumption, replay prevention, freshness, activation
    authorization이 아니다."""

    if type(payload) is not dict:
        return _invalid("approval_intent_not_object")

    keys = set(payload.keys())
    if keys - OPERATOR_APPROVAL_INTENT_FIELD_NAMES:
        return _invalid("approval_intent_unknown_field")
    if OPERATOR_APPROVAL_INTENT_FIELD_NAMES - keys:
        return _invalid("approval_intent_missing_field")

    schema_version = payload["schema_version"]
    if type(schema_version) is not int or isinstance(schema_version, bool):
        return _invalid("approval_intent_invalid_field")
    if schema_version != OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION:
        return _invalid("approval_intent_unsupported_schema")

    if not checked_at_valid(payload["declared_at"]):
        return _invalid("approval_intent_invalid_declared_at")

    evidence_schema_version = payload["evidence_schema_version"]
    evidence_sha256 = payload["evidence_sha256"]
    if not _evidence_binding_valid(evidence_schema_version, evidence_sha256):
        return _invalid("approval_intent_invalid_evidence_binding")

    market = payload["market"]
    symbol = payload["symbol"]
    approval_scope = payload["approval_scope"]
    if not _identity_scope_valid(market, symbol, approval_scope):
        if type(approval_scope) is not str or approval_scope != APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE:
            return _invalid("approval_intent_invalid_scope")
        return _invalid("approval_intent_invalid_field")

    if not _declarations_valid(
        payload["operator_approval_declared"],
        payload["writers_stopped_manually_confirmed"],
        payload["live_orders_forbidden_confirmed"],
    ):
        return _invalid("approval_intent_invalid_declaration")

    if not _activation_posture_valid(
        payload["activation_authorized"],
        payload["runtime_activation_outcome"],
    ):
        return _invalid("approval_intent_invalid_activation_posture")

    stored_hash = payload["approval_intent_sha256"]
    if not is_hex64(stored_hash):
        return _invalid("approval_intent_invalid_field")

    validated = validate_operator_approval_intent_scalars(
        schema_version=schema_version,
        declared_at=payload["declared_at"],
        evidence_schema_version=evidence_schema_version,
        evidence_sha256=evidence_sha256,
        market=market,
        symbol=symbol,
        approval_scope=approval_scope,
        operator_approval_declared=payload["operator_approval_declared"],
        writers_stopped_manually_confirmed=payload["writers_stopped_manually_confirmed"],
        live_orders_forbidden_confirmed=payload["live_orders_forbidden_confirmed"],
        activation_authorized=payload["activation_authorized"],
        runtime_activation_outcome=payload["runtime_activation_outcome"],
        approval_intent_sha256=stored_hash,
    )
    if validated is None:
        return _invalid("approval_intent_invalid_field")

    hash_payload = operator_approval_intent_hash_payload(
        declared_at=validated.declared_at,
        evidence_schema_version=validated.evidence_schema_version,
        evidence_sha256=validated.evidence_sha256,
        market=validated.market,
        symbol=validated.symbol,
    )
    recomputed = payload_sha256(hash_payload)
    if recomputed != stored_hash:
        return _invalid(
            "approval_intent_hash_mismatch",
            schema_version=schema_version,
            evidence_schema_version=validated.evidence_schema_version,
            evidence_sha256=validated.evidence_sha256,
            approval_intent_sha256=stored_hash,
        )

    return OperatorApprovalIntentVerification(
        outcome=OperatorApprovalIntentVerificationOutcome.VALID,
        schema_version=schema_version,
        evidence_schema_version=validated.evidence_schema_version,
        evidence_sha256=validated.evidence_sha256,
        approval_intent_sha256=stored_hash,
        reason_codes=(),
    )



def _evidence_binding_valid(
    evidence_schema_version: object,
    evidence_sha256: object,
) -> bool:
    if type(evidence_schema_version) is not int or isinstance(evidence_schema_version, bool):
        return False
    if evidence_schema_version != ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION:
        return False
    return is_hex64(evidence_sha256)


def _identity_scope_valid(market: object, symbol: object, approval_scope: object) -> bool:
    if not market_valid(market):
        return False
    if not symbol_valid(symbol):
        return False
    return type(approval_scope) is str and approval_scope == APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE


def _declarations_valid(
    operator_approval_declared: object,
    writers_stopped_manually_confirmed: object,
    live_orders_forbidden_confirmed: object,
) -> bool:
    return (
        type(operator_approval_declared) is bool
        and operator_approval_declared is True
        and type(writers_stopped_manually_confirmed) is bool
        and writers_stopped_manually_confirmed is True
        and type(live_orders_forbidden_confirmed) is bool
        and live_orders_forbidden_confirmed is True
    )


def _activation_posture_valid(
    activation_authorized: object,
    runtime_activation_outcome: object,
) -> bool:
    return (
        type(activation_authorized) is bool
        and activation_authorized is False
        and type(runtime_activation_outcome) is str
        and runtime_activation_outcome == "no_go"
    )


def _invalid(
    reason: str,
    *,
    schema_version: int | None = None,
    evidence_schema_version: int | None = None,
    evidence_sha256: str | None = None,
    approval_intent_sha256: str | None = None,
) -> OperatorApprovalIntentVerification:
    return OperatorApprovalIntentVerification(
        outcome=OperatorApprovalIntentVerificationOutcome.INVALID,
        schema_version=schema_version,
        evidence_schema_version=evidence_schema_version,
        evidence_sha256=evidence_sha256,
        approval_intent_sha256=approval_intent_sha256,
        reason_codes=(reason,),
    )
