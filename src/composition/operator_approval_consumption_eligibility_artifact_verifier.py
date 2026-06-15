"""Standalone Operator approval-consumption eligibility artifact verification (RTM-7c.4u).

Pure verifier over an untrusted serialized eligibility-artifact payload. Validates strict schema,
the exact 13-field set, exact scalar types, semantic constants, aware-timestamp ordering, and the
canonical ``eligibility_artifact_sha256`` against a recomputation over the actual 12 serialized
content fields — then converts a VALID artifact into an immutable detached snapshot.

VALID means artifact schema/semantic/hash consistency only. It does **not** mean actual approval
consumption, authentication/signature, replay prevention, persistence, freshness/TTL
re-evaluation, or activation authorization. Runtime activation posture stays a constant NO-GO.

Caller-owned payload is observed exactly once into a detached built-in dict; the verifier and
snapshot never re-access the caller payload afterward, so post-snapshot caller mutation cannot
change the verdict or the snapshot. This is not a point-in-time concurrent-atomicity claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from composition.operator_approval_consumption_eligibility_artifact import (
    OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_FIELD_NAMES,
    ValidatedOperatorApprovalConsumptionEligibilityArtifact,
    operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars,
    validate_operator_approval_consumption_eligibility_artifact_scalars_detailed,
)
from decision.canonical_json import payload_sha256

__all__ = [
    "OperatorApprovalConsumptionEligibilityArtifactVerification",
    "OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome",
    "VerifiedOperatorApprovalConsumptionEligibilityArtifact",
    "VerifiedOperatorApprovalConsumptionEligibilityArtifactResult",
    "is_lower_hex64",
    "operator_approval_consumption_eligibility_artifact_verification_metadata_matches_payload",
    "validate_operator_approval_consumption_eligibility_artifact_verification_invariants",
    "validate_verified_operator_approval_consumption_eligibility_artifact_result_invariants",
    "verify_and_snapshot_operator_approval_consumption_eligibility_artifact",
    "verify_operator_approval_consumption_eligibility_artifact_payload",
]

_HEX64_RE = re.compile(r"[0-9a-f]{64}")

_VERIFICATION_METADATA_FIELD_NAMES = (
    "schema_version",
    "approval_intent_schema_version",
    "approval_intent_sha256",
    "candidate_evidence_schema_version",
    "candidate_evidence_sha256",
    "eligibility_artifact_sha256",
)

_VR_NOT_OBJECT = "eligibility_artifact_not_object"
_VR_UNKNOWN_FIELD = "eligibility_artifact_unknown_field"
_VR_MISSING_FIELD = "eligibility_artifact_missing_field"
_VR_INVALID_FIELD = "eligibility_artifact_invalid_field"
_VR_HASH_MISMATCH = "eligibility_artifact_hash_mismatch"


class OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True)
class OperatorApprovalConsumptionEligibilityArtifactVerification:
    """Eligibility-artifact verification verdict — 원문 payload 미보관."""

    outcome: OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome
    schema_version: int | None
    approval_intent_schema_version: int | None
    approval_intent_sha256: str | None
    candidate_evidence_schema_version: int | None
    candidate_evidence_sha256: str | None
    eligibility_artifact_sha256: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedOperatorApprovalConsumptionEligibilityArtifact:
    """검증 완료 eligibility-artifact immutable scalar snapshot — raw payload 미보관.

    13개 field는 schema v1 canonical field set과 정확히 일치한다. Actual consumption, consumed
    marker, replay prevention, persistence, authentication/signature, TTL/freshness 재평가,
    activation authorization이 아니다. Runtime activation posture는 NO-GO."""

    schema_version: int
    checked_at: str
    approval_intent_schema_version: int
    approval_intent_sha256: str
    candidate_evidence_schema_version: int
    candidate_evidence_sha256: str
    market: str
    symbol: str
    evidence_evaluated_at: str
    intent_declared_at: str
    activation_authorized: bool
    runtime_activation_outcome: str
    eligibility_artifact_sha256: str


@dataclass(frozen=True)
class VerifiedOperatorApprovalConsumptionEligibilityArtifactResult:
    """Snapshot build verdict — VALID이면 immutable ``snapshot``, INVALID이면 ``None``."""

    outcome: OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome
    reason_codes: tuple[str, ...]
    snapshot: VerifiedOperatorApprovalConsumptionEligibilityArtifact | None


def is_exact_int(value: object) -> bool:
    """Exact built-in ``int`` 여부 — ``bool``/``Decimal``/subclass는 거부한다."""

    return type(value) is int


def is_lower_hex64(value: object) -> bool:
    """Exact lowercase hex64 digest 여부."""

    return type(value) is str and _HEX64_RE.fullmatch(value) is not None


def validate_operator_approval_consumption_eligibility_artifact_verification_invariants(
    result: object,
) -> bool:
    """Verifier 반환값이 exact type과 VALID/INVALID outcome invariant를 만족하는지 검사한다.

    Custom object의 property getter는 호출하지 않는다 — exact-type guard가 먼저 실행된다."""

    if type(result) is not OperatorApprovalConsumptionEligibilityArtifactVerification:
        return False

    outcome = result.outcome
    reason_codes = result.reason_codes

    if outcome is OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.VALID:
        if type(reason_codes) is not tuple or reason_codes != ():
            return False
        return (
            is_exact_int(result.schema_version)
            and is_exact_int(result.approval_intent_schema_version)
            and is_exact_int(result.candidate_evidence_schema_version)
            and is_lower_hex64(result.approval_intent_sha256)
            and is_lower_hex64(result.candidate_evidence_sha256)
            and is_lower_hex64(result.eligibility_artifact_sha256)
        )

    if outcome is OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.INVALID:
        if type(reason_codes) is not tuple or len(reason_codes) != 1:
            return False
        return type(reason_codes[0]) is str

    return False


def operator_approval_consumption_eligibility_artifact_verification_metadata_matches_payload(
    result: OperatorApprovalConsumptionEligibilityArtifactVerification,
    payload: dict[str, object],
) -> bool:
    """VALID verification metadata가 capture된 payload metadata와 exact 일치하는지 검사한다."""

    for field_name in _VERIFICATION_METADATA_FIELD_NAMES:
        if getattr(result, field_name) != payload[field_name]:
            return False
    return True


def validate_verified_operator_approval_consumption_eligibility_artifact_result_invariants(
    result: object,
) -> bool:
    """``verify_and_snapshot_...`` 반환값이 exact type과 VALID/INVALID invariant를 만족하는지 검사한다."""

    if type(result) is not VerifiedOperatorApprovalConsumptionEligibilityArtifactResult:
        return False

    outcome = result.outcome
    reason_codes = result.reason_codes

    if outcome is OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.VALID:
        if type(reason_codes) is not tuple or reason_codes != ():
            return False
        return type(result.snapshot) is VerifiedOperatorApprovalConsumptionEligibilityArtifact

    if outcome is OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.INVALID:
        if type(reason_codes) is not tuple or len(reason_codes) != 1:
            return False
        if type(reason_codes[0]) is not str:
            return False
        return result.snapshot is None

    return False


def verify_operator_approval_consumption_eligibility_artifact_payload(
    payload: object,
) -> OperatorApprovalConsumptionEligibilityArtifactVerification:
    """Untrusted serialized eligibility-artifact의 strict schema·semantic·hash 일치를 검증한다.

    VALID는 schema/semantic/hash consistency만 의미한다. Actual consumption, authentication,
    replay prevention, persistence, freshness/TTL 재평가, activation authorization이 아니다."""

    try:
        detached, reason = _snapshot_artifact_payload(payload)
        if reason is not None:
            return _invalid(reason)
        assert detached is not None
        verification, _snapshot = _verify_detached_artifact(detached)
        return verification
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _invalid(_VR_INVALID_FIELD)


def verify_and_snapshot_operator_approval_consumption_eligibility_artifact(
    payload: object,
) -> VerifiedOperatorApprovalConsumptionEligibilityArtifactResult:
    """Untrusted payload를 한 번 snapshot한 뒤 검증하고 immutable verified snapshot을 반환한다.

    INVALID면 ``snapshot``은 ``None``. Raw payload/dict/list reference, authentication, actual
    consumption, persistence, activation authorization을 결과에 보관하지 않는다."""

    try:
        detached, reason = _snapshot_artifact_payload(payload)
        if reason is not None:
            return VerifiedOperatorApprovalConsumptionEligibilityArtifactResult(
                outcome=OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.INVALID,
                reason_codes=(reason,),
                snapshot=None,
            )
        assert detached is not None
        verification, snapshot = _verify_detached_artifact(detached)
        return VerifiedOperatorApprovalConsumptionEligibilityArtifactResult(
            outcome=verification.outcome,
            reason_codes=verification.reason_codes,
            snapshot=snapshot,
        )
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return VerifiedOperatorApprovalConsumptionEligibilityArtifactResult(
            outcome=OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.INVALID,
            reason_codes=(_VR_INVALID_FIELD,),
            snapshot=None,
        )


def _verify_detached_artifact(
    detached: dict[str, object],
) -> tuple[
    OperatorApprovalConsumptionEligibilityArtifactVerification,
    VerifiedOperatorApprovalConsumptionEligibilityArtifact | None,
]:
    """Detached built-in dict에 대해 schema·semantic·hash 검증을 정확히 1회 수행한다."""

    scalar_result = validate_operator_approval_consumption_eligibility_artifact_scalars_detailed(
        schema_version=detached["schema_version"],
        checked_at=detached["checked_at"],
        approval_intent_schema_version=detached["approval_intent_schema_version"],
        approval_intent_sha256=detached["approval_intent_sha256"],
        candidate_evidence_schema_version=detached["candidate_evidence_schema_version"],
        candidate_evidence_sha256=detached["candidate_evidence_sha256"],
        market=detached["market"],
        symbol=detached["symbol"],
        evidence_evaluated_at=detached["evidence_evaluated_at"],
        intent_declared_at=detached["intent_declared_at"],
        activation_authorized=detached["activation_authorized"],
        runtime_activation_outcome=detached["runtime_activation_outcome"],
        eligibility_artifact_sha256=detached["eligibility_artifact_sha256"],
    )
    validated = scalar_result.validated
    if validated is None:
        return _invalid(scalar_result.reason_code or _VR_INVALID_FIELD), None

    recomputed = payload_sha256(
        operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars(
            schema_version=validated.schema_version,
            checked_at=validated.checked_at,
            approval_intent_schema_version=validated.approval_intent_schema_version,
            approval_intent_sha256=validated.approval_intent_sha256,
            candidate_evidence_schema_version=validated.candidate_evidence_schema_version,
            candidate_evidence_sha256=validated.candidate_evidence_sha256,
            market=validated.market,
            symbol=validated.symbol,
            evidence_evaluated_at=validated.evidence_evaluated_at,
            intent_declared_at=validated.intent_declared_at,
            activation_authorized=validated.activation_authorized,
            runtime_activation_outcome=validated.runtime_activation_outcome,
        )
    )
    if recomputed != validated.eligibility_artifact_sha256:
        return _invalid(_VR_HASH_MISMATCH, validated=validated), None

    snapshot = VerifiedOperatorApprovalConsumptionEligibilityArtifact(
        schema_version=validated.schema_version,
        checked_at=validated.checked_at,
        approval_intent_schema_version=validated.approval_intent_schema_version,
        approval_intent_sha256=validated.approval_intent_sha256,
        candidate_evidence_schema_version=validated.candidate_evidence_schema_version,
        candidate_evidence_sha256=validated.candidate_evidence_sha256,
        market=validated.market,
        symbol=validated.symbol,
        evidence_evaluated_at=validated.evidence_evaluated_at,
        intent_declared_at=validated.intent_declared_at,
        activation_authorized=validated.activation_authorized,
        runtime_activation_outcome=validated.runtime_activation_outcome,
        eligibility_artifact_sha256=validated.eligibility_artifact_sha256,
    )
    return (
        OperatorApprovalConsumptionEligibilityArtifactVerification(
            outcome=OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.VALID,
            schema_version=validated.schema_version,
            approval_intent_schema_version=validated.approval_intent_schema_version,
            approval_intent_sha256=validated.approval_intent_sha256,
            candidate_evidence_schema_version=validated.candidate_evidence_schema_version,
            candidate_evidence_sha256=validated.candidate_evidence_sha256,
            eligibility_artifact_sha256=validated.eligibility_artifact_sha256,
            reason_codes=(),
        ),
        snapshot,
    )


def _snapshot_artifact_payload(
    payload: object,
) -> tuple[dict[str, object] | None, str | None]:
    """Caller-owned dict를 한 번 관찰해 exact built-in key detached tree로 동결한다.

    ``tuple(payload.items())`` 직후 각 key에 ``type(key) is str``을 요구한다.
    ``set(payload.keys())`` 등 hash/equality hook을 유발하는 연산은 사용하지 않는다.
    Dict subclass는 거부한다."""

    if type(payload) is not dict:
        return None, _VR_NOT_OBJECT

    try:
        observed = tuple(payload.items())
    except MemoryError:
        raise
    except KeyboardInterrupt:
        raise
    except SystemExit:
        raise
    except (RuntimeError, KeyError):
        return None, _VR_UNKNOWN_FIELD

    detached: dict[str, object] = {}
    seen: set[str] = set()
    for key, value in observed:
        if type(key) is not str:
            return None, _VR_UNKNOWN_FIELD
        if key not in OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_FIELD_NAMES:
            return None, _VR_UNKNOWN_FIELD
        if key in seen:
            return None, _VR_UNKNOWN_FIELD
        seen.add(key)
        detached[key] = value

    if OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_FIELD_NAMES - seen:
        return None, _VR_MISSING_FIELD

    return detached, None


def _invalid(
    reason: str,
    *,
    validated: ValidatedOperatorApprovalConsumptionEligibilityArtifact | None = None,
) -> OperatorApprovalConsumptionEligibilityArtifactVerification:
    if validated is not None:
        return OperatorApprovalConsumptionEligibilityArtifactVerification(
            outcome=OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.INVALID,
            schema_version=validated.schema_version,
            approval_intent_schema_version=validated.approval_intent_schema_version,
            approval_intent_sha256=validated.approval_intent_sha256,
            candidate_evidence_schema_version=validated.candidate_evidence_schema_version,
            candidate_evidence_sha256=validated.candidate_evidence_sha256,
            eligibility_artifact_sha256=validated.eligibility_artifact_sha256,
            reason_codes=(reason,),
        )
    return OperatorApprovalConsumptionEligibilityArtifactVerification(
        outcome=OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome.INVALID,
        schema_version=None,
        approval_intent_schema_version=None,
        approval_intent_sha256=None,
        candidate_evidence_schema_version=None,
        candidate_evidence_sha256=None,
        eligibility_artifact_sha256=None,
        reason_codes=(reason,),
    )
