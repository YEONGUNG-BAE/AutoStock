"""Policy-neutral receipt time observation (RTM-7c.4i, +7c.4j snapshot).

Observes the time relationship between a verified precheck receipt's ``checked_at`` and a
caller-supplied ``now``. It computes the exact receipt age and fail-closes a future
``checked_at``; it deliberately does **not** select any max-age / TTL threshold, does not
evaluate any freshness policy (``freshness_policy_evaluated`` is constant ``False``), does
not consume an Operator approval, does not assert writer-stop, and authorizes nothing.

``now`` is supplied by the caller and must be a timezone-aware ``datetime``; this module
reads no clock of its own.

RTM-7c.4j: ``assess_receipt_time`` is a raw-payload wrapper that builds an immutable verified
snapshot once and delegates to ``assess_verified_receipt_time``. The snapshot-based core runs
no verifier and performs no ``checked_at`` parse — it reads the already-aware ``datetime`` off
the immutable snapshot. The verifier-VALID schema already guarantees a parseable aware
``checked_at``, so a malformed ``checked_at`` is absorbed at snapshot-build time as
``receipt_time_receipt_invalid``; there is no separate ``receipt_time_invalid_checked_at``
reason (it was unreachable after a VALID verification).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from composition.verified_precheck_receipt import (
    VerifiedPrecheckReceipt,
    VerifiedReceiptSnapshotOutcome,
    verify_and_snapshot_precheck_receipt,
)

__all__ = [
    "ReceiptTimeAssessmentOutcome",
    "ReceiptTimeAssessment",
    "assess_receipt_time",
    "assess_verified_receipt_time",
]


class ReceiptTimeAssessmentOutcome(StrEnum):
    VALID = "valid"
    NO_GO = "no_go"


@dataclass(frozen=True)
class ReceiptTimeAssessment:
    """Policy-neutral receipt time observation verdict — receipt 원문 payload 미보관.

    ``receipt_age_microseconds`` is the **exact** integer microseconds of ``now - checked_at``
    (computed from ``timedelta.days/seconds/microseconds``, never ``total_seconds`` float); it
    is ``>= 0`` on a VALID outcome and ``None`` otherwise. ``receipt_age_evaluated`` is ``True``
    once a verified receipt's ``checked_at`` has been compared against ``now`` (VALID or
    future-receipt NO_GO), ``False`` for the pre-comparison fail-closed paths.
    ``freshness_policy_evaluated`` is constant ``False`` — this lane evaluates no freshness
    threshold, TTL, or max-age."""

    outcome: ReceiptTimeAssessmentOutcome
    reasons: tuple[str, ...]
    receipt_checked_at: str | None
    receipt_age_microseconds: int | None
    receipt_age_evaluated: bool
    freshness_policy_evaluated: bool


def assess_receipt_time(
    *,
    receipt_payload: object,
    now: datetime,
) -> ReceiptTimeAssessment:
    """Raw-payload wrapper: strict ``now`` guard → verify/snapshot once → snapshot-based core.

    Order preserves the 4i precedence: an invalid ``now`` fail-closes before the receipt is
    even verified. Returns stable reason codes only; no raw ``checked_at`` or exception value
    leaks. Evaluates no max-age/TTL/freshness threshold."""

    if _now_is_invalid(now):
        return _no_go("receipt_time_invalid_now")

    snapshot_result = verify_and_snapshot_precheck_receipt(receipt_payload)
    if snapshot_result.outcome is not VerifiedReceiptSnapshotOutcome.VALID:
        return _no_go("receipt_time_receipt_invalid")
    assert snapshot_result.receipt is not None

    return assess_verified_receipt_time(receipt=snapshot_result.receipt, now=now)


def assess_verified_receipt_time(
    *,
    receipt: VerifiedPrecheckReceipt,
    now: datetime,
) -> ReceiptTimeAssessment:
    """Snapshot-based core: compare the immutable snapshot's aware ``checked_at`` to ``now``.

    No verifier call, no ``checked_at`` parse — the snapshot already carries a timezone-aware
    ``datetime``. Future ``checked_at`` fail-closes; otherwise the exact integer age is
    returned. No max-age/TTL/freshness threshold is applied."""

    if _now_is_invalid(now):
        return _no_go("receipt_time_invalid_now")

    checked_at = receipt.checked_at
    checked_at_raw = receipt.checked_at_iso

    if checked_at > now:
        # future receipt: comparison happened (age evaluated) but no non-negative age exists.
        return ReceiptTimeAssessment(
            outcome=ReceiptTimeAssessmentOutcome.NO_GO,
            reasons=("receipt_time_in_future",),
            receipt_checked_at=checked_at_raw,
            receipt_age_microseconds=None,
            receipt_age_evaluated=True,
            freshness_policy_evaluated=False,
        )

    return ReceiptTimeAssessment(
        outcome=ReceiptTimeAssessmentOutcome.VALID,
        reasons=(),
        receipt_checked_at=checked_at_raw,
        receipt_age_microseconds=_exact_age_microseconds(now=now, checked_at=checked_at),
        receipt_age_evaluated=True,
        freshness_policy_evaluated=False,
    )


def _exact_age_microseconds(*, now: datetime, checked_at: datetime) -> int:
    """Exact integer microseconds of ``now - checked_at`` (no float ``total_seconds``)."""

    delta = now - checked_at
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _now_is_invalid(now: object) -> bool:
    if not isinstance(now, datetime):
        return True
    try:
        offset = now.utcoffset()
    except Exception:
        return True
    return offset is None


def _no_go(reason: str) -> ReceiptTimeAssessment:
    return ReceiptTimeAssessment(
        outcome=ReceiptTimeAssessmentOutcome.NO_GO,
        reasons=(reason,),
        receipt_checked_at=None,
        receipt_age_microseconds=None,
        receipt_age_evaluated=False,
        freshness_policy_evaluated=False,
    )
