"""RTM-7c.4n — canonical freshness-qualified candidate evidence tests.

Covers evidence consistency and fail-closed closure: nested identity/age/posture
consistency, evaluated_at↔observed-age exact binding, combined fail-closed outcome, and
single-execution composition. Evidence is created only for a freshness-qualified PASS whose
nested final/freshness/time-assessment observations all agree; any mismatch fails closed."""

from __future__ import annotations

import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition import activation_candidate_evidence as evidence_mod
from composition.activation_candidate_evidence import (
    ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
    ActivationCandidateEvidenceOutcome,
    FreshnessQualifiedEvidenceOutcome,
    build_activation_candidate_evidence,
    freshness_qualify_and_build_candidate_evidence,
)
from composition.activation_candidate_final_preflight import (
    ActivationCandidateFinalPreflightOutcome,
    ActivationCandidateFinalPreflightResult,
)
from composition.activation_candidate_freshness_preflight import (
    ActivationCandidateFreshnessPreflightOutcome,
    ActivationCandidateFreshnessPreflightResult,
)
from composition.receipt_freshness_policy import (
    ReceiptFreshnessEvaluation,
    ReceiptFreshnessOutcome,
    ReceiptFreshnessPolicy,
)
from composition.receipt_time_assessment import (
    ReceiptTimeAssessment,
    ReceiptTimeAssessmentOutcome,
)
from decision.canonical_json import payload_sha256

import test_activation_candidate_freshness_preflight as fr_helper

_KST = timezone(timedelta(hours=9))
_EVAL_AT = datetime(2026, 6, 14, 12, 0, 0, tzinfo=_KST)
_SHA = "a" * 64
_AGE = 1000
_MAX = 300_000_000


# --- synthetic result builders (fast, no filesystem) — fully consistent by default ---


def _time_assessment(
    *,
    age: int = _AGE,
    checked_at: str | None = None,
    outcome: ReceiptTimeAssessmentOutcome = ReceiptTimeAssessmentOutcome.VALID,
    reasons: tuple[str, ...] = (),
    age_evaluated: bool = True,
    evaluated_at: datetime = _EVAL_AT,
) -> ReceiptTimeAssessment:
    if checked_at is None:
        checked_at = (evaluated_at - timedelta(microseconds=age)).isoformat()
    return ReceiptTimeAssessment(
        outcome=outcome,
        reasons=reasons,
        receipt_checked_at=checked_at,
        receipt_age_microseconds=age,
        receipt_age_evaluated=age_evaluated,
        freshness_policy_evaluated=False,
    )


def _final_pass(
    *, age: int = _AGE, evaluated_at: datetime = _EVAL_AT, ta: Any = ..., **overrides: Any
) -> ActivationCandidateFinalPreflightResult:
    base = dict(
        outcome=ActivationCandidateFinalPreflightOutcome.PASS,
        receipt_sha256=_SHA,
        market="KR",
        symbol="005930",
        reasons=(),
        revalidation_result=None,
        current_precheck_result=None,
        fresh_precheck_executed=True,
        receipt_age_evaluated=True,
        receipt_age_microseconds=age,
        receipt_time_assessment=(
            _time_assessment(age=age, evaluated_at=evaluated_at) if ta is ... else ta
        ),
        freshness_policy_evaluated=False,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        explicit_operator_approval_required=True,
        writers_stopped_manual_confirmation_required=True,
    )
    base.update(overrides)
    return ActivationCandidateFinalPreflightResult(**base)


def _fresh_eval(
    *,
    age: int = _AGE,
    max_age: int = _MAX,
    outcome: ReceiptFreshnessOutcome = ReceiptFreshnessOutcome.FRESH,
    policy_evaluated: bool = True,
    reasons: Any = ...,
) -> ReceiptFreshnessEvaluation:
    if reasons is ...:
        reasons = ()
        if outcome is ReceiptFreshnessOutcome.STALE:
            reasons = ("receipt_age_exceeds_policy",)
    return ReceiptFreshnessEvaluation(
        outcome=outcome,
        reasons=reasons,
        receipt_age_microseconds=age,
        max_age_microseconds=max_age,
        freshness_policy_evaluated=policy_evaluated,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
    )


