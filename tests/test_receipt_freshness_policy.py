"""RTM-7c.4k — explicit receipt freshness policy evaluation (pure API) tests.

Verifies policy validation, inclusive max-age boundary, invalid time-assessment fail-close,
isolation (no clock/fs/network/env/DB), and that no implicit default threshold exists.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.activation_candidate_final_preflight import (
    final_preflight_activation_candidate,
)
from composition.receipt_freshness_policy import (
    ReceiptFreshnessOutcome,
    ReceiptFreshnessPolicy,
    evaluate_receipt_freshness,
)
from composition.receipt_time_assessment import (
    ReceiptTimeAssessment,
    ReceiptTimeAssessmentOutcome,
    assess_receipt_time,
)

import test_precheck_receipt_verifier as vrf_helper

_CHECKED_AT = "2026-06-16T00:30:00+00:00"
_CHECKED = datetime(2026, 6, 16, 0, 30, tzinfo=UTC)


def _receipt(checked_at: str = _CHECKED_AT) -> dict[str, Any]:
    return vrf_helper._valid_receipt(checked_at=checked_at)


def _valid_assessment(*, age_microseconds: int = 0) -> ReceiptTimeAssessment:
    now = _CHECKED + timedelta(microseconds=age_microseconds)
    result = assess_receipt_time(receipt_payload=_receipt(), now=now)
    assert result.outcome is ReceiptTimeAssessmentOutcome.VALID
    assert result.receipt_age_microseconds == age_microseconds
    return result


def _policy(max_age: int) -> ReceiptFreshnessPolicy:
    return ReceiptFreshnessPolicy(max_age_microseconds=max_age)


# --- policy validation ---


@pytest.mark.parametrize("max_age", [0, 1, 86_400_000_000])
def test_policy_accepts_zero_and_positive_int(max_age: int) -> None:
    result = evaluate_receipt_freshness(
        time_assessment=_valid_assessment(age_microseconds=0),
        policy=_policy(max_age),
    )
    assert result.outcome is ReceiptFreshnessOutcome.FRESH


@pytest.mark.parametrize(
    "invalid_max",
    [
        True,
        False,
        -1,
        -999,
        1.0,
        0.0,
        "1000",
        "0",
        object(),
        None,
    ],
    ids=[
        "bool_true",
        "bool_false",
        "negative_one",
        "negative_large",
        "float_pos",
        "float_zero",
        "str_pos",
        "str_zero",
        "object",
        "none",
    ],
)
def test_invalid_policy_is_no_go(invalid_max: object) -> None:
    policy = ReceiptFreshnessPolicy(max_age_microseconds=invalid_max)  # type: ignore[arg-type]
    result = evaluate_receipt_freshness(
        time_assessment=_valid_assessment(),
        policy=policy,
    )
    assert result.outcome is ReceiptFreshnessOutcome.NO_GO
    assert result.reasons == ("freshness_policy_invalid",)
    assert result.freshness_policy_evaluated is False
    assert result.max_age_microseconds is None
    assert result.receipt_age_microseconds is None
    assert result.activation_authorized is False
    assert result.runtime_activation_outcome == "no_go"


# --- inclusive boundary matrix ---


@pytest.mark.parametrize(
    ("age", "max_age", "expected_outcome", "expected_reasons"),
    [
        (0, 0, ReceiptFreshnessOutcome.FRESH, ()),
        (1, 0, ReceiptFreshnessOutcome.STALE, ("receipt_age_exceeds_policy",)),
        (100, 100, ReceiptFreshnessOutcome.FRESH, ()),
        (101, 100, ReceiptFreshnessOutcome.STALE, ("receipt_age_exceeds_policy",)),
        (999_999_999_999, 999_999_999_999, ReceiptFreshnessOutcome.FRESH, ()),
        (
            1_000_000_000_000,
            999_999_999_999,
            ReceiptFreshnessOutcome.STALE,
            ("receipt_age_exceeds_policy",),
        ),
    ],
)
def test_inclusive_boundary_matrix(
    age: int,
    max_age: int,
    expected_outcome: ReceiptFreshnessOutcome,
    expected_reasons: tuple[str, ...],
) -> None:
    result = evaluate_receipt_freshness(
        time_assessment=_valid_assessment(age_microseconds=age),
        policy=_policy(max_age),
    )
    assert result.outcome is expected_outcome
    assert result.reasons == expected_reasons
    assert result.receipt_age_microseconds == age
    assert result.max_age_microseconds == max_age
    assert result.freshness_policy_evaluated is True
    assert result.activation_authorized is False
    assert result.runtime_activation_outcome == "no_go"


# --- invalid time assessment matrix ---


def _synthetic_assessment(**overrides: Any) -> ReceiptTimeAssessment:
    base = _valid_assessment()
    fields = {f.name: getattr(base, f.name) for f in dataclasses.fields(ReceiptTimeAssessment)}
    fields.update(overrides)
    return ReceiptTimeAssessment(**fields)


@pytest.mark.parametrize(
    "overrides",
    [
        {"outcome": ReceiptTimeAssessmentOutcome.NO_GO, "reasons": ("receipt_time_in_future",)},
        {"outcome": ReceiptTimeAssessmentOutcome.NO_GO, "reasons": ("receipt_time_receipt_invalid",)},
        {"receipt_age_evaluated": False},
        {"receipt_age_microseconds": None},
        {"receipt_age_microseconds": -1},
        {"receipt_age_microseconds": True},
        {"receipt_age_microseconds": 1.5},
    ],
    ids=[
        "future_receipt_no_go",
        "invalid_receipt_no_go",
        "age_not_evaluated",
        "missing_age",
        "negative_age",
        "bool_age",
        "float_age",
    ],
)
def test_invalid_time_assessment_is_no_go(overrides: dict[str, Any]) -> None:
    assessment = _synthetic_assessment(**overrides)
    result = evaluate_receipt_freshness(time_assessment=assessment, policy=_policy(100))
    assert result.outcome is ReceiptFreshnessOutcome.NO_GO
    assert result.reasons == ("freshness_time_assessment_invalid",)
    assert result.freshness_policy_evaluated is False
    assert result.max_age_microseconds == 100
    assert result.receipt_age_microseconds is None
    assert result.activation_authorized is False


def test_future_receipt_assessment_from_real_path_is_no_go() -> None:
    assessment = assess_receipt_time(
        receipt_payload=_receipt(),
        now=_CHECKED - timedelta(microseconds=1),
    )
    assert assessment.outcome is ReceiptTimeAssessmentOutcome.NO_GO
    result = evaluate_receipt_freshness(time_assessment=assessment, policy=_policy(100))
    assert result.outcome is ReceiptFreshnessOutcome.NO_GO
    assert result.reasons == ("freshness_time_assessment_invalid",)
    assert result.freshness_policy_evaluated is False


# --- reason sanitization ---


def test_stale_reason_is_stable_and_sanitized() -> None:
    result = evaluate_receipt_freshness(
        time_assessment=_valid_assessment(age_microseconds=500),
        policy=_policy(100),
    )
    assert result.reasons == ("receipt_age_exceeds_policy",)
    for reason in result.reasons:
        assert _CHECKED_AT not in reason
        assert "RuntimeError" not in reason
        assert "Traceback" not in reason


# --- isolation ---


def test_module_reads_no_clock() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "receipt_freshness_policy.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("datetime.now", "datetime.utcnow", "time.time", "time.monotonic"):
        assert forbidden not in source


def test_module_has_no_implicit_default_threshold() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "receipt_freshness_policy.py"
    ).read_text(encoding="utf-8")
    assert "DEFAULT" not in source
    assert "os.environ" not in source
    assert "load_settings" not in source
    assert "open(" not in source
    assert "sqlite3" not in source
    assert "requests" not in source


def test_evaluate_does_not_recall_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    import composition.verified_precheck_receipt as snap_mod

    assessment = _valid_assessment(age_microseconds=10)

    def _fail_verifier(_: object) -> object:
        raise AssertionError("verifier must not run during freshness evaluation")

    monkeypatch.setattr(snap_mod, "verify_runtime_precheck_receipt_payload", _fail_verifier)
    result = evaluate_receipt_freshness(time_assessment=assessment, policy=_policy(100))
    assert result.outcome is ReceiptFreshnessOutcome.FRESH


def test_evaluate_module_imports_no_datetime_for_clock() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "receipt_freshness_policy.py"
    ).read_text(encoding="utf-8")
    assert "from datetime import" not in source
    assert "import datetime" not in source


def test_evaluation_dataclass_is_frozen() -> None:
    result = evaluate_receipt_freshness(
        time_assessment=_valid_assessment(),
        policy=_policy(0),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = ReceiptFreshnessOutcome.STALE  # type: ignore[misc]


def test_policy_dataclass_is_frozen() -> None:
    policy = _policy(100)
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.max_age_microseconds = 200  # type: ignore[misc]


# --- import guard: final preflight unchanged ---


def test_final_preflight_module_does_not_import_freshness_policy() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "activation_candidate_final_preflight.py"
    ).read_text(encoding="utf-8")
    assert "receipt_freshness_policy" not in source
    assert re.search(r"freshness_policy_evaluated\s*=\s*True", source) is None


def test_final_preflight_freshness_policy_evaluated_stays_false(tmp_path: Path) -> None:
    import test_activation_candidate_final_preflight as fp_helper
    import test_paper_fast_loop_composition as pfl_helper

    settings = fp_helper._settings(tmp_path)
    pfl_helper._seed_valid_stack(tmp_path, settings)
    receipt = fp_helper._pass_receipt_for_seeded_stack(tmp_path, settings)
    result = final_preflight_activation_candidate(
        settings=settings,
        receipt_payload=receipt,
        now=fp_helper._NOW,
        base_dir=tmp_path,
    )
    assert result.freshness_policy_evaluated is False


# --- carry-over hardening H1: wrong-object fail-closed ---


@pytest.mark.parametrize(
    "bad_policy",
    [None, object(), "100", 100],
    ids=["none", "object", "str", "int"],
)
def test_wrong_policy_object_is_no_go(bad_policy: object) -> None:
    result = evaluate_receipt_freshness(
        time_assessment=_valid_assessment(),
        policy=bad_policy,  # type: ignore[arg-type]
    )
    assert result.outcome is ReceiptFreshnessOutcome.NO_GO
    assert result.reasons == ("freshness_policy_invalid",)
    assert result.freshness_policy_evaluated is False


def test_policy_subclass_is_no_go() -> None:
    class SubPolicy(ReceiptFreshnessPolicy):
        pass

    result = evaluate_receipt_freshness(
        time_assessment=_valid_assessment(),
        policy=SubPolicy(max_age_microseconds=100),
    )
    assert result.outcome is ReceiptFreshnessOutcome.NO_GO
    assert result.reasons == ("freshness_policy_invalid",)


@pytest.mark.parametrize(
    "bad_assessment",
    [None, object(), "assessment"],
    ids=["none", "object", "str"],
)
def test_wrong_assessment_object_is_no_go(bad_assessment: object) -> None:
    result = evaluate_receipt_freshness(
        time_assessment=bad_assessment,  # type: ignore[arg-type]
        policy=_policy(100),
    )
    assert result.outcome is ReceiptFreshnessOutcome.NO_GO
    assert result.reasons == ("freshness_time_assessment_invalid",)
    assert result.max_age_microseconds == 100
    assert result.freshness_policy_evaluated is False


def test_assessment_subclass_is_no_go() -> None:
    class SubAssessment(ReceiptTimeAssessment):
        pass

    base = _valid_assessment()
    fields = {f.name: getattr(base, f.name) for f in dataclasses.fields(ReceiptTimeAssessment)}
    assessment = SubAssessment(**fields)
    result = evaluate_receipt_freshness(time_assessment=assessment, policy=_policy(100))
    assert result.outcome is ReceiptFreshnessOutcome.NO_GO
    assert result.reasons == ("freshness_time_assessment_invalid",)


def test_none_policy_and_assessment_do_not_escape_attribute_error() -> None:
    result = evaluate_receipt_freshness(
        time_assessment=None,  # type: ignore[arg-type]
        policy=None,  # type: ignore[arg-type]
    )
    assert result.outcome is ReceiptFreshnessOutcome.NO_GO
    assert result.reasons == ("freshness_policy_invalid",)


def test_malformed_exact_policy_missing_field_fail_closed() -> None:
    """H1 — exact-type instance with deleted field must not escape AttributeError."""

    from composition.receipt_freshness_policy import (
        receipt_freshness_policy_is_valid,
        snapshot_receipt_freshness_policy,
    )

    policy = ReceiptFreshnessPolicy(max_age_microseconds=100)
    object.__delattr__(policy, "max_age_microseconds")
    assert snapshot_receipt_freshness_policy(policy) is None
    assert receipt_freshness_policy_is_valid(policy) is False
    result = evaluate_receipt_freshness(
        time_assessment=_valid_assessment(),
        policy=policy,
    )
    assert result.outcome is ReceiptFreshnessOutcome.NO_GO
    assert result.reasons == ("freshness_policy_invalid",)
    assert result.freshness_policy_evaluated is False


# --- carry-over hardening H2: single-read observation ---


def test_evaluate_uses_single_read_locals_for_assessment_fields() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "receipt_freshness_policy.py"
    ).read_text(encoding="utf-8")
    for field in (
        "assessment_outcome = time_assessment.outcome",
        "assessment_reasons = time_assessment.reasons",
        "age_evaluated = time_assessment.receipt_age_evaluated",
        "age = time_assessment.receipt_age_microseconds",
        "policy_already_evaluated = time_assessment.freshness_policy_evaluated",
        "checked_at = time_assessment.receipt_checked_at",
    ):
        assert field in source


# --- carry-over hardening H3: contradictory VALID assessment ---


@pytest.mark.parametrize(
    "overrides",
    [
        {"reasons": ("unexpected",)},
        {"freshness_policy_evaluated": True},
        {"receipt_checked_at": None},
        {"receipt_checked_at": 123},
        {"receipt_age_evaluated": False},
        {"receipt_age_microseconds": None},
        {"receipt_age_microseconds": -1},
        {"outcome": ReceiptTimeAssessmentOutcome.NO_GO, "reasons": ("receipt_time_in_future",)},
    ],
    ids=[
        "nonempty_reasons",
        "policy_already_evaluated",
        "missing_checked_at",
        "non_string_checked_at",
        "age_not_evaluated",
        "missing_age",
        "negative_age",
        "no_go_assessment",
    ],
)
def test_contradictory_valid_assessment_is_no_go(overrides: dict[str, Any]) -> None:
    assessment = _synthetic_assessment(**overrides)
    result = evaluate_receipt_freshness(time_assessment=assessment, policy=_policy(100))
    assert result.outcome is ReceiptFreshnessOutcome.NO_GO
    assert result.reasons == ("freshness_time_assessment_invalid",)
    assert result.freshness_policy_evaluated is False


def test_legitimate_4i_assessment_still_passes_freshness() -> None:
    assessment = assess_receipt_time(receipt_payload=_receipt(), now=_CHECKED)
    result = evaluate_receipt_freshness(time_assessment=assessment, policy=_policy(1_000_000))
    assert result.outcome is ReceiptFreshnessOutcome.FRESH
    assert result.freshness_policy_evaluated is True
