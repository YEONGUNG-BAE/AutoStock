"""Standalone Operator approval-intent verification (RTM-7c.4r).

Pure verifier over untrusted JSON-decoded approval-intent payloads. Validates strict schema,
semantic posture, and ``approval_intent_sha256`` against a canonical payload recomputation.
Does **not** authenticate Operator identity, verify signatures, revalidate evidence contents,
consume approval, or authorize runtime activation.

Duplicate object key detection is the CLI stdin JSON layer's responsibility — not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from composition.operator_approval_intent import (
    OPERATOR_APPROVAL_INTENT_FIELD_NAMES,
    operator_approval_intent_hash_payload,
    validate_operator_approval_intent_scalars_detailed,
)
from decision.canonical_json import payload_sha256

__all__ = [
    "OperatorApprovalIntentVerification",
    "OperatorApprovalIntentVerificationOutcome",
    "VerifiedOperatorApprovalIntent",
    "VerifiedOperatorApprovalIntentResult",
    "verify_and_snapshot_operator_approval_intent",
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


@dataclass(frozen=True)
class VerifiedOperatorApprovalIntent:
    """검증 완료 approval-intent immutable scalar snapshot — raw payload 미보관.

    13개 field는 schema v1 canonical field set과 정확히 일치한다. Authentication, signature,
    evidence 내용 재검증, consumption, persistence, activation authorization이 아니다."""

    schema_version: int
    declared_at: str
    evidence_schema_version: int
    evidence_sha256: str
    market: str
    symbol: str
    approval_scope: str
    operator_approval_declared: bool
    writers_stopped_manually_confirmed: bool
    live_orders_forbidden_confirmed: bool
    activation_authorized: bool
    runtime_activation_outcome: str
    approval_intent_sha256: str


@dataclass(frozen=True)
class VerifiedOperatorApprovalIntentResult:
    """Snapshot build verdict — VALID이면 immutable ``snapshot``, INVALID이면 ``None``."""

    outcome: OperatorApprovalIntentVerificationOutcome
    reason_codes: tuple[str, ...]
    snapshot: VerifiedOperatorApprovalIntent | None


def verify_operator_approval_intent_payload(
    payload: object,
) -> OperatorApprovalIntentVerification:
    """Untrusted approval-intent JSON object의 strict schema·semantic·hash 일치를 검증한다.

    VALID는 schema 준수 + canonical hash 일치만 의미한다. Operator identity, signature/HMAC,
    evidence 내용 재검증, approval consumption, replay prevention, freshness, activation
    authorization이 아니다."""

    try:
        detached, reason = _snapshot_operator_approval_intent_payload(payload)
        if reason is not None:
            return _invalid(reason)
        assert detached is not None
        verification, _snapshot = _verify_detached_operator_approval_intent(detached)
        return verification
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _invalid("approval_intent_invalid_field")


def verify_and_snapshot_operator_approval_intent(
    payload: object,
) -> VerifiedOperatorApprovalIntentResult:
    """Untrusted payload를 한 번 snapshot한 뒤 schema·semantic·hash 검증하고 immutable snapshot을 반환한다.

    INVALID면 ``snapshot``은 ``None``. Raw payload reference, authentication, consumption,
    persistence, activation authorization을 결과에 보관하지 않는다."""

    try:
        detached, reason = _snapshot_operator_approval_intent_payload(payload)
        if reason is not None:
            verification = _invalid(reason)
            return VerifiedOperatorApprovalIntentResult(
                outcome=verification.outcome,
                reason_codes=verification.reason_codes,
                snapshot=None,
            )
        assert detached is not None
        verification, snapshot = _verify_detached_operator_approval_intent(detached)
        return VerifiedOperatorApprovalIntentResult(
            outcome=verification.outcome,
            reason_codes=verification.reason_codes,
            snapshot=snapshot,
        )
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        verification = _invalid("approval_intent_invalid_field")
        return VerifiedOperatorApprovalIntentResult(
            outcome=verification.outcome,
            reason_codes=verification.reason_codes,
            snapshot=None,
        )


def _verify_detached_operator_approval_intent(
    detached: dict[str, object],
) -> tuple[OperatorApprovalIntentVerification, VerifiedOperatorApprovalIntent | None]:
    """Detached built-in dict에 대해 schema·semantic·hash 검증을 정확히 1회 수행한다."""

    schema_version = detached["schema_version"]
    scalar_result = validate_operator_approval_intent_scalars_detailed(
        schema_version=schema_version,
        declared_at=detached["declared_at"],
        evidence_schema_version=detached["evidence_schema_version"],
        evidence_sha256=detached["evidence_sha256"],
        market=detached["market"],
        symbol=detached["symbol"],
        approval_scope=detached["approval_scope"],
        operator_approval_declared=detached["operator_approval_declared"],
        writers_stopped_manually_confirmed=detached["writers_stopped_manually_confirmed"],
        live_orders_forbidden_confirmed=detached["live_orders_forbidden_confirmed"],
        activation_authorized=detached["activation_authorized"],
        runtime_activation_outcome=detached["runtime_activation_outcome"],
        approval_intent_sha256=detached["approval_intent_sha256"],
    )
    if scalar_result.validated is None:
        reason = scalar_result.reason_code or "approval_intent_invalid_field"
        extra: dict[str, object] = {}
        if (
            reason == "approval_intent_unsupported_schema"
            and type(schema_version) is int
            and not isinstance(schema_version, bool)
        ):
            extra["schema_version"] = schema_version
        return _invalid(reason, **extra), None  # type: ignore[arg-type]

    validated = scalar_result.validated
    stored_hash = detached["approval_intent_sha256"]

    hash_payload = operator_approval_intent_hash_payload(
        declared_at=validated.declared_at,
        evidence_schema_version=validated.evidence_schema_version,
        evidence_sha256=validated.evidence_sha256,
        market=validated.market,
        symbol=validated.symbol,
    )
    recomputed = payload_sha256(hash_payload)
    if recomputed != stored_hash:
        return (
            _invalid(
                "approval_intent_hash_mismatch",
                schema_version=validated.schema_version,
                evidence_schema_version=validated.evidence_schema_version,
                evidence_sha256=validated.evidence_sha256,
                approval_intent_sha256=stored_hash,
            ),
            None,
        )

    snapshot = VerifiedOperatorApprovalIntent(
        schema_version=validated.schema_version,
        declared_at=validated.declared_at,
        evidence_schema_version=validated.evidence_schema_version,
        evidence_sha256=validated.evidence_sha256,
        market=validated.market,
        symbol=validated.symbol,
        approval_scope=validated.approval_scope,
        operator_approval_declared=validated.operator_approval_declared,
        writers_stopped_manually_confirmed=validated.writers_stopped_manually_confirmed,
        live_orders_forbidden_confirmed=validated.live_orders_forbidden_confirmed,
        activation_authorized=validated.activation_authorized,
        runtime_activation_outcome=validated.runtime_activation_outcome,
        approval_intent_sha256=validated.approval_intent_sha256,
    )
    return (
        OperatorApprovalIntentVerification(
            outcome=OperatorApprovalIntentVerificationOutcome.VALID,
            schema_version=validated.schema_version,
            evidence_schema_version=validated.evidence_schema_version,
            evidence_sha256=validated.evidence_sha256,
            approval_intent_sha256=stored_hash,
            reason_codes=(),
        ),
        snapshot,
    )


def _snapshot_operator_approval_intent_payload(
    payload: object,
) -> tuple[dict[str, object] | None, str | None]:
    """Caller-owned dict를 한 번 관찰해 exact built-in key detached tree로 동결한다.

    ``tuple(payload.items())`` 직후 각 key에 ``type(key) is str``을 요구한다.
    ``set(payload.keys())`` 등 hash/equality hook을 유발하는 연산은 사용하지 않는다."""

    if type(payload) is not dict:
        return None, "approval_intent_not_object"

    try:
        observed = tuple(payload.items())
    except MemoryError:
        raise
    except KeyboardInterrupt:
        raise
    except SystemExit:
        raise
    except (RuntimeError, KeyError):
        return None, "approval_intent_unknown_field"

    detached: dict[str, object] = {}
    seen: set[str] = set()
    for key, value in observed:
        if type(key) is not str:
            return None, "approval_intent_unknown_field"
        if key not in OPERATOR_APPROVAL_INTENT_FIELD_NAMES:
            return None, "approval_intent_unknown_field"
        if key in seen:
            return None, "approval_intent_unknown_field"
        seen.add(key)
        detached[key] = value

    if OPERATOR_APPROVAL_INTENT_FIELD_NAMES - seen:
        return None, "approval_intent_missing_field"

    return detached, None


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