def _qualified_pass(
    *,
    age: int = _AGE,
    max_age: int = _MAX,
    evaluated_at: datetime = _EVAL_AT,
    final: Any = ...,
    fresh: Any = ...,
    **overrides: Any,
) -> ActivationCandidateFreshnessPreflightResult:
    base = dict(
        outcome=ActivationCandidateFreshnessPreflightOutcome.PASS,
        reasons=(),
        receipt_sha256=_SHA,
        market="KR",
        symbol="005930",
        final_preflight_result=(
            _final_pass(age=age, evaluated_at=evaluated_at) if final is ... else final
        ),
        freshness_evaluation=(
            _fresh_eval(age=age, max_age=max_age) if fresh is ... else fresh
        ),
        freshness_policy_evaluated=True,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        explicit_operator_approval_required=True,
        writers_stopped_manual_confirmation_required=True,
    )
    base.update(overrides)
    return ActivationCandidateFreshnessPreflightResult(**base)


def _build(qualified: Any = ..., evaluated_at: datetime = _EVAL_AT) -> Any:
    return build_activation_candidate_evidence(
        qualified_result=_qualified_pass() if qualified is ... else qualified,
        evaluated_at=evaluated_at,
    )


# --- eligibility: CREATED ---


def test_qualified_pass_fresh_creates_evidence() -> None:
    result = _build()
    assert result.outcome is ActivationCandidateEvidenceOutcome.CREATED
    assert result.reasons == ()
    ev = result.evidence
    assert ev is not None
    assert ev.schema_version == ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION
    assert ev.evaluated_at == _EVAL_AT.isoformat()
    assert ev.receipt_sha256 == _SHA
    assert ev.market == "KR"
    assert ev.symbol == "005930"
    assert ev.max_age_microseconds == _MAX
    assert ev.receipt_age_microseconds == _AGE
    assert ev.final_preflight_outcome == "pass"
    assert ev.freshness_outcome == "fresh"
    assert ev.fresh_precheck_executed is True
    assert ev.receipt_age_evaluated is True
    assert ev.freshness_policy_evaluated is True
    assert ev.activation_authorized is False
    assert ev.runtime_activation_outcome == "no_go"
    assert evidence_mod._is_lower_hex64(ev.evidence_sha256)


def test_inclusive_boundary_age_equals_max_creates_evidence() -> None:
    result = _build(_qualified_pass(age=100, max_age=100))
    assert result.outcome is ActivationCandidateEvidenceOutcome.CREATED


# --- eligibility: NOT_ELIGIBLE (well-formed non-PASS) ---


def test_stale_no_go_is_not_eligible() -> None:
    result = _build(
        _qualified_pass(
            outcome=ActivationCandidateFreshnessPreflightOutcome.NO_GO,
            reasons=("candidate_receipt_stale",),
            fresh=_fresh_eval(age=500, max_age=100, outcome=ReceiptFreshnessOutcome.STALE),
        )
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.NOT_ELIGIBLE
    assert result.reasons == ("candidate_evidence_not_eligible",)
    assert result.evidence is None


def test_final_preflight_no_go_is_not_eligible() -> None:
    result = _build(
        _qualified_pass(
            outcome=ActivationCandidateFreshnessPreflightOutcome.NO_GO,
            reasons=("candidate_current_precheck:x",),
            final=None,
            fresh=None,
            freshness_policy_evaluated=False,
        )
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.NOT_ELIGIBLE
    assert result.evidence is None


# --- eligibility: INVALID (wrong object / bad evaluated_at) ---


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(object(), id="arbitrary_object"),
        pytest.param(None, id="none"),
        pytest.param("pass", id="str"),
        pytest.param({"outcome": "pass"}, id="dict"),
    ],
)
def test_wrong_object_result_is_invalid(bad: object) -> None:
    result = build_activation_candidate_evidence(qualified_result=bad, evaluated_at=_EVAL_AT)
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID
    assert result.reasons == ("candidate_evidence_invalid_input",)
    assert result.evidence is None


def test_result_subclass_is_invalid() -> None:
    class _Sub(ActivationCandidateFreshnessPreflightResult):
        pass

    sub = _Sub(**asdict(_qualified_pass()))  # type: ignore[arg-type]
    result = build_activation_candidate_evidence(qualified_result=sub, evaluated_at=_EVAL_AT)
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID


