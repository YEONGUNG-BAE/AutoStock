"""Policy-neutral receipt time observation (RTM-7c.4i).

Observes the time relationship between a verified precheck receipt's ``checked_at`` and a
caller-supplied ``now``. It computes the exact receipt age and fail-closes a future
``checked_at``; it deliberately does **not** select any max-age / TTL threshold, does not
evaluate any freshness policy (``freshness_policy_evaluated`` is constant ``False``), does
not consume an Operator approval, does not assert writer-stop, and authorizes nothing.

``now`` is supplied by the caller and must be a timezone-aware ``datetime``; this module
reads no clock of its own. It reuses the existing precheck receipt verifier — it builds no
new canonical verifier and no new JSON parser — and never echoes the raw ``checked_at``
string (or any other raw payload value) into a reason code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from composition.precheck_receipt_verifier import (
    ReceiptVerificationOutcome,
    verify_runtime_precheck_receipt_payload,
)

__all__ = [
    "ReceiptTimeAssessmentOutcome",
    "ReceiptTimeAssessment",
    "assess_receipt_time",
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
    """Observe the time relationship between a verified receipt and the caller ``now``.

    Order: strict ``now`` guard → verifier reuse → aware ``checked_at`` parse → future
    fail-close → exact age. Returns stable reason codes only; no raw ``checked_at`` or
    exception value leaks. Evaluates no max-age/TTL/freshness threshold."""

    if _now_is_invalid(now):
        return _no_go("receipt_time_invalid_now")

    verification = verify_runtime_precheck_receipt_payload(receipt_payload)
    if verification.outcome is not ReceiptVerificationOutcome.VALID:
        return _no_go("receipt_time_receipt_invalid")

    # VALID guarantees receipt_payload is a dict with a structurally valid aware checked_at,
    # but parse defensively so a malformed value can never raise out of this API.
    assert isinstance(receipt_payload, dict)
    checked_at_raw = receipt_payload["checked_at"]
    checked_at = _parse_aware(checked_at_raw)
    if checked_at is None:
        return _no_go("receipt_time_invalid_checked_at")

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


def _parse_aware(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


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
