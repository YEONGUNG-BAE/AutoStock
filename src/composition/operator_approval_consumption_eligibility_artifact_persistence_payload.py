"""Canonical persistence-payload encode/decode for the verified eligibility artifact (RTM-7c.4w).

Pure API that fixes the *byte format* and strict round-trip of a verified eligibility-artifact
snapshot. The encoder turns an immutable verified snapshot into canonical UTF-8 JSON bytes; the
decoder interprets bounded strict JSON bytes and re-validates them through the existing artifact
verifier into a fresh immutable snapshot.

This lane is **API-only**. It does **not** create or read any file, handle any path, persist
state, claim persistence success, consume approval, create a consumed marker, prevent replay,
sign/HMAC, authenticate Operator identity, verify provenance, or authorize runtime activation.

Decoder VALID means the canonical payload satisfies the artifact schema/semantic/hash
*consistency* only — never authenticity, provenance, persistence, or activation. A freshly minted
semantically valid payload with a recomputed digest can decode VALID; that is consistency, not
proof of where the bytes came from. Runtime activation posture stays a constant NO-GO.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from composition.operator_approval_consumption_eligibility_artifact_verifier import (
    OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome,
    VerifiedOperatorApprovalConsumptionEligibilityArtifact,
    operator_approval_consumption_eligibility_artifact_verification_metadata_matches_payload,
    validate_operator_approval_consumption_eligibility_artifact_verification_invariants,
    validate_verified_operator_approval_consumption_eligibility_artifact_result_invariants,
    verify_and_snapshot_operator_approval_consumption_eligibility_artifact,
    verify_operator_approval_consumption_eligibility_artifact_payload,
)
from composition.precheck_receipt_stdin_json import (
    ReceiptStdinJsonError,
    parse_receipt_stdin_json,
)
from decision.canonical_json import canonical_json_dumps

__all__ = [
    "ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES",
    "EligibilityArtifactPersistencePayloadOutcome",
    "EligibilityArtifactPersistencePayloadResult",
    "EligibilityArtifactPersistencePayloadVerification",
    "EligibilityArtifactPersistencePayloadVerificationOutcome",
    "decode_operator_approval_consumption_eligibility_artifact_payload",
    "encode_verified_operator_approval_consumption_eligibility_artifact",
]

ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES = 1 << 20  # 1 MiB — untrusted bound

_ENCODE_INVALID = "eligibility_persistence_payload_invalid_snapshot"

_DECODE_NOT_BYTES = "eligibility_persistence_payload_not_bytes"
_DECODE_EMPTY = "eligibility_persistence_payload_empty"
_DECODE_TOO_LARGE = "eligibility_persistence_payload_too_large"
_DECODE_NOT_UTF8 = "eligibility_persistence_payload_not_utf8"
_DECODE_NOT_JSON = "eligibility_persistence_payload_not_json"
_DECODE_TOO_DEEP = "eligibility_persistence_payload_too_deep"
_DECODE_DUPLICATE_KEY = "eligibility_persistence_payload_duplicate_key"
_DECODE_INVALID_ARTIFACT = "eligibility_persistence_payload_invalid_artifact"
_DECODE_NOT_CANONICAL = "eligibility_persistence_payload_not_canonical"

# Strict-parser reason → persistence decode reason (외부 노출 namespace 분리).
_PARSER_TO_DECODE_REASON: dict[str, str] = {
    "receipt_input_not_json": _DECODE_NOT_JSON,
    "receipt_input_too_deep": _DECODE_TOO_DEEP,
    "receipt_input_duplicate_key": _DECODE_DUPLICATE_KEY,
}


class EligibilityArtifactPersistencePayloadOutcome(StrEnum):
    CREATED = "created"
    INVALID = "invalid"


class EligibilityArtifactPersistencePayloadVerificationOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class EligibilityArtifactPersistencePayloadResult:
    """Encode verdict — CREATED이면 canonical bytes, INVALID이면 ``None``. Raw field/timestamp/
    exception 미보관."""

    outcome: EligibilityArtifactPersistencePayloadOutcome
    reason_codes: tuple[str, ...]
    payload_bytes: bytes | None
    eligibility_artifact_sha256: str | None


@dataclass(frozen=True)
class EligibilityArtifactPersistencePayloadVerification:
    """Decode verdict — VALID이면 immutable verified snapshot, INVALID이면 ``None``. Raw bytes/
    parser dict reference 미보관."""

    outcome: EligibilityArtifactPersistencePayloadVerificationOutcome
    reason_codes: tuple[str, ...]
    snapshot: VerifiedOperatorApprovalConsumptionEligibilityArtifact | None


def encode_verified_operator_approval_consumption_eligibility_artifact(
    snapshot: object,
) -> EligibilityArtifactPersistencePayloadResult:
    """Verified immutable snapshot을 deterministic canonical UTF-8 JSON bytes로 변환한다.

    13개 scalar를 정확히 1회씩 local로 읽어 built-in dict를 만들고, 기존 artifact verifier로
    정확히 1회 재검증한 뒤 VALID일 때만 canonical bytes를 만든다. Caller snapshot은 이후 재접근하지
    않으므로 capture 후 손상돼도 결과는 불변. 파일 생성/persistence/consumption/signing/activation이
    아니다."""

    try:
        return _encode(snapshot)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _encode_invalid()


def decode_operator_approval_consumption_eligibility_artifact_payload(
    payload_bytes: object,
) -> EligibilityArtifactPersistencePayloadVerification:
    """Canonical persistence bytes를 bounded strict JSON으로 해석하고 기존 artifact verifier로
    재검증해 immutable verified snapshot을 반환한다.

    VALID는 schema/semantic/hash consistency만 의미한다. 실제 파일 저장, payload origin/provenance,
    Operator identity, signature, approval consumption, replay prevention, activation authorization이
    아니다. 파일 읽기/path 처리를 하지 않는다."""

    return _decode(payload_bytes)


def _artifact_payload_dict_from_snapshot(
    snapshot: VerifiedOperatorApprovalConsumptionEligibilityArtifact,
) -> dict[str, object]:
    """Verified snapshot 13 scalar를 canonical emission source용 built-in dict로 변환한다."""

    return {
        "schema_version": snapshot.schema_version,
        "checked_at": snapshot.checked_at,
        "approval_intent_schema_version": snapshot.approval_intent_schema_version,
        "approval_intent_sha256": snapshot.approval_intent_sha256,
        "candidate_evidence_schema_version": snapshot.candidate_evidence_schema_version,
        "candidate_evidence_sha256": snapshot.candidate_evidence_sha256,
        "market": snapshot.market,
        "symbol": snapshot.symbol,
        "evidence_evaluated_at": snapshot.evidence_evaluated_at,
        "intent_declared_at": snapshot.intent_declared_at,
        "activation_authorized": snapshot.activation_authorized,
        "runtime_activation_outcome": snapshot.runtime_activation_outcome,
        "eligibility_artifact_sha256": snapshot.eligibility_artifact_sha256,
    }


def _encode(snapshot: object) -> EligibilityArtifactPersistencePayloadResult:
    if type(snapshot) is not VerifiedOperatorApprovalConsumptionEligibilityArtifact:
        return _encode_invalid()

    # 13 scalar fields는 각각 정확히 1회만 local로 읽는다(asdict 신뢰 금지). 이후 caller snapshot은
    # 재접근하지 않는다.
    payload = _artifact_payload_dict_from_snapshot(snapshot)

    try:
        verification = verify_operator_approval_consumption_eligibility_artifact_payload(payload)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _encode_invalid()

    if not validate_operator_approval_consumption_eligibility_artifact_verification_invariants(
        verification
    ):
        return _encode_invalid()
    if (
        verification.outcome
        is not OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.VALID
    ):
        return _encode_invalid()
    if not operator_approval_consumption_eligibility_artifact_verification_metadata_matches_payload(
        verification, payload
    ):
        return _encode_invalid()

    try:
        encoded = canonical_json_dumps(payload).encode("utf-8")
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _encode_invalid()

    return EligibilityArtifactPersistencePayloadResult(
        outcome=EligibilityArtifactPersistencePayloadOutcome.CREATED,
        reason_codes=(),
        payload_bytes=encoded,
        eligibility_artifact_sha256=verification.eligibility_artifact_sha256,
    )


def _decode(payload_bytes: object) -> EligibilityArtifactPersistencePayloadVerification:
    if type(payload_bytes) is not bytes:
        return _decode_invalid(_DECODE_NOT_BYTES)
    if len(payload_bytes) == 0:
        return _decode_invalid(_DECODE_EMPTY)
    if len(payload_bytes) > ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES:
        return _decode_invalid(_DECODE_TOO_LARGE)

    try:
        text = payload_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _decode_invalid(_DECODE_NOT_UTF8)

    try:
        parsed = parse_receipt_stdin_json(text)
    except ReceiptStdinJsonError as exc:
        return _decode_invalid(_PARSER_TO_DECODE_REASON.get(exc.reason_code, _DECODE_NOT_JSON))
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _decode_invalid(_DECODE_NOT_JSON)

    try:
        result = verify_and_snapshot_operator_approval_consumption_eligibility_artifact(parsed)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _decode_invalid(_DECODE_INVALID_ARTIFACT)

    if not validate_verified_operator_approval_consumption_eligibility_artifact_result_invariants(
        result
    ):
        return _decode_invalid(_DECODE_INVALID_ARTIFACT)

    if (
        result.outcome
        is not OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.VALID
    ):
        # Artifact verifier reason(예: eligibility_artifact_not_object / missing_field /
        # invalid_field / hash_mismatch)을 그대로 보존한다.
        return _decode_invalid(result.reason_codes[0])

    snapshot = result.snapshot
    assert snapshot is not None

    try:
        canonical_bytes = canonical_json_dumps(
            _artifact_payload_dict_from_snapshot(snapshot)
        ).encode("utf-8")
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _decode_invalid(_DECODE_INVALID_ARTIFACT)

    if canonical_bytes != payload_bytes:
        return _decode_invalid(_DECODE_NOT_CANONICAL)

    return EligibilityArtifactPersistencePayloadVerification(
        outcome=EligibilityArtifactPersistencePayloadVerificationOutcome.VALID,
        reason_codes=(),
        snapshot=snapshot,
    )


def _encode_invalid() -> EligibilityArtifactPersistencePayloadResult:
    return EligibilityArtifactPersistencePayloadResult(
        outcome=EligibilityArtifactPersistencePayloadOutcome.INVALID,
        reason_codes=(_ENCODE_INVALID,),
        payload_bytes=None,
        eligibility_artifact_sha256=None,
    )


def _decode_invalid(reason: str) -> EligibilityArtifactPersistencePayloadVerification:
    return EligibilityArtifactPersistencePayloadVerification(
        outcome=EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID,
        reason_codes=(reason,),
        snapshot=None,
    )