@pytest.mark.parametrize(
    "invalid_now",
    [
        pytest.param(datetime(2026, 6, 14, 12, 0, 0), id="naive"),  # noqa: DTZ001
        pytest.param("2026-06-14T12:00:00+09:00", id="str"),
        pytest.param(None, id="none"),
        pytest.param(1718000000, id="int"),
    ],
)
def test_invalid_evaluated_at_is_invalid(invalid_now: object) -> None:
    result = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(), evaluated_at=invalid_now  # type: ignore[arg-type]
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID
    assert result.evidence is None


# --- eligibility: INVALID (contradictory outer PASS) ---


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"reasons": ("unexpected",)}, id="pass_with_reasons"),
        pytest.param({"activation_authorized": True}, id="activation_true"),
        pytest.param({"runtime_activation_outcome": "go"}, id="runtime_go"),
        pytest.param({"freshness_policy_evaluated": False}, id="policy_not_evaluated"),
        pytest.param({"final": None}, id="no_final_result"),
        pytest.param({"fresh": None}, id="no_freshness_eval"),
        pytest.param({"explicit_operator_approval_required": False}, id="outer_approval_false"),
        pytest.param(
            {"writers_stopped_manual_confirmation_required": False},
            id="outer_writers_false",
        ),
    ],
)
def test_contradictory_outer_pass_is_invalid(overrides: dict[str, Any]) -> None:
    result = _build(_qualified_pass(**overrides))
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID
    assert result.evidence is None


# --- nested consistency matrix (P1 closure) ---


