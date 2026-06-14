"""Canonical Operator approval intent binding (RTM-7c.4o).

Freezes a freshness-qualified combined PASS with CREATED canonical evidence into an immutable
approval-intent digest that a *future* approval consumer can reference. The digest binds one
explicit evidence digest, three manual Operator declarations, and a caller ``declared_at`` time.

Approval intent does **not** mean: Operator identity authentication, signing/HMAC, writer-stop
machine proof, approval consumption, replay prevention, an activation token, or runtime
activation authorization. The activation posture is a constant NO-GO on every code path.

Intent is produced **only** for a combined ``PASS`` whose evidence is ``CREATED`` with a
strict schema-v2 evidence contract and a recomputed matching ``evidence_sha256``. Combined
``NO_GO`` is ``NOT_ELIGIBLE``; a contradictory combined ``PASS`` or malformed evidence is
``INVALID``.

No clock read of its own (the caller passes ``declared_at``), no network/credential/broker
access, no operational DB write, no filesystem/intent-file write, no persistence, and no
receipt verifier / precheck / evaluator / evidence-builder re-invocation.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum

from composition.activation_candidate_evidence import (
    ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
    ActivationCandidateEvidence,
    ActivationCandidateEvidenceOutcome,
    ActivationCandidateEvidenceResult,
    FreshnessQualifiedEvidenceOutcome,
    FreshnessQualifiedEvidenceResult,
)
from composition.activation_candidate_final_preflight import final_preflight_now_is_invalid
from composition.precheck_receipt_schema import market_valid, symbol_valid
from decision.canonical_json import payload_sha256

__all__ = [
    "APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE",
    "OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION",
    "OperatorApprovalIntent",
    "OperatorApprovalIntentOutcome",
    "OperatorApprovalIntentResult",
    "build_operator_approval_intent",
]

OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION = 1

APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE = "attended_paper_fast_loop_candidate"

_HEX64 = re.compile(r"[0-9a-f]{64}")


class OperatorApprovalIntentOutcome(StrEnum):
    CREATED = "created"
    NOT_ELIGIBLE = "not_eligible"
    INVALID = "invalid"


@dataclass(frozen=True)
class OperatorApprovalIntent:
    """Immutable canonical approval-intent digest — not identity, signature, or activation.

    ``approval_intent_sha256`` is the canonical sha256 over every other field (the hash-payload
    fields in :func:`_intent_hash_payload`), so an independent party can recompute it from the
    same scalars. The three ``*_confirmed`` / ``operator_approval_declared`` fields are manual
    declarations, not machine proof."""

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
class OperatorApprovalIntentResult:
    outcome: OperatorApprovalIntentOutcome
    reasons: tuple[str, ...]
    intent: OperatorApprovalIntent | None


def build_operator_approval_intent(
    *,
    combined_result: FreshnessQualifiedEvidenceResult,
    declared_at: datetime,
    operator_approval_declared: bool,
    writers_stopped_manually_confirmed: bool,
    live_orders_forbidden_confirmed: bool,
) -> OperatorApprovalIntentResult:
    """Build canonical approval intent from a combined PASS + CREATED evidence result.

    ``declared_at`` must be timezone-aware and must not precede the evidence ``evaluated_at``.
    The three confirmation booleans must each be the exact built-in ``True`` — they are manual
    declarations, not machine proof. This builder reads no clock and re-invokes no upstream
    verifier/precheck/evaluator/evidence builder."""

    if type(combined_result) is not FreshnessQualifiedEvidenceResult:
        return _invalid()

    # Single-read snapshot of the combined result into locals.
    try:
        combined_outcome = combined_result.outcome
        combined_reasons = combined_result.reasons
        evidence_result = combined_result.evidence_result
    except AttributeError:
        return _invalid()

    if combined_outcome is FreshnessQualifiedEvidenceOutcome.NO_GO:
        return _not_eligible()

    if combined_outcome is not FreshnessQualifiedEvidenceOutcome.PASS:
        return _invalid()

    # A combined PASS with non-empty reasons is contradictory — fail closed.
    if combined_reasons != ():
        return _invalid()

    if type(evidence_result) is not ActivationCandidateEvidenceResult:
        return _invalid()

    # Single-read snapshot of the evidence result into locals.
    try:
        er_outcome = evidence_result.outcome
        er_reasons = evidence_result.reasons
        evidence = evidence_result.evidence
    except AttributeError:
        return _invalid()

    if er_outcome is not ActivationCandidateEvidenceOutcome.CREATED:
        return _invalid()
    if er_reasons != ():
        return _invalid()
    if evidence is None:
        return _invalid()
    if type(evidence) is not ActivationCandidateEvidence:
        return _invalid()

    # Single-read snapshot of evidence scalars into locals; validation + hash reuse them.
    try:
        ev_schema_version = evidence.schema_version
        ev_evaluated_at = evidence.evaluated_at
        evidence_sha256 = evidence.evidence_sha256
        market = evidence.market
        symbol = evidence.symbol
        ev_activation = evidence.activation_authorized
        ev_runtime = evidence.runtime_activation_outcome
    except AttributeError:
        return _invalid()

    if ev_schema_version != ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION:
        return _invalid()
    if not _is_lower_hex64(evidence_sha256):
        return _invalid()
    if not market_valid(market) or not symbol_valid(symbol):
        return _invalid()
    if not _posture_ok(ev_activation, ev_runtime):
        return _invalid()

    evidence_payload = asdict(evidence)
    stored_evidence_sha = evidence_payload.pop("evidence_sha256")
    if stored_evidence_sha != evidence_sha256:
        return _invalid()
    if payload_sha256(evidence_payload) != evidence_sha256:
        return _invalid()

    if not _exact_true_bool(operator_approval_declared):
        return _invalid()
    if not _exact_true_bool(writers_stopped_manually_confirmed):
        return _invalid()
    if not _exact_true_bool(live_orders_forbidden_confirmed):
        return _invalid()

    # ``declared_at`` must be an *exact* built-in ``datetime`` (not a subclass).
    if type(declared_at) is not datetime or final_preflight_now_is_invalid(declared_at):
        return _invalid()

    evidence_evaluated = _parse_aware(ev_evaluated_at)
    if evidence_evaluated is None:
        return _invalid()
    if declared_at < evidence_evaluated:
        return _invalid()

    declared_at_iso = declared_at.isoformat()
    hash_payload = _intent_hash_payload(
        declared_at=declared_at_iso,
        evidence_schema_version=ev_schema_version,
        evidence_sha256=evidence_sha256,
        market=market,
        symbol=symbol,
    )
    approval_intent_sha256 = payload_sha256(hash_payload)

    intent = OperatorApprovalIntent(
        schema_version=OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
        declared_at=declared_at_iso,
        evidence_schema_version=ev_schema_version,
        evidence_sha256=evidence_sha256,
        market=market,
        symbol=symbol,
        approval_scope=APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE,
        operator_approval_declared=True,
        writers_stopped_manually_confirmed=True,
        live_orders_forbidden_confirmed=True,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        approval_intent_sha256=approval_intent_sha256,
    )
    return OperatorApprovalIntentResult(
        outcome=OperatorApprovalIntentOutcome.CREATED,
        reasons=(),
        intent=intent,
    )


def _intent_hash_payload(
    *,
    declared_at: str,
    evidence_schema_version: int,
    evidence_sha256: str,
    market: str,
    symbol: str,
) -> dict[str, object]:
    """Canonical hash payload — every intent field except ``approval_intent_sha256``."""

    return {
        "schema_version": OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
        "declared_at": declared_at,
        "evidence_schema_version": evidence_schema_version,
        "evidence_sha256": evidence_sha256,
        "market": market,
        "symbol": symbol,
        "approval_scope": APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE,
        "operator_approval_declared": True,
        "writers_stopped_manually_confirmed": True,
        "live_orders_forbidden_confirmed": True,
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
    }


def _exact_true_bool(value: object) -> bool:
    """Exact built-in ``True`` only — rejects ``False``/``0``/``1``/``None``/``\"true\"``/subclass."""

    return type(value) is bool and value is True


def _posture_ok(activation_authorized: object, runtime_activation_outcome: object) -> bool:
    return activation_authorized is False and runtime_activation_outcome == "no_go"


def _parse_aware(value: object) -> datetime | None:
    """Parse evidence ``evaluated_at`` into a timezone-aware datetime, else ``None``."""

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


def _invalid() -> OperatorApprovalIntentResult:
    return OperatorApprovalIntentResult(
        outcome=OperatorApprovalIntentOutcome.INVALID,
        reasons=("approval_intent_invalid_input",),
        intent=None,
    )


def _not_eligible() -> OperatorApprovalIntentResult:
    return OperatorApprovalIntentResult(
        outcome=OperatorApprovalIntentOutcome.NOT_ELIGIBLE,
        reasons=("approval_intent_not_eligible",),
        intent=None,
    )
