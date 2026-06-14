"""RTM-7c.4i — policy-neutral receipt time observation (pure API) tests.

Verifies exact integer receipt-age microseconds, future-receipt fail-close, strict ``now``
validation, verifier reuse, and that NO max-age / TTL / freshness threshold is applied and
no raw ``checked_at`` / exception value leaks into a reason code.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.receipt_time_assessment import (
    ReceiptTimeAssessmentOutcome,
    assess_receipt_time,
)

import test_precheck_receipt_verifier as vrf_helper

_CHECKED_AT = "2026-06-16T00:30:00+00:00"
_CHECKED = datetime(2026, 6, 16, 0, 30, tzinfo=UTC)
_KST = timezone(timedelta(hours=9))


def _receipt(checked_at: str = _CHECKED_AT) -> dict[str, Any]:
    return vrf_helper._valid_receipt(checked_at=checked_at)


# --- valid age computation ---


def test_assess_age_zero_when_now_equals_checked_at() -> None:
    result = assess_receipt_time(receipt_payload=_receipt(), now=_CHECKED)
    assert result.outcome is ReceiptTimeAssessmentOutcome.VALID
    assert result.reasons == ()
    assert result.receipt_age_microseconds == 0
    assert result.receipt_age_evaluated is True
    assert result.freshness_policy_evaluated is False
    assert result.receipt_checked_at == _CHECKED_AT


def test_assess_age_exact_microseconds_in_past() -> None:
    now = _CHECKED + timedelta(days=1, seconds=2, microseconds=3)
    result = assess_receipt_time(receipt_payload=_receipt(), now=now)
    assert result.outcome is ReceiptTimeAssessmentOutcome.VALID
    # exact integer microseconds, not a float total_seconds rounding.
    assert result.receipt_age_microseconds == (86_400 + 2) * 1_000_000 + 3


def test_assess_age_accepts_kst_aware_now() -> None:
    # same instant expressed in KST → identical age.
    now_kst = (_CHECKED + timedelta(hours=1)).astimezone(_KST)
    result = assess_receipt_time(receipt_payload=_receipt(), now=now_kst)
    assert result.outcome is ReceiptTimeAssessmentOutcome.VALID
    assert result.receipt_age_microseconds == 3_600 * 1_000_000


def test_assess_age_with_offset_checked_at_is_instant_based() -> None:
    # receipt checked_at written in KST; now in UTC, one hour later in absolute time.
    receipt = _receipt("2026-06-16T09:30:00+09:00")  # == 00:30:00Z
    result = assess_receipt_time(receipt_payload=receipt, now=_CHECKED + timedelta(hours=1))
    assert result.outcome is ReceiptTimeAssessmentOutcome.VALID
    assert result.receipt_age_microseconds == 3_600 * 1_000_000


# --- future receipt fail-close ---


def test_assess_future_receipt_is_no_go() -> None:
    result = assess_receipt_time(
        receipt_payload=_receipt(), now=_CHECKED - timedelta(microseconds=1)
    )
    assert result.outcome is ReceiptTimeAssessmentOutcome.NO_GO
    assert result.reasons == ("receipt_time_in_future",)
    assert result.receipt_age_microseconds is None
    assert result.receipt_age_evaluated is True  # comparison happened
    assert result.freshness_policy_evaluated is False


# --- receipt invalidity ---


def test_assess_rejects_invalid_receipt() -> None:
    result = assess_receipt_time(receipt_payload={"schema_version": 1}, now=_CHECKED)
    assert result.outcome is ReceiptTimeAssessmentOutcome.NO_GO
    assert result.reasons == ("receipt_time_receipt_invalid",)
    assert result.receipt_age_evaluated is False
    assert result.receipt_checked_at is None


def test_assess_rejects_non_object_receipt() -> None:
    result = assess_receipt_time(receipt_payload="not-a-receipt", now=_CHECKED)
    assert result.reasons == ("receipt_time_receipt_invalid",)
    assert result.receipt_age_evaluated is False


# --- strict now validation ---


class _RaisingTzInfo(tzinfo):
    def utcoffset(self, dt: datetime | None) -> Any:
        raise ValueError("boom")

    def tzname(self, dt: datetime | None) -> str | None:
        return None

    def dst(self, dt: datetime | None) -> Any:
        return None


class _NoneOffsetTzInfo(tzinfo):
    def utcoffset(self, dt: datetime | None) -> Any:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None

    def dst(self, dt: datetime | None) -> Any:
        return None


@pytest.mark.parametrize(
    "bad_now",
    [
        None,
        "2026-06-16T00:30:00+00:00",
        1718498400,
        datetime(2026, 6, 16, 0, 30),  # naive  # noqa: DTZ001
        datetime(2026, 6, 16, 0, 30, tzinfo=_RaisingTzInfo()),
        datetime(2026, 6, 16, 0, 30, tzinfo=_NoneOffsetTzInfo()),
    ],
    ids=["none", "str", "int", "naive", "raising_tz", "none_offset"],
)
def test_assess_rejects_invalid_now(bad_now: object) -> None:
    result = assess_receipt_time(receipt_payload=_receipt(), now=bad_now)  # type: ignore[arg-type]
    assert result.outcome is ReceiptTimeAssessmentOutcome.NO_GO
    assert result.reasons == ("receipt_time_invalid_now",)
    assert result.receipt_age_evaluated is False
    assert result.receipt_age_microseconds is None
    assert result.receipt_checked_at is None


# --- no raw value / threshold leakage ---


def test_assess_reasons_never_contain_raw_checked_at() -> None:
    # even a huge age must not surface the raw checked_at string or any threshold verdict.
    far = _CHECKED + timedelta(days=3650)
    result = assess_receipt_time(receipt_payload=_receipt(), now=far)
    assert result.outcome is ReceiptTimeAssessmentOutcome.VALID
    assert result.reasons == ()
    assert result.freshness_policy_evaluated is False  # no TTL/max-age decision ever


def test_assess_does_not_read_a_clock() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "receipt_time_assessment.py"
    ).read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "time.time" not in source
    assert "time.monotonic" not in source