def test_outer_final_receipt_hash_mismatch_is_invalid() -> None:
    # outer sha "a"*64, final sha "b"*64
    bad_final = _final_pass(receipt_sha256="b" * 64)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_outer_final_market_mismatch_is_invalid() -> None:
    bad_final = _final_pass(market="US")
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_outer_final_symbol_mismatch_is_invalid() -> None:
    bad_final = _final_pass(symbol="000660")
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_final_freshness_age_mismatch_is_invalid() -> None:
    result = _build(
        _qualified_pass(final=_final_pass(age=_AGE), fresh=_fresh_eval(age=2000, max_age=3000))
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID


def test_time_assessment_final_age_mismatch_is_invalid() -> None:
    bad_final = _final_pass(age=_AGE, ta=_time_assessment(age=2000))
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_final_policy_evaluated_true_is_invalid() -> None:
    bad_final = _final_pass(freshness_policy_evaluated=True)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_freshness_reasons_nonempty_is_invalid() -> None:
    bad_fresh = _fresh_eval(reasons=("unexpected",))
    assert _build(_qualified_pass(fresh=bad_fresh)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


# --- final PASS semantic consistency: reasons must be () (RTM-7c.4n nested closure) ---


def test_final_pass_with_nonempty_reasons_is_invalid() -> None:
    bad_final = _final_pass(reasons=("candidate_current_precheck:expired",))
    result = _build(_qualified_pass(final=bad_final))
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID
    assert result.reasons == ("candidate_evidence_invalid_input",)
    assert result.evidence is None
    # The raw final reason is never surfaced in the stable evidence reason.
    assert "expired" not in result.reasons[0]


@pytest.mark.parametrize(
    "reasons",
    [
        pytest.param([], id="final_reasons_empty_list"),
        pytest.param(None, id="final_reasons_none"),
        pytest.param(["x"], id="final_reasons_nonempty_list"),
        pytest.param(object(), id="final_reasons_arbitrary_object"),
    ],
)
def test_final_pass_non_empty_tuple_reasons_is_invalid(reasons: object) -> None:
    bad_final = _final_pass(reasons=reasons)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_final_pass_with_deleted_reasons_is_invalid() -> None:
    bad_final = _final_pass()
    object.__delattr__(bad_final, "reasons")
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


# --- policy-neutral time assessment: freshness_policy_evaluated must be False ---


@pytest.mark.parametrize(
    "flag",
    [
        pytest.param(True, id="ta_policy_true"),
        pytest.param(0, id="ta_policy_zero"),
        pytest.param(1, id="ta_policy_one"),
        pytest.param(None, id="ta_policy_none"),
        pytest.param("false", id="ta_policy_string"),
    ],
)
def test_time_assessment_policy_evaluated_not_false_is_invalid(flag: object) -> None:
    bad_ta = replace(_time_assessment(), freshness_policy_evaluated=flag)
    bad_final = _final_pass(ta=bad_ta)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_time_assessment_with_deleted_policy_flag_is_invalid() -> None:
    bad_ta = _time_assessment()
    object.__delattr__(bad_ta, "freshness_policy_evaluated")
    bad_final = _final_pass(ta=bad_ta)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


@pytest.mark.parametrize(
    "final_overrides",
    [
        pytest.param({"activation_authorized": True}, id="final_activation_true"),
        pytest.param({"runtime_activation_outcome": "go"}, id="final_runtime_go"),
        pytest.param({"explicit_operator_approval_required": False}, id="final_approval_false"),
        pytest.param(
            {"writers_stopped_manual_confirmation_required": False},
            id="final_writers_false",
        ),
    ],
)
def test_nested_final_posture_mismatch_is_invalid(final_overrides: dict[str, Any]) -> None:
    bad_final = _final_pass(**final_overrides)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


@pytest.mark.parametrize(
    "fresh_overrides",
    [
        pytest.param({"activation_authorized": True}, id="fresh_activation_true"),
        pytest.param({"runtime_activation_outcome": "go"}, id="fresh_runtime_go"),
    ],
)
def test_nested_freshness_posture_mismatch_is_invalid(fresh_overrides: dict[str, Any]) -> None:
    bad_fresh = replace(_fresh_eval(), **fresh_overrides)
    assert _build(_qualified_pass(fresh=bad_fresh)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


@pytest.mark.parametrize(
    "bad_final",
    [
        pytest.param(object(), id="final_arbitrary_object"),
        pytest.param("final", id="final_str"),
    ],
)
def test_nested_final_wrong_object_is_invalid(bad_final: object) -> None:
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_nested_final_subclass_is_invalid() -> None:
    class _SubFinal(ActivationCandidateFinalPreflightResult):
        pass

    sub = _SubFinal(**asdict(_final_pass()))  # type: ignore[arg-type]
    assert _build(_qualified_pass(final=sub)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_nested_freshness_wrong_object_is_invalid() -> None:
    assert _build(_qualified_pass(fresh=object())).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_time_assessment_none_is_invalid() -> None:
    bad_final = _final_pass(ta=None)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_time_assessment_wrong_object_is_invalid() -> None:
    bad_final = _final_pass(ta=object())
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_time_assessment_not_valid_outcome_is_invalid() -> None:
    bad_ta = _time_assessment(outcome=ReceiptTimeAssessmentOutcome.NO_GO)
    bad_final = _final_pass(ta=bad_ta)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_time_assessment_reasons_nonempty_is_invalid() -> None:
    bad_ta = _time_assessment(reasons=("x",))
    bad_final = _final_pass(ta=bad_ta)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


@pytest.mark.parametrize(
    "final_overrides",
    [
        pytest.param({"outcome": ActivationCandidateFinalPreflightOutcome.NO_GO}, id="final_no_go"),
        pytest.param({"fresh_precheck_executed": False}, id="final_precheck_false"),
        pytest.param({"receipt_age_evaluated": False}, id="final_age_eval_false"),
    ],
)
def test_contradictory_final_scalar_is_invalid(final_overrides: dict[str, Any]) -> None:
    bad_final = _final_pass(**final_overrides)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


@pytest.mark.parametrize(
    "fresh_overrides",
    [
        pytest.param(
            {"outcome": ReceiptFreshnessOutcome.STALE, "reasons": ()}, id="fresh_stale"
        ),
        pytest.param({"policy_evaluated": False}, id="fresh_policy_false"),
    ],
)
def test_contradictory_freshness_scalar_is_invalid(fresh_overrides: dict[str, Any]) -> None:
    bad_fresh = _fresh_eval(**fresh_overrides)
    assert _build(_qualified_pass(fresh=bad_fresh)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_age_exceeds_max_is_invalid() -> None:
    # consistent age across stages but age > max_age
    result = _build(_qualified_pass(age=500, max_age=100))
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID


# --- evaluated_at <-> observed age exact binding (P1 closure) ---


def test_time_binding_exact_same_now_creates_evidence() -> None:
    assert _build(evaluated_at=_EVAL_AT).outcome is (
        ActivationCandidateEvidenceOutcome.CREATED
    )


def test_time_binding_utc_same_instant_creates_evidence() -> None:
    result = _build(evaluated_at=_EVAL_AT.astimezone(timezone.utc))
    assert result.outcome is ActivationCandidateEvidenceOutcome.CREATED


def test_time_binding_one_microsecond_mismatch_is_invalid() -> None:
    # evaluated_at shifted +1us but checked_at/age unchanged → age no longer matches
    result = _build(evaluated_at=_EVAL_AT + timedelta(microseconds=1))
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID


def test_time_binding_three_hour_mismatch_is_invalid() -> None:
    result = _build(evaluated_at=_EVAL_AT + timedelta(hours=3))
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID


def test_time_binding_evaluated_before_checked_at_is_invalid() -> None:
    future_checked = (_EVAL_AT + timedelta(hours=1)).isoformat()
    bad_final = _final_pass(ta=_time_assessment(checked_at=future_checked))
    result = _build(_qualified_pass(final=bad_final))
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID


@pytest.mark.parametrize(
    "checked_at",
    [
        pytest.param("2026-06-14T11:59:59.999000", id="naive"),
        pytest.param("not-a-datetime", id="malformed"),
        pytest.param("", id="empty"),
    ],
)
def test_time_binding_malformed_checked_at_is_invalid(checked_at: str) -> None:
    bad_final = _final_pass(ta=_time_assessment(checked_at=checked_at))
    result = _build(_qualified_pass(final=bad_final))
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID


# --- canonical hash: determinism, recomputation, field sensitivity ---


def _consistent(
    *,
    age: int = _AGE,
    max_age: int = _MAX,
    sha: str = _SHA,
    market: str = "KR",
    symbol: str = "005930",
    evaluated_at: datetime = _EVAL_AT,
) -> tuple[Any, datetime]:
    """Fully consistent qualified result + matching evaluated_at (always CREATED)."""
    final = _final_pass(
        age=age,
        evaluated_at=evaluated_at,
        receipt_sha256=sha,
        market=market,
        symbol=symbol,
    )
    fresh = _fresh_eval(age=age, max_age=max_age)
    qual = _qualified_pass(
        receipt_sha256=sha, market=market, symbol=symbol, final=final, fresh=fresh
    )
    return qual, evaluated_at


def _digest(**kwargs: Any) -> str:
    qual, evaluated_at = _consistent(**kwargs)
    result = build_activation_candidate_evidence(
        qualified_result=qual, evaluated_at=evaluated_at
    )
    assert result.evidence is not None
    return result.evidence.evidence_sha256


def test_evidence_hash_is_deterministic() -> None:
    assert _digest() == _digest()


def test_evidence_hash_independent_recomputation() -> None:
    result = _build()
    assert result.evidence is not None
    payload = asdict(result.evidence)
    payload.pop("evidence_sha256")
    assert payload_sha256(payload) == result.evidence.evidence_sha256


def test_evidence_sha256_is_lowercase_hex64() -> None:
    result = _build()
    assert result.evidence is not None
    assert evidence_mod._is_lower_hex64(result.evidence.evidence_sha256)


def test_each_hash_field_changes_digest() -> None:
    base = _digest()
    # evaluated_at shifts together with checked_at so age stays constant but evaluated_at differs
    assert _digest(evaluated_at=_EVAL_AT + timedelta(seconds=5)) != base
    assert _digest(age=2000) != base
    assert _digest(max_age=500_000_000) != base
    assert _digest(sha="b" * 64) != base
    assert _digest(market="US") != base
    assert _digest(symbol="000660") != base


def test_posture_field_change_changes_digest() -> None:
    base_payload = asdict(_build().evidence)
    base_payload.pop("evidence_sha256")
    base = payload_sha256(base_payload)
    tampered = dict(base_payload)
    tampered["runtime_activation_outcome"] = "go"
    assert payload_sha256(tampered) != base
    tampered2 = dict(base_payload)
    tampered2["activation_authorized"] = True
    assert payload_sha256(tampered2) != base


def test_final_and_freshness_outcome_fields_change_digest() -> None:
    base_payload = asdict(_build().evidence)
    base_payload.pop("evidence_sha256")
    base = payload_sha256(base_payload)
    for field in ("final_preflight_outcome", "freshness_outcome"):
        tampered = dict(base_payload)
        tampered[field] = "tampered"
        assert payload_sha256(tampered) != base


# --- combined fail-closed outcome (builder failure must not be combined PASS) ---


def test_combined_pass_on_created_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)
    combined = freshness_qualify_and_build_candidate_evidence(
        settings=settings,
        receipt_payload=receipt,
        now=fr_helper._NOW,
        policy=ReceiptFreshnessPolicy(max_age_microseconds=1_000_000_000),
        base_dir=tmp_path,
    )
    assert combined.outcome is FreshnessQualifiedEvidenceOutcome.PASS
    assert combined.reasons == ()
    assert combined.evidence_result is not None
    assert combined.evidence_result.outcome is ActivationCandidateEvidenceOutcome.CREATED


def _invalid_evidence_result() -> Any:
    from composition.activation_candidate_evidence import ActivationCandidateEvidenceResult

    return ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.INVALID,
        reasons=("candidate_evidence_invalid_input",),
        evidence=None,
    )


def _not_eligible_evidence_result() -> Any:
    from composition.activation_candidate_evidence import ActivationCandidateEvidenceResult

    return ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.NOT_ELIGIBLE,
        reasons=("candidate_evidence_not_eligible",),
        evidence=None,
    )


@pytest.mark.parametrize(
    "fake_result",
    [
        pytest.param(_invalid_evidence_result, id="builder_invalid"),
        pytest.param(_not_eligible_evidence_result, id="builder_not_eligible"),
    ],
)
def test_combined_no_go_when_qualified_pass_but_evidence_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_result: Any
) -> None:
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)

    def _fake_builder(**kwargs: Any) -> Any:
        return fake_result()

    monkeypatch.setattr(
        evidence_mod, "build_activation_candidate_evidence", _fake_builder
    )

    combined = freshness_qualify_and_build_candidate_evidence(
        settings=settings,
        receipt_payload=receipt,
        now=fr_helper._NOW,
        policy=ReceiptFreshnessPolicy(max_age_microseconds=1_000_000_000),
        base_dir=tmp_path,
    )
    assert combined.qualified_result.outcome is (
        ActivationCandidateFreshnessPreflightOutcome.PASS
    )
    assert combined.outcome is FreshnessQualifiedEvidenceOutcome.NO_GO
    assert combined.reasons == ("candidate_evidence_generation_invalid",)


