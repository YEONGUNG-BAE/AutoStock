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
    final_preflight_now_is_invalid,
)
from composition.activation_candidate_freshness_preflight import (
    ActivationCandidateFreshnessPreflightOutcome,
    ActivationCandidateFreshnessPreflightResult,
    freshness_qualify_activation_candidate,
)
from composition.receipt_freshness_policy import (
    ReceiptFreshnessOutcome,
    ReceiptFreshnessPolicy,
)
from config.settings import RuntimePaperFastLoopSettings
from decision.canonical_json import payload_sha256

__all__ = [
    "ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION",
    "ActivationCandidateEvidence",
    "ActivationCandidateEvidenceOutcome",
    "ActivationCandidateEvidenceResult",
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


@dataclass(frozen=True)
class FreshnessQualifiedEvidenceResult:
    """Freshness-qualified verdict paired with optional canonical evidence.

    ``evidence_result`` is ``None`` when the qualified verdict is ``NO_GO`` (the builder is
    not invoked); it is a ``CREATED`` evidence result on a qualified ``PASS``."""

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
    except AttributeError:
        return _invalid()

    # A well-formed non-PASS verdict (NO_GO/STALE/final NO_GO) is simply not eligible.
    if outcome is not ActivationCandidateFreshnessPreflightOutcome.PASS:
        return _not_eligible()

    # From here the result claims PASS — any broken invariant is a contradictory/synthetic
    # result and fails closed as INVALID (never silently produces a digest).
    try:
        if reasons != ():
            return _invalid()
        if final_result is None or freshness_eval is None:
            return _invalid()
        if type(policy_evaluated) is not bool or policy_evaluated is not True:
            return _invalid()
        if activation_authorized is not False:
            return _invalid()
        if runtime_activation_outcome != "no_go":
            return _invalid()

        final_outcome = final_result.outcome
        fresh_precheck_executed = final_result.fresh_precheck_executed
        receipt_age_evaluated = final_result.receipt_age_evaluated

        fresh_outcome = freshness_eval.outcome
        fresh_policy_evaluated = freshness_eval.freshness_policy_evaluated
        receipt_age = freshness_eval.receipt_age_microseconds
        max_age = freshness_eval.max_age_microseconds
    except AttributeError:
        return _invalid()

    if final_outcome is not ActivationCandidateFinalPreflightOutcome.PASS:
        return _invalid()
    if fresh_outcome is not ReceiptFreshnessOutcome.FRESH:
        return _invalid()
    if fresh_policy_evaluated is not True:
        return _invalid()
    if fresh_precheck_executed is not True or receipt_age_evaluated is not True:
        return _invalid()
    if not _is_lower_hex64(receipt_sha256):
        return _invalid()
    if not _is_nonempty_str(market) or not _is_nonempty_str(symbol):
        return _invalid()
    if not _is_nonnegative_int(receipt_age) or not _is_nonnegative_int(max_age):
        return _invalid()
    if receipt_age > max_age:
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
    if qualified.outcome is ActivationCandidateFreshnessPreflightOutcome.PASS:
        evidence_result: ActivationCandidateEvidenceResult | None = (
            build_activation_candidate_evidence(qualified_result=qualified, evaluated_at=now)
        )
    else:
        evidence_result = None
    return FreshnessQualifiedEvidenceResult(
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
