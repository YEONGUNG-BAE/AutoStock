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
    "OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION",
    "OperatorApprovalConsumptionEligibilityArtifact",
    "OperatorApprovalConsumptionEligibilityArtifactOutcome",
    "OperatorApprovalConsumptionEligibilityArtifactResult",
    "build_operator_approval_consumption_eligibility_artifact",
    "operator_approval_consumption_eligibility_artifact_hash_payload",
]

OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION = 1

_ARTIFACT_MARKET = "KR"
_ARTIFACT_RUNTIME_ACTIVATION_OUTCOME = "no_go"

_HEX64 = re.compile(r"[0-9a-f]{64}")
_SYMBOL6 = re.compile(r"[0-9]{6}")

_REASON_NOT_ELIGIBLE = "approval_consumption_artifact_not_eligible"
_REASON_INVALID = "approval_consumption_artifact_invalid_input"


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

    if not _is_lower_hex64(approval_intent_sha256):
        return _invalid()
    if not _is_lower_hex64(evidence_sha256):
        return _invalid()
    if type(market) is not str or market != _ARTIFACT_MARKET:
        return _invalid()
    if type(symbol) is not str or _SYMBOL6.fullmatch(symbol) is None:
        return _invalid()
    if activation_authorized is not False:
        return _invalid()
    if (
        type(runtime_activation_outcome) is not str
        or runtime_activation_outcome != _ARTIFACT_RUNTIME_ACTIVATION_OUTCOME
    ):
        return _invalid()

    evidence_parsed = _parse_aware(evidence_evaluated_at)
    intent_parsed = _parse_aware(intent_declared_at)
    checked_parsed = _parse_aware(checked_at)
    if evidence_parsed is None or intent_parsed is None or checked_parsed is None:
        return _invalid()
    if not (evidence_parsed <= intent_parsed <= checked_parsed):
        return _invalid()

    hash_payload = operator_approval_consumption_eligibility_artifact_hash_payload(
        checked_at=checked_at,
        approval_intent_sha256=approval_intent_sha256,
        candidate_evidence_sha256=evidence_sha256,
        market=market,
        symbol=symbol,
        evidence_evaluated_at=evidence_evaluated_at,
        intent_declared_at=intent_declared_at,
    )
    eligibility_artifact_sha256 = payload_sha256(hash_payload)

    artifact = OperatorApprovalConsumptionEligibilityArtifact(
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
        activation_authorized=False,
        runtime_activation_outcome=_ARTIFACT_RUNTIME_ACTIVATION_OUTCOME,
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
    """Canonical hash payload — every artifact field except ``eligibility_artifact_sha256``."""

    return {
        "schema_version": OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION,
        "checked_at": checked_at,
        "approval_intent_schema_version": OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
        "approval_intent_sha256": approval_intent_sha256,
        "candidate_evidence_schema_version": ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
        "candidate_evidence_sha256": candidate_evidence_sha256,
        "market": market,
        "symbol": symbol,
        "evidence_evaluated_at": evidence_evaluated_at,
        "intent_declared_at": intent_declared_at,
        "activation_authorized": False,
        "runtime_activation_outcome": _ARTIFACT_RUNTIME_ACTIVATION_OUTCOME,
    }


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