def test_combined_no_go_preserves_qualified_reasons_on_stale(tmp_path: Path) -> None:
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)
    combined = freshness_qualify_and_build_candidate_evidence(
        settings=settings,
        receipt_payload=receipt,
        now=fr_helper._NOW + timedelta(microseconds=5),
        policy=ReceiptFreshnessPolicy(max_age_microseconds=0),
        base_dir=tmp_path,
    )
    assert combined.outcome is FreshnessQualifiedEvidenceOutcome.NO_GO
    assert combined.reasons == ("candidate_receipt_stale",)
    assert combined.evidence_result is None


# --- API composition: single execution, PASS evidence, NO_GO no evidence ---


def test_composition_pass_builds_evidence_single_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)

    snapshot_calls: list[int] = []
    precheck_calls: list[int] = []
    evaluator_calls: list[int] = []
    builder_calls: list[int] = []

    import composition.activation_candidate_freshness_preflight as freshness_mod
    import composition.activation_candidate_final_preflight as final_mod

    # snapshot build wraps the single receipt verifier call (verifier-once proven in 4l);
    # spying the snapshot here proves one receipt snapshot == one verifier observation.
    real_snapshot = freshness_mod.verify_and_snapshot_precheck_receipt
    real_precheck = final_mod.precheck_runtime
    real_eval = freshness_mod.evaluate_receipt_freshness
    real_builder = evidence_mod.build_activation_candidate_evidence

    def _spy_snapshot(payload: object) -> Any:
        snapshot_calls.append(1)
        return real_snapshot(payload)

    def _spy_precheck(*args: Any, **kwargs: Any) -> Any:
        precheck_calls.append(1)
        return real_precheck(*args, **kwargs)

    def _spy_eval(**kwargs: Any) -> Any:
        evaluator_calls.append(1)
        return real_eval(**kwargs)

    def _spy_builder(**kwargs: Any) -> Any:
        builder_calls.append(1)
        return real_builder(**kwargs)

    monkeypatch.setattr(freshness_mod, "verify_and_snapshot_precheck_receipt", _spy_snapshot)
    monkeypatch.setattr(final_mod, "precheck_runtime", _spy_precheck)
    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _spy_eval)
    monkeypatch.setattr(evidence_mod, "build_activation_candidate_evidence", _spy_builder)

    combined = freshness_qualify_and_build_candidate_evidence(
        settings=settings,
        receipt_payload=receipt,
        now=fr_helper._NOW,
        policy=ReceiptFreshnessPolicy(max_age_microseconds=1_000_000_000),
        base_dir=tmp_path,
    )

    assert combined.outcome is FreshnessQualifiedEvidenceOutcome.PASS
    assert combined.evidence_result is not None
    assert combined.evidence_result.outcome is ActivationCandidateEvidenceOutcome.CREATED
    ev = combined.evidence_result.evidence
    assert ev is not None
    assert ev.receipt_sha256 == combined.qualified_result.receipt_sha256
    assert ev.evaluated_at == fr_helper._NOW.isoformat()
    assert len(snapshot_calls) == 1
    assert len(precheck_calls) == 1
    assert len(evaluator_calls) == 1
    assert len(builder_calls) == 1


