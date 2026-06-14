"""Explicit receipt freshness policy evaluation (RTM-7c.4k).

Compares an already-computed ``ReceiptTimeAssessment`` against a **caller-supplied**
``ReceiptFreshnessPolicy``. It decides whether the observed receipt age is within the
explicit max-age bound — it does **not** select a threshold, bind config/CLI defaults,
consume Operator approval, assert writer-stop, or authorize activation.

No clock read, no receipt payload/snapshot re-read, no verifier re-call, and no config/env
access. ``activation_authorized`` and ``runtime_activation_outcome`` are constant NO-GO on
every path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from composition.receipt_time_assessment import (
    ReceiptTimeAssessment,
    ReceiptTimeAssessmentOutcome,
)

__all__ = [
    "ReceiptFreshnessOutcome",
    "ReceiptFreshnessPolicy",
    "ReceiptFreshnessEvaluation",
    "evaluate_receipt_freshness",
]


class ReceiptFreshnessOutcome(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    NO_GO = "no_go"


@dataclass(frozen=True)
class ReceiptFreshnessPolicy:
    """Explicit max-age policy — no module-level default, no config binding.

    ``max_age_microseconds`` must be an exact built-in ``int`` (``bool`` rejected), ``>= 0``.
    Inclusive boundary: ``receipt_age_microseconds <= max_age_microseconds`` → FRESH."""

    max_age_microseconds: int


@dataclass(frozen=True)
class ReceiptFreshnessEvaluation:
    """Freshness verdict against an explicit policy — not approval or activation.

    ``freshness_policy_evaluated`` is ``True`` only when a valid policy was applied to a
    valid age observation (FRESH or STALE). ``activation_authorized`` is constant ``False``."""

    outcome: ReceiptFreshnessOutcome
    reasons: tuple[str, ...]
    receipt_age_microseconds: int | None
    max_age_microseconds: int | None
    freshness_policy_evaluated: bool
    activation_authorized: bool
    runtime_activation_outcome: str


def evaluate_receipt_freshness(
    *,
    time_assessment: ReceiptTimeAssessment,
    policy: ReceiptFreshnessPolicy,
) -> ReceiptFreshnessEvaluation:
    """Pure freshness evaluation — consumes an existing age observation only.

    Processing order: policy validation → time-assessment validity → age shape → inclusive
    max-age comparison. No new clock read and no receipt re-read."""

    if not _max_age_is_valid(policy.max_age_microseconds):
        return _no_go(
            reason="freshness_policy_invalid",
            receipt_age_microseconds=None,
            max_age_microseconds=None,
            freshness_policy_evaluated=False,
        )

    max_age = policy.max_age_microseconds

    if time_assessment.outcome is not ReceiptTimeAssessmentOutcome.VALID:
        return _no_go(
            reason="freshness_time_assessment_invalid",
            receipt_age_microseconds=None,
            max_age_microseconds=max_age,
            freshness_policy_evaluated=False,
        )

    if not time_assessment.receipt_age_evaluated:
        return _no_go(
            reason="freshness_time_assessment_invalid",
            receipt_age_microseconds=None,
            max_age_microseconds=max_age,
            freshness_policy_evaluated=False,
        )

    age = time_assessment.receipt_age_microseconds
    if not _receipt_age_is_valid(age):
        return _no_go(
            reason="freshness_time_assessment_invalid",
            receipt_age_microseconds=None,
            max_age_microseconds=max_age,
            freshness_policy_evaluated=False,
        )

    assert age is not None  # _receipt_age_is_valid guarantees non-None

    if age <= max_age:
        return ReceiptFreshnessEvaluation(
            outcome=ReceiptFreshnessOutcome.FRESH,
            reasons=(),
            receipt_age_microseconds=age,
            max_age_microseconds=max_age,
            freshness_policy_evaluated=True,
            activation_authorized=False,
            runtime_activation_outcome="no_go",
        )

    return ReceiptFreshnessEvaluation(
        outcome=ReceiptFreshnessOutcome.STALE,
        reasons=("receipt_age_exceeds_policy",),
        receipt_age_microseconds=age,
        max_age_microseconds=max_age,
        freshness_policy_evaluated=True,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
    )


def _max_age_is_valid(value: object) -> bool:
    return type(value) is int and not isinstance(value, bool) and value >= 0


def _receipt_age_is_valid(value: object) -> bool:
    return type(value) is int and not isinstance(value, bool) and value >= 0


def _no_go(
    *,
    reason: str,
    receipt_age_microseconds: int | None,
    max_age_microseconds: int | None,
    freshness_policy_evaluated: bool,
) -> ReceiptFreshnessEvaluation:
    return ReceiptFreshnessEvaluation(
        outcome=ReceiptFreshnessOutcome.NO_GO,
        reasons=(reason,),
        receipt_age_microseconds=receipt_age_microseconds,
        max_age_microseconds=max_age_microseconds,
        freshness_policy_evaluated=freshness_policy_evaluated,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
    )
