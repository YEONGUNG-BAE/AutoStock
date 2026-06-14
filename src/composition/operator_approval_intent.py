"""Canonical Operator approval intent binding (RTM-7c.4o).

Freezes a freshness-qualified combined PASS with CREATED canonical evidence into an immutable
approval-intent digest that a *future* approval consumer can reference. The digest binds one
explicit evidence digest, three manual Operator declarations, and a caller ``declared_at`` time.

Approval intent does **not** mean: Operator identity authentication, signing/HMAC, writer-stop
machine proof, approval consumption, replay prevention, an activation token, or runtime
activation authorization. The activation posture is a constant NO-GO on every code path.

Intent is produced **only** for a combined ``PASS`` whose evidence is ``CREATED`` with a
strict schema-v2 evidence semantic contract (matching hash alone is insufficient) and a
recomputed matching ``evidence_sha256``. Combined ``NO_GO`` is ``NOT_ELIGIBLE``; a
contradictory combined ``PASS`` or malformed evidence is ``INVALID``.

No clock read of its own (the caller passes ``declared_at``), no network/credential/broker
access, no operational DB write, no filesystem/intent-file write, no persistence, and no
receipt verifier / precheck / evaluator / evidence-builder re-invocation. Production validation
does not serialize or deep-copy caller-owned evidence objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from composition.activation_candidate_evidence import (
    ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
    ActivationCandidateEvidence,
    ActivationCandidateEvidenceOutcome,
    ActivationCandidateEvidenceResult,
    FreshnessQualifiedEvidenceOutcome,
    FreshnessQualifiedEvidenceResult,
    validate_activation_candidate_evidence_scalars,
)
from composition.precheck_receipt_schema import is_hex64, market_valid, symbol_valid
from composition.activation_candidate_freshness_preflight import (
    ActivationCandidateFreshnessPreflightOutcome,
    ActivationCandidateFreshnessPreflightResult,
)
from decision.canonical_json import payload_sha256

__all__ = [
    "APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE",
    "OPERATOR_APPROVAL_INTENT_FIELD_NAMES",
    "OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION",
    "OperatorApprovalIntent",
    "OperatorApprovalIntentOutcome",
    "OperatorApprovalIntentResult",
    "ValidatedOperatorApprovalIntentScalars",
    "build_operator_approval_intent",
    "operator_approval_intent_hash_payload",
    "validate_operator_approval_intent_object",
    "validate_operator_approval_intent_scalars",
]

_OPERATOR_APPROVAL_INTENT_FIELD_NAMES = frozenset(
    {
        "schema_version",
        "declared_at",
        "evidence_schema_version",
        "evidence_sha256",
        "market",
        "symbol",
        "approval_scope",
        "operator_approval_declared",
        "writers_stopped_manually_confirmed",
        "live_orders_forbidden_confirmed",
        "activation_authorized",
        "runtime_activation_outcome",
        "approval_intent_sha256",
    }
)

OPERATOR_APPROVAL_INTENT_FIELD_NAMES = _OPERATOR_APPROVAL_INTENT_FIELD_NAMES

OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION = 1

APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE = "attended_paper_fast_loop_candidate"


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


@dataclass(frozen=True)
class ValidatedOperatorApprovalIntentScalars:
    """검증된 approval-intent scalar snapshot — builder/verifier 공유."""

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
        qualified_result = combined_result.qualified_result
        evidence_result = combined_result.evidence_result
    except AttributeError:
        return _invalid()

    if combined_outcome is FreshnessQualifiedEvidenceOutcome.NO_GO:
        return _not_eligible()

    if combined_outcome is not FreshnessQualifiedEvidenceOutcome.PASS:
        return _invalid()

    # A combined PASS with contradictory reasons fail closed — exact empty tuple only.
    if not _is_exact_empty_reasons(combined_reasons):
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
    if not _is_exact_empty_reasons(er_reasons):
        return _invalid()
    if evidence is None:
        return _invalid()
    if type(evidence) is not ActivationCandidateEvidence:
        return _invalid()

    # Single-read snapshot of every evidence scalar into locals; validation + hash reuse them.
    try:
        ev_schema_version = evidence.schema_version
        ev_evaluated_at = evidence.evaluated_at
        ev_receipt_sha256 = evidence.receipt_sha256
        ev_fresh_receipt_sha256 = evidence.fresh_precheck_receipt_sha256
        market = evidence.market
        symbol = evidence.symbol
        ev_max_age = evidence.max_age_microseconds
        ev_receipt_age = evidence.receipt_age_microseconds
        ev_final_outcome = evidence.final_preflight_outcome
        ev_freshness_outcome = evidence.freshness_outcome
        ev_fresh_executed = evidence.fresh_precheck_executed
        ev_age_evaluated = evidence.receipt_age_evaluated
        ev_policy_evaluated = evidence.freshness_policy_evaluated
        ev_activation = evidence.activation_authorized
        ev_runtime = evidence.runtime_activation_outcome
        evidence_sha256 = evidence.evidence_sha256
    except AttributeError:
        return _invalid()

    validated = validate_activation_candidate_evidence_scalars(
        schema_version=ev_schema_version,
        evaluated_at=ev_evaluated_at,
        receipt_sha256=ev_receipt_sha256,
        fresh_precheck_receipt_sha256=ev_fresh_receipt_sha256,
        market=market,
        symbol=symbol,
        max_age_microseconds=ev_max_age,
        receipt_age_microseconds=ev_receipt_age,
        final_preflight_outcome=ev_final_outcome,
        freshness_outcome=ev_freshness_outcome,
        fresh_precheck_executed=ev_fresh_executed,
        receipt_age_evaluated=ev_age_evaluated,
        freshness_policy_evaluated=ev_policy_evaluated,
        activation_authorized=ev_activation,
        runtime_activation_outcome=ev_runtime,
        evidence_sha256=evidence_sha256,
    )
    if validated is None:
        return _invalid()

    # Combined PASS requires a consistent qualified PASS identity/posture contract.
    if type(qualified_result) is not ActivationCandidateFreshnessPreflightResult:
        return _invalid()
    try:
        qr_outcome = qualified_result.outcome
        qr_reasons = qualified_result.reasons
        qr_receipt_sha256 = qualified_result.receipt_sha256
        qr_market = qualified_result.market
        qr_symbol = qualified_result.symbol
        qr_freshness_policy_evaluated = qualified_result.freshness_policy_evaluated
        qr_activation = qualified_result.activation_authorized
        qr_runtime = qualified_result.runtime_activation_outcome
        qr_explicit_approval = qualified_result.explicit_operator_approval_required
        qr_writers_stopped = qualified_result.writers_stopped_manual_confirmation_required
    except AttributeError:
        return _invalid()

    if qr_outcome is not ActivationCandidateFreshnessPreflightOutcome.PASS:
        return _invalid()
    if not _is_exact_empty_reasons(qr_reasons):
        return _invalid()
    if type(qr_receipt_sha256) is not str or qr_receipt_sha256 != validated.receipt_sha256:
        return _invalid()
    if type(qr_market) is not str or qr_market != validated.market:
        return _invalid()
    if type(qr_symbol) is not str or qr_symbol != validated.symbol:
        return _invalid()
    if not _exact_true_bool(qr_freshness_policy_evaluated):
        return _invalid()
    if not _exact_false_bool(qr_activation):
        return _invalid()
    if type(qr_runtime) is not str or qr_runtime != "no_go":
        return _invalid()
    if not _exact_true_bool(qr_explicit_approval):
        return _invalid()
    if not _exact_true_bool(qr_writers_stopped):
        return _invalid()

    if not _exact_true_bool(operator_approval_declared):
        return _invalid()
    if not _exact_true_bool(writers_stopped_manually_confirmed):
        return _invalid()
    if not _exact_true_bool(live_orders_forbidden_confirmed):
        return _invalid()

    # ``declared_at``는 caller datetime을 한 번만 관찰해 동결한다.
    declared_snapshot = snapshot_declared_at(declared_at)
    if declared_snapshot is None:
        return _invalid()

    declared_at_iso, declared_at_parsed = declared_snapshot

    evidence_evaluated = _parse_aware(validated.evaluated_at)
    if evidence_evaluated is None:
        return _invalid()
    if declared_at_parsed < evidence_evaluated:
        return _invalid()
    hash_payload = operator_approval_intent_hash_payload(
        declared_at=declared_at_iso,
        evidence_schema_version=validated.schema_version,
        evidence_sha256=validated.evidence_sha256,
        market=validated.market,
        symbol=validated.symbol,
    )
    approval_intent_sha256 = payload_sha256(hash_payload)

    intent = OperatorApprovalIntent(
        schema_version=OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
        declared_at=declared_at_iso,
        evidence_schema_version=validated.schema_version,
        evidence_sha256=validated.evidence_sha256,
        market=validated.market,
        symbol=validated.symbol,
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


def operator_approval_intent_hash_payload(
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


def validate_operator_approval_intent_scalars(
    *,
    schema_version: object,
    declared_at: object,
    evidence_schema_version: object,
    evidence_sha256: object,
    market: object,
    symbol: object,
    approval_scope: object,
    operator_approval_declared: object,
    writers_stopped_manually_confirmed: object,
    live_orders_forbidden_confirmed: object,
    activation_authorized: object,
    runtime_activation_outcome: object,
    approval_intent_sha256: object,
) -> ValidatedOperatorApprovalIntentScalars | None:
    """Shared scalar+semantic contract — builder output과 verifier 입력 모두 동일 규칙."""

    if type(schema_version) is not int or isinstance(schema_version, bool):
        return None
    if schema_version != OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION:
        return None

    if not _approval_intent_declared_at_valid(declared_at):
        return None

    if type(evidence_schema_version) is not int or isinstance(evidence_schema_version, bool):
        return None
    if evidence_schema_version != ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION:
        return None

    if not is_hex64(evidence_sha256):
        return None

    if not market_valid(market):
        return None
    if not symbol_valid(symbol):
        return None

    if type(approval_scope) is not str:
        return None
    if approval_scope != APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE:
        return None

    if not _exact_true_bool(operator_approval_declared):
        return None
    if not _exact_true_bool(writers_stopped_manually_confirmed):
        return None
    if not _exact_true_bool(live_orders_forbidden_confirmed):
        return None
    if not _exact_false_bool(activation_authorized):
        return None

    if type(runtime_activation_outcome) is not str:
        return None
    if runtime_activation_outcome != "no_go":
        return None

    if not is_hex64(approval_intent_sha256):
        return None

    return ValidatedOperatorApprovalIntentScalars(
        schema_version=schema_version,
        declared_at=declared_at,
        evidence_schema_version=evidence_schema_version,
        evidence_sha256=evidence_sha256,
        market=market,
        symbol=symbol,
        approval_scope=approval_scope,
        operator_approval_declared=True,
        writers_stopped_manually_confirmed=True,
        live_orders_forbidden_confirmed=True,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        approval_intent_sha256=approval_intent_sha256,
    )


def validate_operator_approval_intent_object(
    value: object,
) -> ValidatedOperatorApprovalIntentScalars | None:
    """Exact ``OperatorApprovalIntent`` dataclass만 허용 — subclass/duck-type 거부."""

    if type(value) is not OperatorApprovalIntent:
        return None
    try:
        return validate_operator_approval_intent_scalars(
            schema_version=value.schema_version,
            declared_at=value.declared_at,
            evidence_schema_version=value.evidence_schema_version,
            evidence_sha256=value.evidence_sha256,
            market=value.market,
            symbol=value.symbol,
            approval_scope=value.approval_scope,
            operator_approval_declared=value.operator_approval_declared,
            writers_stopped_manually_confirmed=value.writers_stopped_manually_confirmed,
            live_orders_forbidden_confirmed=value.live_orders_forbidden_confirmed,
            activation_authorized=value.activation_authorized,
            runtime_activation_outcome=value.runtime_activation_outcome,
            approval_intent_sha256=value.approval_intent_sha256,
        )
    except AttributeError:
        return None


def _approval_intent_declared_at_valid(value: object) -> bool:
    """ISO parseable timezone-aware ``declared_at`` string."""

    if type(value) is not str or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return False
    if type(parsed) is not datetime:
        return False
    try:
        offset = parsed.utcoffset()
    except Exception:
        return False
    return offset is not None


def snapshot_declared_at(value: object) -> tuple[str, datetime] | None:
    """Caller ``declared_at``를 detached single observation으로 동결한다.

    정확한 built-in ``datetime``만 허용한다. ``isoformat()``을 정확히 한 번 호출하고,
    그 문자열을 ``fromisoformat()``으로 재파싱한 뒤 timezone-aware 여부를 확인한다.
    이후 caller datetime 또는 caller tzinfo에는 재접근하지 않는다."""

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


def _is_exact_empty_reasons(value: object) -> bool:
    """Exact built-in empty tuple only — caller ``__eq__``/``__ne__`` hook을 실행하지 않는다."""

    return type(value) is tuple and len(value) == 0


def _exact_true_bool(value: object) -> bool:
    """Exact built-in ``True`` only — rejects ``False``/``0``/``1``/``None``/``\"true\"``/subclass."""

    return type(value) is bool and value is True


def _exact_false_bool(value: object) -> bool:
    """Exact built-in ``False`` only — rejects ``True``/``0``/``1``/``None``/subclass."""

    return type(value) is bool and value is False


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