def test_composition_evidence_failure_does_not_rerun_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)

    snapshot_calls: list[int] = []
    precheck_calls: list[int] = []
    evaluator_calls: list[int] = []

    import composition.activation_candidate_freshness_preflight as freshness_mod
    import composition.activation_candidate_final_preflight as final_mod

    real_snapshot = freshness_mod.verify_and_snapshot_precheck_receipt
    real_precheck = final_mod.precheck_runtime
    real_eval = freshness_mod.evaluate_receipt_freshness

    def _spy_snapshot(payload: object) -> Any:
        snapshot_calls.append(1)
        return real_snapshot(payload)

    def _spy_precheck(*args: Any, **kwargs: Any) -> Any:
        precheck_calls.append(1)
        return real_precheck(*args, **kwargs)

    def _spy_eval(**kwargs: Any) -> Any:
        evaluator_calls.append(1)
        return real_eval(**kwargs)

    def _fake_builder(**kwargs: Any) -> Any:
        return _invalid_evidence_result()

    monkeypatch.setattr(freshness_mod, "verify_and_snapshot_precheck_receipt", _spy_snapshot)
    monkeypatch.setattr(final_mod, "precheck_runtime", _spy_precheck)
    monkeypatch.setattr(freshness_mod, "evaluate_receipt_freshness", _spy_eval)
    monkeypatch.setattr(evidence_mod, "build_activation_candidate_evidence", _fake_builder)

    combined = freshness_qualify_and_build_candidate_evidence(
        settings=settings,
        receipt_payload=receipt,
        now=fr_helper._NOW,
        policy=ReceiptFreshnessPolicy(max_age_microseconds=1_000_000_000),
        base_dir=tmp_path,
    )
    assert combined.outcome is FreshnessQualifiedEvidenceOutcome.NO_GO
    # evidence failure must not rerun any upstream stage
    assert len(snapshot_calls) == 1
    assert len(precheck_calls) == 1
    assert len(evaluator_calls) == 1


