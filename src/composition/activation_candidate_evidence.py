"""Canonical freshness-qualified candidate evidence (RTM-7c.4n).

Freezes a freshness-qualified mechanical PASS into a single immutable canonical evidence
payload that a *future* Operator-approval stage can reference. The digest binds one original
verified candidate receipt and one fresh precheck receipt, one explicit max-age policy, one
caller time, and one final-preflight/freshness result together.

Hash equality alone is insufficient: the fresh precheck receipt must satisfy the same
schema/semantic contract as the standalone precheck receipt verifier — an unsupported schema
with a recomputed matching hash, or semantically malformed fingerprints with a matching hash,
still fails closed. Evidence is not approval, signature, or authenticity.

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
from composition.activation_candidate_revalidation import (
    ActivationCandidateRevalidationOutcome,
    ActivationCandidateRevalidationResult,
)
from composition.paper_fast_loop import (
    InspectionOutcome,
    MachineCheckOutcome,
    PaperFastLoopInspection,
    RuntimePrecheckReceipt,
    RuntimePrecheckResult,
)
from composition.paper_fast_loop_artifacts import PAPER_FAST_LOOP_ARTIFACT_NAMES
from composition.precheck_receipt_schema import (
    PrecheckReceiptError,
    market_valid,
    symbol_valid,
    validate_receipt_fingerprints,
    validate_runtime_precheck_receipt_object,
)
from composition.sqlite_inspector import ArtifactFingerprint
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

ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION = 2

_HEX64 = re.compile(r"[0-9a-f]{64}")


class ActivationCandidateEvidenceOutcome(StrEnum):
    CREATED = "created"
    NOT_ELIGIBLE = "not_eligible"
    INVALID = "invalid"


@dataclass(frozen=True)
class ActivationCandidateEvidence:
    """Immutable canonical evidence digest — not approval, signature, or activation.

    ``evidence_sha256`` is the canonical sha256 over every other field (the hash-payload fields
    in :func:`_evidence_hash_payload`), so an independent party can recompute it from the same
    scalars. ``receipt_sha256`` binds the *original* candidate precheck receipt;
    ``fresh_precheck_receipt_sha256`` binds the *current* fresh precheck receipt observed at
    ``evaluated_at`` (schema v2) — a PASS digest combines both observations. No raw receipt,
    path, fingerprint body, or secret is retained."""

    schema_version: int
    evaluated_at: str
    receipt_sha256: str
    fresh_precheck_receipt_sha256: str
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

    # ``evaluated_at`` must be an *exact* built-in ``datetime`` (not a subclass): a subclass
    # could override ``isoformat``/``__sub__``/``utcoffset`` to raise or lie, so the strict
    # type guard fails such an object closed before any arithmetic — no broad ``BaseException``
    # catch is needed and ``MemoryError``/``KeyboardInterrupt``/``SystemExit`` are never
    # swallowed. The shared tz-aware guard then rejects naive / bad-offset datetimes.
    if type(evaluated_at) is not datetime or final_preflight_now_is_invalid(evaluated_at):
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
        revalidation_result = final_result.revalidation_result
        current_precheck_result = final_result.current_precheck_result
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
    if not market_valid(market) or not symbol_valid(symbol):
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

    # --- fresh machine-proof binding (RTM-7c.4n) ---
    # A boolean ``fresh_precheck_executed=True`` is NOT proof. The evidence must bind the
    # actual revalidation PASS *and* the current fresh precheck PASS result objects (with the
    # fresh precheck receipt) observed at ``evaluated_at`` — all mutually consistent. The
    # frozen observations on those result objects are compared directly (the existing
    # final-preflight drift logic is not re-implemented here).
    fresh_precheck_receipt_sha256 = _machine_proof_receipt_sha256(
        revalidation_result=revalidation_result,
        current_precheck_result=current_precheck_result,
        receipt_sha256=receipt_sha256,
        market=market,
        symbol=symbol,
        evaluated_at_iso=evaluated_at_iso,
    )
    if fresh_precheck_receipt_sha256 is None:
        return _invalid()

    hash_payload = _evidence_hash_payload(
        evaluated_at=evaluated_at_iso,
        receipt_sha256=receipt_sha256,
        fresh_precheck_receipt_sha256=fresh_precheck_receipt_sha256,
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
        fresh_precheck_receipt_sha256=fresh_precheck_receipt_sha256,
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
    fresh_precheck_receipt_sha256: str,
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
    of the digest so a tampered posture changes the hash. ``receipt_sha256`` and
    ``fresh_precheck_receipt_sha256`` (schema v2) both feed the digest so changing either
    bound observation changes the hash. Canonical JSON sorts keys, so an independent
    recomputation from ``asdict(evidence)`` minus ``evidence_sha256`` matches."""

    return {
        "schema_version": ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
        "evaluated_at": evaluated_at,
        "receipt_sha256": receipt_sha256,
        "fresh_precheck_receipt_sha256": fresh_precheck_receipt_sha256,
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


def _valid_fingerprint_pair(before: object, after: object) -> bool:
    """Canonical 4-artifact fingerprint pair — exact type, order, semantics (verifier parity).

    A non-tuple, wrong length, a non-``ArtifactFingerprint`` element (subclass / wrong object),
    a name out of canonical order, or a semantic field violation fails closed."""

    if type(before) is not tuple or type(after) is not tuple:
        return False
    if len(before) != len(PAPER_FAST_LOOP_ARTIFACT_NAMES) or len(after) != len(
        PAPER_FAST_LOOP_ARTIFACT_NAMES
    ):
        return False
    for fp_b, fp_a, name in zip(before, after, PAPER_FAST_LOOP_ARTIFACT_NAMES):
        if type(fp_b) is not ArtifactFingerprint or type(fp_a) is not ArtifactFingerprint:
            return False
        if fp_b.name != name or fp_a.name != name:
            return False
    try:
        validate_receipt_fingerprints(before, after)
    except (AttributeError, TypeError, ValueError, PrecheckReceiptError):
        return False
    return True


def _machine_proof_receipt_sha256(
    *,
    revalidation_result: object,
    current_precheck_result: object,
    receipt_sha256: str,
    market: str,
    symbol: str,
    evaluated_at_iso: str,
) -> str | None:
    """Validate the bound revalidation + fresh precheck machine proof; return the fresh
    precheck receipt's independently-recomputed sha256 on success, else ``None``.

    Requires the *actual* result objects (not a boolean ``fresh_precheck_executed``): a
    revalidation PASS, a fresh precheck PASS with an OK inspection and a valid fresh precheck
    receipt, a single canonical artifact observation held identical from candidate
    revalidation through the fresh precheck, and a receipt hash that recomputes via the reused
    canonical receipt schema helper (no hash reimplementation, no filesystem/network re-verify).
    Raw fingerprint bodies, paths, and exceptions never escape into a reason."""

    if (
        type(revalidation_result) is not ActivationCandidateRevalidationResult
        or type(current_precheck_result) is not RuntimePrecheckResult
    ):
        return None

    # Single-read of each caller-owned nested object into locals (revalidation, current
    # precheck, inspection, fresh precheck receipt); validation reuses only these locals.
    try:
        reval_outcome = revalidation_result.outcome
        reval_reasons = revalidation_result.reasons
        reval_sha = revalidation_result.receipt_sha256
        reval_market = revalidation_result.market
        reval_symbol = revalidation_result.symbol
        reval_before = revalidation_result.current_fingerprints_before
        reval_after = revalidation_result.current_fingerprints_after
        reval_activation = revalidation_result.activation_authorized
        reval_runtime = revalidation_result.runtime_activation_outcome
        reval_approval = revalidation_result.explicit_operator_approval_required
        reval_writers = revalidation_result.writers_stopped_manual_confirmation_required
        reval_freshness = revalidation_result.freshness_evaluated

        pc_machine = current_precheck_result.machine_outcome
        pc_reasons = current_precheck_result.reasons
        pc_market = current_precheck_result.market
        pc_symbol = current_precheck_result.symbol
        pc_inspection = current_precheck_result.inspection
        pc_before = current_precheck_result.fingerprints_before
        pc_after = current_precheck_result.fingerprints_after
        pc_activation = current_precheck_result.activation_authorized
        pc_runtime = current_precheck_result.runtime_activation_outcome
        pc_approval = current_precheck_result.explicit_operator_approval_required
        pc_writers = current_precheck_result.writers_stopped_manual_confirmation_required
        pc_receipt = current_precheck_result.receipt
    except AttributeError:
        return None

    # Revalidation strict contract.
    if reval_outcome is not ActivationCandidateRevalidationOutcome.PASS:
        return None
    if reval_reasons != ():
        return None
    if reval_sha != receipt_sha256 or reval_market != market or reval_symbol != symbol:
        return None
    if not _posture_ok(reval_activation, reval_runtime):
        return None
    if reval_approval is not True or reval_writers is not True:
        return None
    if reval_freshness is not False:
        return None

    # Current precheck strict contract.
    if pc_machine is not MachineCheckOutcome.PASS:
        return None
    if pc_reasons != ():
        return None
    if pc_market != market or pc_symbol != symbol:
        return None
    if not _posture_ok(pc_activation, pc_runtime):
        return None
    if pc_approval is not True or pc_writers is not True:
        return None

    # Inspection strict contract.
    if type(pc_inspection) is not PaperFastLoopInspection:
        return None
    try:
        insp_outcome = pc_inspection.outcome
        insp_reasons = pc_inspection.reasons
        insp_market = pc_inspection.market
        insp_symbol = pc_inspection.symbol
    except AttributeError:
        return None
    if insp_outcome is not InspectionOutcome.OK or insp_reasons != ():
        return None
    if insp_market != market or insp_symbol != symbol:
        return None

    # Fresh precheck receipt strict contract.
    if type(pc_receipt) is not RuntimePrecheckReceipt:
        return None
    try:
        rc_schema = pc_receipt.schema_version
        rc_checked_at = pc_receipt.checked_at
        rc_market = pc_receipt.market
        rc_symbol = pc_receipt.symbol
        rc_enabled = pc_receipt.enabled
        rc_machine = pc_receipt.machine_outcome
        rc_inspection = pc_receipt.inspection_outcome
        rc_reasons = pc_receipt.reasons
        rc_before = pc_receipt.fingerprints_before
        rc_after = pc_receipt.fingerprints_after
        rc_activation = pc_receipt.activation_authorized
        rc_runtime = pc_receipt.runtime_activation_outcome
        rc_approval = pc_receipt.explicit_operator_approval_required
        rc_writers = pc_receipt.writers_stopped_manual_confirmation_required
        rc_sha = pc_receipt.receipt_sha256
    except AttributeError:
        return None

    if rc_checked_at != evaluated_at_iso:
        return None
    if rc_market != market or rc_symbol != symbol:
        return None

    # Exact state relationship: one canonical artifact observation held identical from the
    # candidate revalidation through the fresh precheck (frozen observations only).
    if not _valid_fingerprint_pair(reval_before, reval_after):
        return None
    if reval_before != reval_after:
        return None
    if not _valid_fingerprint_pair(pc_before, pc_after):
        return None
    if pc_before != pc_after:
        return None
    if reval_after != pc_after:
        return None
    if rc_before != pc_before or rc_after != pc_after:
        return None

    # Full fresh-receipt schema/semantic/hash validation — same contract as the standalone
    # verifier (hash match alone is insufficient).
    try:
        return validate_runtime_precheck_receipt_object(
            schema_version=rc_schema,
            checked_at=rc_checked_at,
            market=rc_market,
            symbol=rc_symbol,
            enabled=rc_enabled,
            machine_outcome=rc_machine,
            inspection_outcome=rc_inspection,
            reasons=rc_reasons,
            fingerprints_before=rc_before,
            fingerprints_after=rc_after,
            activation_authorized=rc_activation,
            runtime_activation_outcome=rc_runtime,
            explicit_operator_approval_required=rc_approval,
            writers_stopped_manual_confirmation_required=rc_writers,
            receipt_sha256=rc_sha,
        )
    except (AttributeError, TypeError, ValueError, PrecheckReceiptError):
        return None


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
