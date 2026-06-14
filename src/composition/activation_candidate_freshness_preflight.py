"""Explicit freshness-qualified activation candidate preflight (RTM-7c.4l).

Composes the verified final-preflight core (RTM-7c.4h) with an explicit caller-supplied
``ReceiptFreshnessPolicy``. Mechanical final-preflight PASS **and** evaluator ``FRESH`` yields
a freshness-qualified mechanical PASS — this is **not** Operator approval, writer-stop proof,
receipt authenticity, or activation authorization.

Policy is a **required** argument: no default max-age, no config/env/CLI binding. The existing
``final_preflight_activation_candidate`` wrapper remains policy-neutral.

Per call: one policy snapshot build, one verifier (receipt snapshot build), one receipt
snapshot build, zero verifier calls inside the verified final-preflight core and freshness
evaluator. Raw receipt is not re-read after snapshot. ``ReceiptTimeAssessment`` object identity from final preflight is passed unchanged
to the freshness evaluator.

No clock read of its own; no operational DB write; no network/credential/broker access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from composition.activation_candidate_final_preflight import (
    ActivationCandidateFinalPreflightOutcome,
    ActivationCandidateFinalPreflightResult,
    final_preflight_now_is_invalid,
    final_preflight_verified_activation_candidate,
)
from composition.receipt_freshness_policy import (
    ReceiptFreshnessEvaluation,
    ReceiptFreshnessOutcome,
    ReceiptFreshnessPolicy,
    evaluate_receipt_freshness,
    snapshot_receipt_freshness_policy,
)
from composition.verified_precheck_receipt import (
    VerifiedReceiptSnapshotOutcome,
    verify_and_snapshot_precheck_receipt,
)
from config.settings import RuntimePaperFastLoopSettings

__all__ = [
    "ActivationCandidateFreshnessPreflightOutcome",
    "ActivationCandidateFreshnessPreflightResult",
    "freshness_qualify_activation_candidate",
]


class ActivationCandidateFreshnessPreflightOutcome(StrEnum):
    PASS = "pass"
    NO_GO = "no_go"


@dataclass(frozen=True)
class ActivationCandidateFreshnessPreflightResult:
    """Freshness-qualified preflight verdict — not approval or activation.

    ``freshness_policy_evaluated`` is ``True`` only when a valid explicit policy was applied
  to a valid age observation (qualified PASS or ``candidate_receipt_stale``). Final-preflight
    short-circuits and evaluator defensive NO_GO keep it ``False``."""

    outcome: ActivationCandidateFreshnessPreflightOutcome
    reasons: tuple[str, ...]
    receipt_sha256: str | None
    market: str | None
    symbol: str | None
    final_preflight_result: ActivationCandidateFinalPreflightResult | None
    freshness_evaluation: ReceiptFreshnessEvaluation | None
    freshness_policy_evaluated: bool
    activation_authorized: bool
    runtime_activation_outcome: str
    explicit_operator_approval_required: bool
    writers_stopped_manual_confirmation_required: bool


def freshness_qualify_activation_candidate(
    *,
    settings: RuntimePaperFastLoopSettings,
    receipt_payload: object,
    now: datetime,
    policy: ReceiptFreshnessPolicy,
    base_dir: str | Path | None = None,
) -> ActivationCandidateFreshnessPreflightResult:
    """Run verified final preflight then explicit freshness evaluation.

    ``policy`` is required — no optional/default/config fallback. Processing order: policy
    snapshot → ``now`` guard → receipt snapshot → verified final core → freshness evaluation.
    Invalid policy short-circuits before ``now`` validation, snapshot, verifier, filesystem,
    or SQLite access."""

    # Step 1 — explicit policy strict validation + one-shot snapshot (caller policy frozen).
    policy_snapshot = snapshot_receipt_freshness_policy(policy)
    if policy_snapshot is None:
        return _freshness_no_go(
            reasons=("candidate_freshness_policy_invalid",),
            receipt_sha256=None,
            market=None,
            symbol=None,
            final_preflight_result=None,
            freshness_evaluation=None,
            freshness_policy_evaluated=False,
        )

    # Step 2 — strict ``now`` guard (shared with final-preflight wrapper/core).
    if final_preflight_now_is_invalid(now):
        return _freshness_no_go(
            reasons=("candidate_invalid_now",),
            receipt_sha256=None,
            market=None,
            symbol=None,
            final_preflight_result=None,
            freshness_evaluation=None,
            freshness_policy_evaluated=False,
        )

    # Step 3 — raw receipt를 strict detached snapshot으로 한 번만 변환.
    snapshot_result = verify_and_snapshot_precheck_receipt(receipt_payload)
    if snapshot_result.outcome is not VerifiedReceiptSnapshotOutcome.VALID:
        return _freshness_no_go(
            reasons=("candidate_receipt_invalid",),
            receipt_sha256=None,
            market=None,
            symbol=None,
            final_preflight_result=None,
            freshness_evaluation=None,
            freshness_policy_evaluated=False,
        )
    assert snapshot_result.receipt is not None

    # Step 4 — verified final-preflight core (verifier 0, raw payload 0).
    final_result = final_preflight_verified_activation_candidate(
        settings=settings,
        receipt=snapshot_result.receipt,
        now=now,
        base_dir=base_dir,
    )
    if final_result.outcome is not ActivationCandidateFinalPreflightOutcome.PASS:
        return _freshness_no_go(
            reasons=final_result.reasons,
            receipt_sha256=final_result.receipt_sha256,
            market=final_result.market,
            symbol=final_result.symbol,
            final_preflight_result=final_result,
            freshness_evaluation=None,
            freshness_policy_evaluated=False,
        )

    # Step 5 — explicit freshness evaluation on the same receipt-time assessment object.
    freshness_evaluation = evaluate_receipt_freshness(
        time_assessment=final_result.receipt_time_assessment,
        policy=policy_snapshot,
    )
    if freshness_evaluation.outcome is ReceiptFreshnessOutcome.FRESH:
        return ActivationCandidateFreshnessPreflightResult(
            outcome=ActivationCandidateFreshnessPreflightOutcome.PASS,
            reasons=(),
            receipt_sha256=final_result.receipt_sha256,
            market=final_result.market,
            symbol=final_result.symbol,
            final_preflight_result=final_result,
            freshness_evaluation=freshness_evaluation,
            freshness_policy_evaluated=True,
            activation_authorized=False,
            runtime_activation_outcome="no_go",
            explicit_operator_approval_required=True,
            writers_stopped_manual_confirmation_required=True,
        )

    if freshness_evaluation.outcome is ReceiptFreshnessOutcome.STALE:
        return _freshness_no_go(
            reasons=("candidate_receipt_stale",),
            receipt_sha256=final_result.receipt_sha256,
            market=final_result.market,
            symbol=final_result.symbol,
            final_preflight_result=final_result,
            freshness_evaluation=freshness_evaluation,
            freshness_policy_evaluated=True,
        )

    return _freshness_no_go(
        reasons=("candidate_freshness_evaluation_invalid",),
        receipt_sha256=final_result.receipt_sha256,
        market=final_result.market,
        symbol=final_result.symbol,
        final_preflight_result=final_result,
        freshness_evaluation=freshness_evaluation,
        freshness_policy_evaluated=False,
    )


def _freshness_no_go(
    *,
    reasons: tuple[str, ...],
    receipt_sha256: str | None,
    market: str | None,
    symbol: str | None,
    final_preflight_result: ActivationCandidateFinalPreflightResult | None,
    freshness_evaluation: ReceiptFreshnessEvaluation | None,
    freshness_policy_evaluated: bool,
) -> ActivationCandidateFreshnessPreflightResult:
    return ActivationCandidateFreshnessPreflightResult(
        outcome=ActivationCandidateFreshnessPreflightOutcome.NO_GO,
        reasons=reasons,
        receipt_sha256=receipt_sha256,
        market=market,
        symbol=symbol,
        final_preflight_result=final_preflight_result,
        freshness_evaluation=freshness_evaluation,
        freshness_policy_evaluated=freshness_policy_evaluated,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        explicit_operator_approval_required=True,
        writers_stopped_manual_confirmation_required=True,
    )