def test_composition_evidence_uses_same_now_as_qualified(tmp_path: Path) -> None:
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)
    combined = freshness_qualify_and_build_candidate_evidence(
        settings=settings,
        receipt_payload=receipt,
        now=fr_helper._NOW,
        policy=ReceiptFreshnessPolicy(max_age_microseconds=1_000_000_000),
        base_dir=tmp_path,
    )
    assert combined.evidence_result is not None
    ev = combined.evidence_result.evidence
    assert ev is not None
    # evidence digest must be independently recomputable from its own scalars
    payload = asdict(ev)
    payload.pop("evidence_sha256")
    assert payload_sha256(payload) == ev.evidence_sha256
    # evaluated_at equals the single caller now
    assert ev.evaluated_at == fr_helper._NOW.isoformat()


def test_composition_stale_skips_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)

    def _fail_builder(**kwargs: Any) -> Any:
        raise AssertionError("builder must not run on stale NO_GO")

    monkeypatch.setattr(evidence_mod, "build_activation_candidate_evidence", _fail_builder)

    # now strictly after checked_at so age > 0; max_age 0 forces STALE
    combined = freshness_qualify_and_build_candidate_evidence(
        settings=settings,
        receipt_payload=receipt,
        now=fr_helper._NOW + timedelta(microseconds=5),
        policy=ReceiptFreshnessPolicy(max_age_microseconds=0),
        base_dir=tmp_path,
    )
    assert combined.outcome is FreshnessQualifiedEvidenceOutcome.NO_GO
    assert combined.qualified_result.reasons == ("candidate_receipt_stale",)
    assert combined.evidence_result is None
