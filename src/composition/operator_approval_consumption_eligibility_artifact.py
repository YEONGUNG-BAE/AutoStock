"""Operator approval consumption eligibility artifact builder (RTM-7c.4t).

Pure API that freezes an already-produced ELIGIBLE consumption-eligibility observation
(RTM-7c.4s) into a canonical immutable artifact with a stable digest.

This artifact is **not** consumption. It does **not** consume approval, create a consumed
marker, prevent replay, persist state, sign/HMAC, authenticate Operator identity, re-evaluate
TTL/freshness, or authorize runtime activation. Runtime activation posture stays NO-GO.
The upstream eligibility API is **not** re-run — the builder reads the completed observation only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from composition.activation_candidate_evidence import (
    ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
)
from composition.operator_approval_consumption_eligibility import (
    OperatorApprovalConsumptionEligibility,
    OperatorApprovalConsumptionEligibilityOutcome,
    OperatorApprovalConsumptionEligibilityResult,
)
from composition.operator_approval_intent import (
    OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
)
from decision.canonical_json import payload_sha256

__all__ = [
    "OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_FIELD_NAMES",
    "OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION",
    "OperatorApprovalConsumptionEligibilityArtifact",
    "OperatorApprovalConsumptionEligibilityArtifactOutcome",
    "OperatorApprovalConsumptionEligibilityArtifactContentScalarValidation",
    "OperatorApprovalConsumptionEligibilityArtifactResult",
    "OperatorApprovalConsumptionEligibilityArtifactScalarValidation",
    "ValidatedOperatorApprovalConsumptionEligibilityArtifact",
    "ValidatedOperatorApprovalConsumptionEligibilityArtifactContent",
    "build_operator_approval_consumption_eligibility_artifact",
    "operator_approval_consumption_eligibility_artifact_hash_payload",
    "operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars",
    "validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed",
    "validate_operator_approval_consumption_eligibility_artifact_scalars_detailed",
]

OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION = 1

_ARTIFACT_MARKET = "KR"
_ARTIFACT_RUNTIME_ACTIVATION_OUTCOME = "no_go"

_HEX64 = re.compile(r"[0-9a-f]{64}")
_SYMBOL6 = re.compile(r"[0-9]{6}")

_REASON_NOT_ELIGIBLE = "approval_consumption_artifact_not_eligible"
_REASON_INVALID = "approval_consumption_artifact_invalid_input"

OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_FIELD_NAMES = frozenset(
    {
        "schema_version",
        "checked_at",
        "approval_intent_schema_version",
        "approval_intent_sha256",
        "candidate_evidence_schema_version",
        "candidate_evidence_sha256",
        "market",
        "symbol",
        "evidence_evaluated_at",
        "intent_declared_at",
        "activation_authorized",
        "runtime_activation_outcome",
        "eligibility_artifact_sha256",
    }
)

# Stable verifier reason codes (RTM-7c.4u) — one root cause maps to exactly one code.
_VR_NOT_OBJECT = "eligibility_artifact_not_object"
_VR_UNKNOWN_FIELD = "eligibility_artifact_unknown_field"
_VR_MISSING_FIELD = "eligibility_artifact_missing_field"
_VR_UNSUPPORTED_SCHEMA = "eligibility_artifact_unsupported_schema"
_VR_INVALID_FIELD = "eligibility_artifact_invalid_field"
_VR_INVALID_TIMESTAMP = "eligibility_artifact_invalid_timestamp"
_VR_INVALID_BINDING = "eligibility_artifact_invalid_binding"
_VR_INVALID_ACTIVATION_POSTURE = "eligibility_artifact_invalid_activation_posture"
_VR_INVALID_TIME_ORDERING = "eligibility_artifact_invalid_time_ordering"
_VR_HASH_MISMATCH = "eligibility_artifact_hash_mismatch"


class OperatorApprovalConsumptionEligibilityArtifactOutcome(StrEnum):
    CREATED = "created"
    NOT_ELIGIBLE = "not_eligible"
    INVALID = "invalid"


@dataclass(frozen=True)
class OperatorApprovalConsumptionEligibilityArtifact:
    """Canonical immutable eligibility observation artifact — consumption·persistence·authentication 아님."""

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
class OperatorApprovalConsumptionEligibilityArtifactResult:
    outcome: OperatorApprovalConsumptionEligibilityArtifactOutcome
    reasons: tuple[str, ...]
    artifact: OperatorApprovalConsumptionEligibilityArtifact | None


@dataclass(frozen=True)
class ValidatedOperatorApprovalConsumptionEligibilityArtifact:
    """검증된 eligibility-artifact scalar snapshot — builder/verifier 공유 semantic owner."""

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
class ValidatedOperatorApprovalConsumptionEligibilityArtifactContent:
    """검증된 12개 content scalar snapshot — stored digest 제외, builder/verifier 공유 owner."""

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


@dataclass(frozen=True)
class OperatorApprovalConsumptionEligibilityArtifactContentScalarValidation:
    """Content semantic validation owner 결과 — stable reason + validated content snapshot."""

    validated: ValidatedOperatorApprovalConsumptionEligibilityArtifactContent | None
    reason_code: str | None


@dataclass(frozen=True)
class OperatorApprovalConsumptionEligibilityArtifactScalarValidation:
    """Full scalar validation 결과 — content owner + stored digest를 합친 validated snapshot."""

    validated: ValidatedOperatorApprovalConsumptionEligibilityArtifact | None
    reason_code: str | None


def build_operator_approval_consumption_eligibility_artifact(
    eligibility_result: object,
) -> OperatorApprovalConsumptionEligibilityArtifactResult:
    """완료된 ELIGIBLE eligibility observation을 canonical immutable artifact로 동결한다.

    Approval을 소비하지 않으며 consumed marker, replay prevention, persistence, signing,
    authentication, TTL/freshness 재평가, activation authorization이 아니다. Upstream
    eligibility API/intent verifier/evidence validator를 재실행하지 않고 clock도 읽지 않는다."""

    try:
        return _build(eligibility_result)
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _invalid()


def _build(
    eligibility_result: object,
) -> OperatorApprovalConsumptionEligibilityArtifactResult:
    if type(eligibility_result) is not OperatorApprovalConsumptionEligibilityResult:
        return _invalid()

    outcome = eligibility_result.outcome
    reasons = eligibility_result.reasons
    eligibility = eligibility_result.eligibility

    if outcome is OperatorApprovalConsumptionEligibilityOutcome.NO_GO:
        if type(reasons) is tuple and len(reasons) >= 1 and eligibility is None:
            return _not_eligible()
        return _invalid()
    if outcome is not OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE:
        return _invalid()

    if type(reasons) is not tuple or reasons != ():
        return _invalid()
    if type(eligibility) is not OperatorApprovalConsumptionEligibility:
        return _invalid()

    approval_intent_sha256 = eligibility.approval_intent_sha256
    evidence_sha256 = eligibility.evidence_sha256
    market = eligibility.market
    symbol = eligibility.symbol
    evidence_evaluated_at = eligibility.evidence_evaluated_at
    intent_declared_at = eligibility.intent_declared_at
    checked_at = eligibility.checked_at
    activation_authorized = eligibility.activation_authorized
    runtime_activation_outcome = eligibility.runtime_activation_outcome

    content = validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed(
        schema_version=OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION,
        checked_at=checked_at,
        approval_intent_schema_version=OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
        approval_intent_sha256=approval_intent_sha256,
        candidate_evidence_schema_version=ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
        candidate_evidence_sha256=evidence_sha256,
        market=market,
        symbol=symbol,
        evidence_evaluated_at=evidence_evaluated_at,
        intent_declared_at=intent_declared_at,
        activation_authorized=activation_authorized,
        runtime_activation_outcome=runtime_activation_outcome,
    )
    if content.validated is None:
        return _invalid()

    # Hash and artifact are emitted from the validated content snapshot only — never the raw
    # caller locals — so validation, hashing, and construction observe one identical source.
    v = content.validated
    hash_payload = operator_approval_consumption_eligibility_artifact_hash_payload(
        checked_at=v.checked_at,
        approval_intent_sha256=v.approval_intent_sha256,
        candidate_evidence_sha256=v.candidate_evidence_sha256,
        market=v.market,
        symbol=v.symbol,
        evidence_evaluated_at=v.evidence_evaluated_at,
        intent_declared_at=v.intent_declared_at,
    )
    eligibility_artifact_sha256 = payload_sha256(hash_payload)

    artifact = OperatorApprovalConsumptionEligibilityArtifact(
        schema_version=v.schema_version,
        checked_at=v.checked_at,
        approval_intent_schema_version=v.approval_intent_schema_version,
        approval_intent_sha256=v.approval_intent_sha256,
        candidate_evidence_schema_version=v.candidate_evidence_schema_version,
        candidate_evidence_sha256=v.candidate_evidence_sha256,
        market=v.market,
        symbol=v.symbol,
        evidence_evaluated_at=v.evidence_evaluated_at,
        intent_declared_at=v.intent_declared_at,
        activation_authorized=v.activation_authorized,
        runtime_activation_outcome=v.runtime_activation_outcome,
        eligibility_artifact_sha256=eligibility_artifact_sha256,
    )
    return OperatorApprovalConsumptionEligibilityArtifactResult(
        outcome=OperatorApprovalConsumptionEligibilityArtifactOutcome.CREATED,
        reasons=(),
        artifact=artifact,
    )


def operator_approval_consumption_eligibility_artifact_hash_payload(
    *,
    checked_at: str,
    approval_intent_sha256: str,
    candidate_evidence_sha256: str,
    market: str,
    symbol: str,
    evidence_evaluated_at: str,
    intent_declared_at: str,
) -> dict[str, object]:
    """Canonical hash payload — every artifact field except ``eligibility_artifact_sha256``.

    Builder convenience over the canonical constants. The actual canonical owner is
    :func:`operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars`."""

    return operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars(
        schema_version=OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION,
        checked_at=checked_at,
        approval_intent_schema_version=OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
        approval_intent_sha256=approval_intent_sha256,
        candidate_evidence_schema_version=ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
        candidate_evidence_sha256=candidate_evidence_sha256,
        market=market,
        symbol=symbol,
        evidence_evaluated_at=evidence_evaluated_at,
        intent_declared_at=intent_declared_at,
        activation_authorized=False,
        runtime_activation_outcome=_ARTIFACT_RUNTIME_ACTIVATION_OUTCOME,
    )


def operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars(
    *,
    schema_version: int,
    checked_at: str,
    approval_intent_schema_version: int,
    approval_intent_sha256: str,
    candidate_evidence_schema_version: int,
    candidate_evidence_sha256: str,
    market: str,
    symbol: str,
    evidence_evaluated_at: str,
    intent_declared_at: str,
    activation_authorized: bool,
    runtime_activation_outcome: str,
) -> dict[str, object]:
    """Canonical hash payload over the 12 serialized content fields using the passed values.

    The verifier recomputes the digest from the *input payload* values (including the semantic
    constants), so a tampered serialized field changes the digest even when the constants would
    otherwise be auto-inserted."""

    return {
        "schema_version": schema_version,
        "checked_at": checked_at,
        "approval_intent_schema_version": approval_intent_schema_version,
        "approval_intent_sha256": approval_intent_sha256,
        "candidate_evidence_schema_version": candidate_evidence_schema_version,
        "candidate_evidence_sha256": candidate_evidence_sha256,
        "market": market,
        "symbol": symbol,
        "evidence_evaluated_at": evidence_evaluated_at,
        "intent_declared_at": intent_declared_at,
        "activation_authorized": activation_authorized,
        "runtime_activation_outcome": runtime_activation_outcome,
    }


def validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed(
    *,
    schema_version: object,
    checked_at: object,
    approval_intent_schema_version: object,
    approval_intent_sha256: object,
    candidate_evidence_schema_version: object,
    candidate_evidence_sha256: object,
    market: object,
    symbol: object,
    evidence_evaluated_at: object,
    intent_declared_at: object,
    activation_authorized: object,
    runtime_activation_outcome: object,
) -> OperatorApprovalConsumptionEligibilityArtifactContentScalarValidation:
    """12개 content scalar의 stable-reason 분류 + semantic + aware-timestamp + ordering 검증.

    Builder와 verifier가 공유하는 단일 content semantic owner다 (stored digest는 제외).
    schema/intent·evidence binding/digest shape/KR·ASCII symbol/timestamp/posture/ordering을
    이 helper 단독이 소유한다."""

    if type(schema_version) is not int or isinstance(schema_version, bool):
        return _content_invalid(_VR_INVALID_FIELD)
    if schema_version != OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION:
        return _content_invalid(_VR_UNSUPPORTED_SCHEMA)

    if type(approval_intent_schema_version) is not int or isinstance(
        approval_intent_schema_version, bool
    ):
        return _content_invalid(_VR_INVALID_BINDING)
    if approval_intent_schema_version != OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION:
        return _content_invalid(_VR_INVALID_BINDING)
    if not _is_lower_hex64(approval_intent_sha256):
        return _content_invalid(_VR_INVALID_BINDING)
    if type(candidate_evidence_schema_version) is not int or isinstance(
        candidate_evidence_schema_version, bool
    ):
        return _content_invalid(_VR_INVALID_BINDING)
    if candidate_evidence_schema_version != ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION:
        return _content_invalid(_VR_INVALID_BINDING)
    if not _is_lower_hex64(candidate_evidence_sha256):
        return _content_invalid(_VR_INVALID_BINDING)

    if type(market) is not str or market != _ARTIFACT_MARKET:
        return _content_invalid(_VR_INVALID_FIELD)
    if type(symbol) is not str or _SYMBOL6.fullmatch(symbol) is None:
        return _content_invalid(_VR_INVALID_FIELD)

    checked_parsed = _parse_aware(checked_at)
    if checked_parsed is None:
        return _content_invalid(_VR_INVALID_TIMESTAMP)
    evidence_parsed = _parse_aware(evidence_evaluated_at)
    if evidence_parsed is None:
        return _content_invalid(_VR_INVALID_TIMESTAMP)
    intent_parsed = _parse_aware(intent_declared_at)
    if intent_parsed is None:
        return _content_invalid(_VR_INVALID_TIMESTAMP)

    if activation_authorized is not False:
        return _content_invalid(_VR_INVALID_ACTIVATION_POSTURE)
    if (
        type(runtime_activation_outcome) is not str
        or runtime_activation_outcome != _ARTIFACT_RUNTIME_ACTIVATION_OUTCOME
    ):
        return _content_invalid(_VR_INVALID_ACTIVATION_POSTURE)

    if not (evidence_parsed <= intent_parsed <= checked_parsed):
        return _content_invalid(_VR_INVALID_TIME_ORDERING)

    return OperatorApprovalConsumptionEligibilityArtifactContentScalarValidation(
        validated=ValidatedOperatorApprovalConsumptionEligibilityArtifactContent(
            schema_version=schema_version,
            checked_at=checked_at,
            approval_intent_schema_version=approval_intent_schema_version,
            approval_intent_sha256=approval_intent_sha256,
            candidate_evidence_schema_version=candidate_evidence_schema_version,
            candidate_evidence_sha256=candidate_evidence_sha256,
            market=market,
            symbol=symbol,
            evidence_evaluated_at=evidence_evaluated_at,
            intent_declared_at=intent_declared_at,
            activation_authorized=False,
            runtime_activation_outcome=runtime_activation_outcome,
        ),
        reason_code=None,
    )


def validate_operator_approval_consumption_eligibility_artifact_scalars_detailed(
    *,
    schema_version: object,
    checked_at: object,
    approval_intent_schema_version: object,
    approval_intent_sha256: object,
    candidate_evidence_schema_version: object,
    candidate_evidence_sha256: object,
    market: object,
    symbol: object,
    evidence_evaluated_at: object,
    intent_declared_at: object,
    activation_authorized: object,
    runtime_activation_outcome: object,
    eligibility_artifact_sha256: object,
) -> OperatorApprovalConsumptionEligibilityArtifactScalarValidation:
    """Full 13-field validation: shared content owner 정확히 1회 + stored digest shape.

    Verifier core가 정확히 1회 호출한다. Hash 재계산만 호출자 책임으로 남긴다."""

    content = validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed(
        schema_version=schema_version,
        checked_at=checked_at,
        approval_intent_schema_version=approval_intent_schema_version,
        approval_intent_sha256=approval_intent_sha256,
        candidate_evidence_schema_version=candidate_evidence_schema_version,
        candidate_evidence_sha256=candidate_evidence_sha256,
        market=market,
        symbol=symbol,
        evidence_evaluated_at=evidence_evaluated_at,
        intent_declared_at=intent_declared_at,
        activation_authorized=activation_authorized,
        runtime_activation_outcome=runtime_activation_outcome,
    )
    if content.validated is None:
        return _scalar_invalid(content.reason_code or _VR_INVALID_FIELD)

    if not _is_lower_hex64(eligibility_artifact_sha256):
        return _scalar_invalid(_VR_INVALID_FIELD)

    v = content.validated
    return OperatorApprovalConsumptionEligibilityArtifactScalarValidation(
        validated=ValidatedOperatorApprovalConsumptionEligibilityArtifact(
            schema_version=v.schema_version,
            checked_at=v.checked_at,
            approval_intent_schema_version=v.approval_intent_schema_version,
            approval_intent_sha256=v.approval_intent_sha256,
            candidate_evidence_schema_version=v.candidate_evidence_schema_version,
            candidate_evidence_sha256=v.candidate_evidence_sha256,
            market=v.market,
            symbol=v.symbol,
            evidence_evaluated_at=v.evidence_evaluated_at,
            intent_declared_at=v.intent_declared_at,
            activation_authorized=v.activation_authorized,
            runtime_activation_outcome=v.runtime_activation_outcome,
            eligibility_artifact_sha256=eligibility_artifact_sha256,
        ),
        reason_code=None,
    )


def _content_invalid(
    reason: str,
) -> OperatorApprovalConsumptionEligibilityArtifactContentScalarValidation:
    return OperatorApprovalConsumptionEligibilityArtifactContentScalarValidation(
        validated=None, reason_code=reason
    )


def _scalar_invalid(
    reason: str,
) -> OperatorApprovalConsumptionEligibilityArtifactScalarValidation:
    return OperatorApprovalConsumptionEligibilityArtifactScalarValidation(
        validated=None, reason_code=reason
    )


def _parse_aware(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if type(parsed) is not datetime:
        return None
    try:
        offset = parsed.utcoffset()
    except MemoryError:
        raise
    except Exception:
        return None
    if offset is None:
        return None
    return parsed


def _is_lower_hex64(value: object) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _invalid() -> OperatorApprovalConsumptionEligibilityArtifactResult:
    return OperatorApprovalConsumptionEligibilityArtifactResult(
        outcome=OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID,
        reasons=(_REASON_INVALID,),
        artifact=None,
    )


def _not_eligible() -> OperatorApprovalConsumptionEligibilityArtifactResult:
    return OperatorApprovalConsumptionEligibilityArtifactResult(
        outcome=OperatorApprovalConsumptionEligibilityArtifactOutcome.NOT_ELIGIBLE,
        reasons=(_REASON_NOT_ELIGIBLE,),
        artifact=None,
    )
