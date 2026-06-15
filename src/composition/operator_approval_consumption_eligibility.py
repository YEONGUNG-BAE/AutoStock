"""Operator approval consumption eligibility preflight (RTM-7c.4s).

Pure API that judges whether a verified approval intent and matching candidate evidence
**could** be combined as consumption candidates. Does **not** consume approval, persist state,
authenticate Operator identity, or authorize runtime activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from composition.activation_candidate_evidence import (
    ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
    ActivationCandidateEvidence,
    ValidatedActivationCandidateEvidence,
    validate_activation_candidate_evidence_scalars,
)
from composition.operator_approval_intent import _parse_aware
from composition.operator_approval_intent_verifier import (
    OperatorApprovalIntentVerificationOutcome,
    VerifiedOperatorApprovalIntent,
    verify_and_snapshot_operator_approval_intent,
)

__all__ = [
    "OperatorApprovalConsumptionEligibility",
    "OperatorApprovalConsumptionEligibilityOutcome",
    "OperatorApprovalConsumptionEligibilityResult",
    "assess_operator_approval_consumption_eligibility",
]


class OperatorApprovalConsumptionEligibilityOutcome(StrEnum):
    ELIGIBLE = "eligible"
    NO_GO = "no_go"
    INVALID = "invalid"


@dataclass(frozen=True)
class OperatorApprovalConsumptionEligibility:
    """Immutable eligibility observation — consumption·persistence·authentication 아님."""

    approval_intent_sha256: str
    evidence_sha256: str
    market: str
    symbol: str
    evidence_evaluated_at: str
    intent_declared_at: str
    checked_at: str
    activation_authorized: bool
    runtime_activation_outcome: str


@dataclass(frozen=True)
class OperatorApprovalConsumptionEligibilityResult:
    outcome: OperatorApprovalConsumptionEligibilityOutcome
    reasons: tuple[str, ...]
    eligibility: OperatorApprovalConsumptionEligibility | None


def assess_operator_approval_consumption_eligibility(
    *,
    intent_payload: object,
    evidence: object,
    now: object,
) -> OperatorApprovalConsumptionEligibilityResult:
    """Verified intent + evidence가 소비 후보로 결합 가능한지 pure preflight로 판단한다.

    Approval을 소비하지 않는다. Clock read, config/env, filesystem, network, broker/order,
    persistence, consumption marker, replay protection 없음."""

    now_snapshot = _snapshot_consumption_checked_at(now)
    if now_snapshot is None:
        return _invalid("approval_consumption_invalid_now")

    checked_at_iso, now_parsed = now_snapshot

    intent_result = verify_and_snapshot_operator_approval_intent(intent_payload)
    if intent_result.outcome is not OperatorApprovalIntentVerificationOutcome.VALID:
        return _invalid("approval_consumption_intent_invalid")
    assert intent_result.snapshot is not None
    intent_snapshot = intent_result.snapshot

    validated_evidence = _validate_evidence_snapshot(evidence)
    if validated_evidence is None:
        return _invalid("approval_consumption_evidence_invalid")

    if not _binding_matches(intent_snapshot, validated_evidence):
        return _no_go("approval_consumption_evidence_mismatch")

    time_reason = _time_ordering_reason(
        evidence_evaluated_at=validated_evidence.evaluated_at,
        intent_declared_at=intent_snapshot.declared_at,
        now_parsed=now_parsed,
    )
    if time_reason is not None:
        return _no_go(time_reason)

    return OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE,
        reasons=(),
        eligibility=OperatorApprovalConsumptionEligibility(
            approval_intent_sha256=intent_snapshot.approval_intent_sha256,
            evidence_sha256=validated_evidence.evidence_sha256,
            market=validated_evidence.market,
            symbol=validated_evidence.symbol,
            evidence_evaluated_at=validated_evidence.evaluated_at,
            intent_declared_at=intent_snapshot.declared_at,
            checked_at=checked_at_iso,
            activation_authorized=False,
            runtime_activation_outcome="no_go",
        ),
    )


def _snapshot_consumption_checked_at(value: object) -> tuple[str, datetime] | None:
    """Caller ``now``를 detached single observation으로 동결한다.

    Exact built-in timezone-aware ``datetime``만 허용. ``isoformat()`` 정확히 1회 후
    ``fromisoformat()`` 재파싱 — caller datetime/tzinfo 재접근 금지."""

    if type(value) is not datetime:
        return None
    try:
        canonical_iso = value.isoformat()
    except MemoryError:
        raise
    except Exception:
        return None
    try:
        parsed = datetime.fromisoformat(canonical_iso)
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
    return (canonical_iso, parsed)


def _validate_evidence_snapshot(
    evidence: object,
) -> ValidatedActivationCandidateEvidence | None:
    """Exact ``ActivationCandidateEvidence`` scalar snapshot + semantic/hash 검증 1회."""

    if type(evidence) is not ActivationCandidateEvidence:
        return None
    try:
        return validate_activation_candidate_evidence_scalars(
            schema_version=evidence.schema_version,
            evaluated_at=evidence.evaluated_at,
            receipt_sha256=evidence.receipt_sha256,
            fresh_precheck_receipt_sha256=evidence.fresh_precheck_receipt_sha256,
            market=evidence.market,
            symbol=evidence.symbol,
            max_age_microseconds=evidence.max_age_microseconds,
            receipt_age_microseconds=evidence.receipt_age_microseconds,
            final_preflight_outcome=evidence.final_preflight_outcome,
            freshness_outcome=evidence.freshness_outcome,
            fresh_precheck_executed=evidence.fresh_precheck_executed,
            receipt_age_evaluated=evidence.receipt_age_evaluated,
            freshness_policy_evaluated=evidence.freshness_policy_evaluated,
            activation_authorized=evidence.activation_authorized,
            runtime_activation_outcome=evidence.runtime_activation_outcome,
            evidence_sha256=evidence.evidence_sha256,
        )
    except AttributeError:
        return None


def _binding_matches(
    intent: VerifiedOperatorApprovalIntent,
    evidence: ValidatedActivationCandidateEvidence,
) -> bool:
    """Intent ↔ evidence digest/identity binding — exact built-in scalar equality only."""

    if type(intent.evidence_schema_version) is not int:
        return False
    if intent.evidence_schema_version != ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION:
        return False
    if type(evidence.schema_version) is not int:
        return False
    if evidence.schema_version != ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION:
        return False
    if type(intent.evidence_sha256) is not str or type(evidence.evidence_sha256) is not str:
        return False
    if intent.evidence_sha256 != evidence.evidence_sha256:
        return False
    if type(intent.market) is not str or type(evidence.market) is not str:
        return False
    if intent.market != evidence.market:
        return False
    if type(intent.symbol) is not str or type(evidence.symbol) is not str:
        return False
    if intent.symbol != evidence.symbol:
        return False
    return True


def _time_ordering_reason(
    *,
    evidence_evaluated_at: str,
    intent_declared_at: str,
    now_parsed: datetime,
) -> str | None:
    """Validated parsed datetimes만 사용 — evidence <= intent <= now."""

    evidence_parsed = _parse_aware(evidence_evaluated_at)
    intent_parsed = _parse_aware(intent_declared_at)
    if evidence_parsed is None or intent_parsed is None:
        return "approval_consumption_intent_precedes_evidence"

    if intent_parsed < evidence_parsed:
        return "approval_consumption_intent_precedes_evidence"
    if intent_parsed > now_parsed:
        return "approval_consumption_intent_in_future"
    return None


def _invalid(reason: str) -> OperatorApprovalConsumptionEligibilityResult:
    return OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.INVALID,
        reasons=(reason,),
        eligibility=None,
    )


def _no_go(reason: str) -> OperatorApprovalConsumptionEligibilityResult:
    return OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.NO_GO,
        reasons=(reason,),
        eligibility=None,
    )
