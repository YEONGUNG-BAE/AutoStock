"""Canonical freshness-qualified candidate evidence (RTM-7c.4n).

Freezes a freshness-qualified mechanical PASS into a single immutable canonical evidence
payload that a *future* Operator-approval stage can reference. The digest binds one verified
receipt, one explicit max-age policy, one caller time, and one final-preflight/freshness
result together.

Evidence does **not** mean: receipt authenticity, signing/HMAC, Operator approval,
writer-stop proof, an activation token, or runtime activation authorization. The activation
posture is a constant NO-GO on every code path.

Evidence is produced **only** for a freshness-qualified ``PASS`` whose final preflight is
``PASS`` and whose freshness evaluation is ``FRESH``. ``NO_GO``/``STALE`` produce no digest
(``evidence is None``) — there is no generic failure evidence.

No clock read of its own (the caller passes the same ``now`` used by the qualified call), no
network/credential/broker access, no operational DB write, no filesystem/evidence-file write,
and no persistence. Raw receipt payload, artifact/config paths, fingerprint bodies, and
secret/env data are never stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from composition.activation_candidate_final_preflight import (
    ActivationCandidateFinalPreflightOutcome,
    ActivationCandidateFinalPreflightResult,
    final_preflight_now_is_invalid,
)
from composition.activation_candidate_freshness_preflight import (
    ActivationCandidateFreshnessPreflightOutcome,
    ActivationCandidateFreshnessPreflightResult,
    freshness_qualify_activation_candidate,
)
from composition.receipt_freshness_policy import (
    ReceiptFreshnessEvaluation,
    ReceiptFreshnessOutcome,
    ReceiptFreshnessPolicy,
)
from composition.receipt_time_assessment import (
    ReceiptTimeAssessment,
    ReceiptTimeAssessmentOutcome,
)
from config.settings import RuntimePaperFastLoopSettings
from decision.canonical_json import payload_sha256

__all__ = [
    "ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION",
    "ActivationCandidateEvidence",
    "ActivationCandidateEvidenceOutcome",
    "ActivationCandidateEvidenceResult",
    "FreshnessQualifiedEvidenceOutcome",
    "FreshnessQualifiedEvidenceResult",
    "build_activation_candidate_evidence",
    "freshness_qualify_and_build_candidate_evidence",
]

ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION = 1

_HEX64 = re.compile(r"[0-9a-f]{64}")


class ActivationCandidateEvidenceOutcome(StrEnum):
    CREATED = "created"
    NOT_ELIGIBLE = "not_eligible"
    INVALID = "invalid"


@dataclass(frozen=True)
class ActivationCandidateEvidence:
    """Immutable canonical evidence digest — not approval, signature, or activation.

    ``evidence_sha256`` is the canonical sha256 over every other field (the 14 hash-payload
    fields in :func:`_evidence_hash_payload`), so an independent party can recompute it from
    the same scalars. No raw receipt, path, fingerprint body, or secret is retained."""

    schema_version: int
    evaluated_at: str
    receipt_sha256: str
    market: str
    symbol: str
    max_age_microseconds: int
    receipt_age_microseconds: int
    final_preflight_outcome: str
    freshness_outcome: str
    fresh_precheck_executed: bool
    receipt_age_evaluated: bool
    freshness_policy_evaluated: bool
    activation_authorized: bool
    runtime_activation_outcome: str
    evidence_sha256: str


@dataclass(frozen=True)
class ActivationCandidateEvidenceResult:
    outcome: ActivationCandidateEvidenceOutcome
    reasons: tuple[str, ...]
    evidence: ActivationCandidateEvidence | None


class FreshnessQualifiedEvidenceOutcome(StrEnum):
    PASS = "pass"
    NO_GO = "no_go"


@dataclass(frozen=True)
class FreshnessQualifiedEvidenceResult:
    """Combined freshness-qualified + evidence verdict — fail-closed.

    ``outcome`` is ``PASS`` **only** when the qualified verdict is ``PASS`` *and* canonical
    evidence was ``CREATED``. A qualified ``NO_GO`` is ``NO_GO`` with the qualified reasons and
    ``evidence_result is None`` (the builder is not invoked). A qualified ``PASS`` whose
    evidence is not ``CREATED`` (``INVALID``/``NOT_ELIGIBLE``/``None``) is ``NO_GO`` with the
    single stable reason ``candidate_evidence_generation_invalid`` — a qualified PASS alone
    must never be reported as a combined PASS."""

    outcome: FreshnessQualifiedEvidenceOutcome
    reasons: tuple[str, ...]
    qualified_result: ActivationCandidateFreshnessPreflightResult
    evidence_result: ActivationCandidateEvidenceResult | None


def build_activation_candidate_evidence(
    *,
    qualified_result: ActivationCandidateFreshnessPreflightResult,
    evaluated_at: datetime,
) -> ActivationCandidateEvidenceResult:
    """Build canonical evidence from a freshness-qualified PASS result.

    ``evaluated_at`` must be the same timezone-aware ``now`` passed to the qualified call —
    this builder reads no clock. Processing: exact result-type guard → ``evaluated_at`` guard
    → eligibility (PASS/FRESH + posture/shape invariants) → canonical digest. A wrong-object
    or malformed result, an invalid ``evaluated_at``, or a contradictory PASS result fails
    closed as ``INVALID``; a well-formed non-PASS result is ``NOT_ELIGIBLE``."""

    if type(qualified_result) is not ActivationCandidateFreshnessPreflightResult:
        return _invalid()

    if final_preflight_now_is_invalid(evaluated_at):
        return _invalid()

    # Single-read snapshot of the outer qualified result into locals.
    try:
        outcome = qualified_result.outcome
        reasons = qualified_result.reasons
        final_result = qualified_result.final_preflight_result
        freshness_eval = qualified_result.freshness_evaluation
        policy_evaluated = qualified_result.freshness_policy_evaluated
        receipt_sha256 = qualified_result.receipt_sha256
        market = qualified_result.market
        symbol = qualified_result.symbol
        activation_authorized = qualified_result.activation_authorized
        runtime_activation_outcome = qualified_result.runtime_activation_outcome
        approval_required = qualified_result.explicit_operator_approval_required
        writers_required = qualified_result.writers_stopped_manual_confirmation_required
    except AttributeError:
        return _invalid()

    # A well-formed non-PASS verdict (NO_GO/STALE/final NO_GO) is simply not eligible.
    if outcome is not ActivationCandidateFreshnessPreflightOutcome.PASS:
        return _not_eligible()

    # From here the result claims PASS — any broken invariant is a contradictory/synthetic
    # result and fails closed as INVALID (never silently produces a digest).
    if reasons != ():
        return _invalid()
    if type(policy_evaluated) is not bool or policy_evaluated is not True:
        return _invalid()
    if not _posture_ok(activation_authorized, runtime_activation_outcome):
        return _invalid()
    if approval_required is not True or writers_required is not True:
        return _invalid()

    # Nested results must be the exact built-in result types — subclass / arbitrary object
    # fails closed before any field is trusted.
    if (
        type(final_result) is not ActivationCandidateFinalPreflightResult
        or type(freshness_eval) is not ReceiptFreshnessEvaluation
    ):
        return _invalid()

    # Single-read snapshot of each caller-owned nested object into locals; the same locals
    # feed validation and the hash payload (the caller objects are not re-read afterwards).
    try:
        final_outcome = final_result.outcome
        final_reasons = final_result.reasons
        final_sha = final_result.receipt_sha256
        final_market = final_result.market
        final_symbol = final_result.symbol
        fresh_precheck_executed = final_result.fresh_precheck_executed
        receipt_age_evaluated = final_result.receipt_age_evaluated
        final_age = final_result.receipt_age_microseconds
        time_assessment = final_result.receipt_time_assessment
        final_policy_evaluated = final_result.freshness_policy_evaluated
        final_activation = final_result.activation_authorized
        final_runtime = final_result.runtime_activation_outcome
        final_approval_required = final_result.explicit_operator_approval_required
        final_writers_required = final_result.writers_stopped_manual_confirmation_required

        fresh_outcome = freshness_eval.outcome
        fresh_reasons = freshness_eval.reasons
        fresh_policy_evaluated = freshness_eval.freshness_policy_evaluated
        receipt_age = freshness_eval.receipt_age_microseconds
        max_age = freshness_eval.max_age_microseconds
        fresh_activation = freshness_eval.activation_authorized
        fresh_runtime = freshness_eval.runtime_activation_outcome
    except AttributeError:
        return _invalid()

    if type(time_assessment) is not ReceiptTimeAssessment:
        return _invalid()
    try:
        ta_outcome = time_assessment.outcome
        ta_reasons = time_assessment.reasons
        ta_checked_at = time_assessment.receipt_checked_at
        ta_age = time_assessment.receipt_age_microseconds
        ta_age_evaluated = time_assessment.receipt_age_evaluated
        ta_policy_evaluated = time_assessment.freshness_policy_evaluated
    except AttributeError:
        return _invalid()

    # Final preflight is policy-neutral; the freshness result is the explicit FRESH verdict.
    # A final PASS that still carries failure reasons is a contradictory result (the PASS and
    # the reasons cannot both hold) — reasons must be the empty tuple. A nonempty tuple, list,
    # None, or arbitrary object all differ from ``()`` and fail closed; the raw reason content
    # is never surfaced in the stable evidence reason.
    if final_outcome is not ActivationCandidateFinalPreflightOutcome.PASS:
        return _invalid()
    if final_reasons != ():
        return _invalid()
    if final_policy_evaluated is not False:
        return _invalid()
    if fresh_outcome is not ReceiptFreshnessOutcome.FRESH:
        return _invalid()
    if fresh_policy_evaluated is not True or fresh_reasons != ():
        return _invalid()

    # Nested posture must match the constant NO-GO posture across every stage.
    if not _posture_ok(final_activation, final_runtime):
        return _invalid()
    if not _posture_ok(fresh_activation, fresh_runtime):
        return _invalid()
    if final_approval_required is not True or final_writers_required is not True:
        return _invalid()

    # The fresh precheck and age observation must actually have run, and the time assessment
    # must be a clean VALID observation.
    if fresh_precheck_executed is not True or receipt_age_evaluated is not True:
        return _invalid()
    if ta_outcome is not ReceiptTimeAssessmentOutcome.VALID or ta_reasons != ():
        return _invalid()
    if ta_age_evaluated is not True:
        return _invalid()
    # The time assessment lane is policy-neutral by construction; it observes age but never
    # evaluates a freshness policy. A True/0/1/None/string/deleted flag means the nested
    # semantic roles overlap (the policy verdict belongs only to the freshness evaluation) and
    # fails closed.
    if ta_policy_evaluated is not False:
        return _invalid()

    # Identity must agree between the outer qualified result and the final preflight.
    if final_sha != receipt_sha256 or final_market != market or final_symbol != symbol:
        return _invalid()

    if not _is_lower_hex64(receipt_sha256):
        return _invalid()
    if not _is_nonempty_str(market) or not _is_nonempty_str(symbol):
        return _invalid()
    if (
        not _is_nonnegative_int(receipt_age)
        or not _is_nonnegative_int(max_age)
        or not _is_nonnegative_int(final_age)
        or not _is_nonnegative_int(ta_age)
    ):
        return _invalid()

    # One agreed receipt age across the final preflight, freshness, and time assessment.
    if final_age != receipt_age or ta_age != receipt_age:
        return _invalid()
    if receipt_age > max_age:
        return _invalid()

    # evaluated_at must be the same instant the age was observed from: the exact integer
    # microseconds between the verified receipt ``checked_at`` and ``evaluated_at`` equals the
    # agreed age (no float ``total_seconds``; a malformed/naive checked_at or an evaluated_at
    # before the receipt fails closed).
    checked_at = _parse_aware(ta_checked_at)
    if checked_at is None:
        return _invalid()
    delta = evaluated_at - checked_at
    age_us = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    if age_us != receipt_age:
        return _invalid()

    evaluated_at_iso = evaluated_at.isoformat()
    hash_payload = _evidence_hash_payload(
        evaluated_at=evaluated_at_iso,
        receipt_sha256=receipt_sha256,
        market=market,
        symbol=symbol,
        max_age_microseconds=max_age,
        receipt_age_microseconds=receipt_age,
        final_preflight_outcome=final_outcome.value,
        freshness_outcome=fresh_outcome.value,
        fresh_precheck_executed=fresh_precheck_executed,
        receipt_age_evaluated=receipt_age_evaluated,
        freshness_policy_evaluated=True,
    )
    evidence_sha256 = payload_sha256(hash_payload)

    evidence = ActivationCandidateEvidence(
        schema_version=ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
        evaluated_at=evaluated_at_iso,
        receipt_sha256=receipt_sha256,
        market=market,
        symbol=symbol,
        max_age_microseconds=max_age,
        receipt_age_microseconds=receipt_age,
        final_preflight_outcome=final_outcome.value,
        freshness_outcome=fresh_outcome.value,
        fresh_precheck_executed=fresh_precheck_executed,
        receipt_age_evaluated=receipt_age_evaluated,
        freshness_policy_evaluated=True,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        evidence_sha256=evidence_sha256,
    )
    return ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED,
        reasons=(),
        evidence=evidence,
    )


def freshness_qualify_and_build_candidate_evidence(
    *,
    settings: RuntimePaperFastLoopSettings,
    receipt_payload: object,
    now: datetime,
    policy: ReceiptFreshnessPolicy,
    base_dir: str | Path | None = None,
) -> FreshnessQualifiedEvidenceResult:
    """Run freshness-qualified preflight then build evidence on PASS only.

    The same ``now`` feeds the qualified call and the evidence ``evaluated_at`` — no extra
    clock read, verifier call, snapshot build, fresh precheck, or freshness evaluation. A
    qualified ``NO_GO`` skips the builder entirely (no evidence)."""

    qualified = freshness_qualify_activation_candidate(
        settings=settings,
        receipt_payload=receipt_payload,
        now=now,
        policy=policy,
        base_dir=base_dir,
    )
    # Qualified NO_GO: the builder is not invoked (no upstream stage reruns); combined NO_GO
    # preserves the existing qualified reasons verbatim.
    if qualified.outcome is not ActivationCandidateFreshnessPreflightOutcome.PASS:
        return FreshnessQualifiedEvidenceResult(
            outcome=FreshnessQualifiedEvidenceOutcome.NO_GO,
            reasons=qualified.reasons,
            qualified_result=qualified,
            evidence_result=None,
        )

    evidence_result = build_activation_candidate_evidence(
        qualified_result=qualified, evaluated_at=now
    )
    # Combined PASS requires evidence CREATED. A qualified PASS whose evidence is not created
    # fails closed to combined NO_GO — a qualified PASS alone is never a combined PASS.
    if (
        evidence_result.outcome is ActivationCandidateEvidenceOutcome.CREATED
        and evidence_result.evidence is not None
    ):
        return FreshnessQualifiedEvidenceResult(
            outcome=FreshnessQualifiedEvidenceOutcome.PASS,
            reasons=(),
            qualified_result=qualified,
            evidence_result=evidence_result,
        )
    return FreshnessQualifiedEvidenceResult(
        outcome=FreshnessQualifiedEvidenceOutcome.NO_GO,
        reasons=("candidate_evidence_generation_invalid",),
        qualified_result=qualified,
        evidence_result=evidence_result,
    )


def _evidence_hash_payload(
    *,
    evaluated_at: str,
    receipt_sha256: str,
    market: str,
    symbol: str,
    max_age_microseconds: int,
    receipt_age_microseconds: int,
    final_preflight_outcome: str,
    freshness_outcome: str,
    fresh_precheck_executed: bool,
    receipt_age_evaluated: bool,
    freshness_policy_evaluated: bool,
) -> dict[str, object]:
    """Canonical hash payload — every evidence field except ``evidence_sha256``.

    ``activation_authorized``/``runtime_activation_outcome`` are constant NO-GO; they are part
    of the digest so a tampered posture changes the hash. Canonical JSON sorts keys, so an
    independent recomputation from ``asdict(evidence)`` minus ``evidence_sha256`` matches."""

    return {
        "schema_version": ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
        "evaluated_at": evaluated_at,
        "receipt_sha256": receipt_sha256,
        "market": market,
        "symbol": symbol,
        "max_age_microseconds": max_age_microseconds,
        "receipt_age_microseconds": receipt_age_microseconds,
        "final_preflight_outcome": final_preflight_outcome,
        "freshness_outcome": freshness_outcome,
        "fresh_precheck_executed": fresh_precheck_executed,
        "receipt_age_evaluated": receipt_age_evaluated,
        "freshness_policy_evaluated": freshness_policy_evaluated,
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
    }


def _posture_ok(activation_authorized: object, runtime_activation_outcome: object) -> bool:
    return activation_authorized is False and runtime_activation_outcome == "no_go"


def _parse_aware(value: object) -> datetime | None:
    """Parse an ISO ``checked_at`` string into a timezone-aware datetime, else ``None``.

    Only an exact built-in ``str`` is accepted; a naive datetime string, a ``None`` UTC
    offset, or any parse error fails closed (no raw value/exception escapes)."""

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
    except Exception:
        return None
    if offset is None:
        return None
    return parsed


def _is_lower_hex64(value: object) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _is_nonempty_str(value: object) -> bool:
    return type(value) is str and value != ""


def _is_nonnegative_int(value: object) -> bool:
    return type(value) is int and not isinstance(value, bool) and value >= 0


def _invalid() -> ActivationCandidateEvidenceResult:
    return ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.INVALID,
        reasons=("candidate_evidence_invalid_input",),
        evidence=None,
    )


def _not_eligible() -> ActivationCandidateEvidenceResult:
    return ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.NOT_ELIGIBLE,
        reasons=("candidate_evidence_not_eligible",),
        evidence=None,
    )
