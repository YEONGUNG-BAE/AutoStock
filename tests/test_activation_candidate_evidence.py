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
    PRECHECK_RECEIPT_SCHEMA_VERSION,
    build_receipt_hash_payload,
    compute_receipt_sha256,
)
from composition.sqlite_inspector import ArtifactFingerprint
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


# --- machine-proof nested builders (fast, no filesystem) — fully consistent by default ---


def _fingerprints(variant: int = 0) -> tuple[ArtifactFingerprint, ...]:
    """Canonical 4-artifact fingerprint tuple in the single-source order.

    ``variant`` perturbs only the artifact body (sha256), producing a distinct — but still
    internally consistent — fresh precheck receipt hash."""

    return tuple(
        ArtifactFingerprint(
            name=name,
            present=True,
            is_regular_file=True,
            size=100 + i,
            sha256=f"{variant:02d}{i:02d}" + "f" * 60,
            user_version=i,
            sidecar_suffixes=(),
        )
        for i, name in enumerate(PAPER_FAST_LOOP_ARTIFACT_NAMES)
    )


def _revalidation(
    *,
    sha: str = _SHA,
    market: str = "KR",
    symbol: str = "005930",
    fps: tuple[ArtifactFingerprint, ...] | None = None,
    **overrides: Any,
) -> ActivationCandidateRevalidationResult:
    if fps is None:
        fps = _fingerprints()
    base = dict(
        outcome=ActivationCandidateRevalidationOutcome.PASS,
        receipt_sha256=sha,
        market=market,
        symbol=symbol,
        reasons=(),
        current_fingerprints_before=fps,
        current_fingerprints_after=fps,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        explicit_operator_approval_required=True,
        writers_stopped_manual_confirmation_required=True,
        freshness_evaluated=False,
    )
    base.update(overrides)
    return ActivationCandidateRevalidationResult(**base)


def _inspection(
    *, market: str = "KR", symbol: str = "005930", **overrides: Any
) -> PaperFastLoopInspection:
    base = dict(
        outcome=InspectionOutcome.OK,
        market=market,
        symbol=symbol,
        ledger=None,
        journal=None,
        active_store=None,
        execution_inputs=None,
        active_decision=None,
        missing_databases=(),
        reasons=(),
    )
    base.update(overrides)
    return PaperFastLoopInspection(**base)


def _fresh_receipt(
    *,
    checked_at: str,
    market: str = "KR",
    symbol: str = "005930",
    fps: tuple[ArtifactFingerprint, ...] | None = None,
    recompute_sha: bool = True,
    **overrides: Any,
) -> RuntimePrecheckReceipt:
    if fps is None:
        fps = _fingerprints()
    sha = compute_receipt_sha256(
        build_receipt_hash_payload(
            schema_version=PRECHECK_RECEIPT_SCHEMA_VERSION,
            checked_at=checked_at,
            market=market,
            symbol=symbol,
            enabled=True,
            machine_outcome="pass",
            inspection_outcome="ok",
            reasons=(),
            fingerprints_before=fps,
            fingerprints_after=fps,
        )
    )
    base = dict(
        schema_version=PRECHECK_RECEIPT_SCHEMA_VERSION,
        checked_at=checked_at,
        market=market,
        symbol=symbol,
        enabled=True,
        machine_outcome="pass",
        inspection_outcome="ok",
        reasons=(),
        fingerprints_before=fps,
        fingerprints_after=fps,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        explicit_operator_approval_required=True,
        writers_stopped_manual_confirmation_required=True,
        receipt_sha256=sha,
    )
    base.update(overrides)
    if recompute_sha and "receipt_sha256" not in overrides:
        base["receipt_sha256"] = sha
    return RuntimePrecheckReceipt(**base)


def _precheck(
    *,
    checked_at: str,
    market: str = "KR",
    symbol: str = "005930",
    fps: tuple[ArtifactFingerprint, ...] | None = None,
    inspection: Any = ...,
    receipt: Any = ...,
    **overrides: Any,
) -> RuntimePrecheckResult:
    if fps is None:
        fps = _fingerprints()
    base = dict(
        machine_outcome=MachineCheckOutcome.PASS,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        explicit_operator_approval_required=True,
        writers_stopped_manual_confirmation_required=True,
        market=market,
        symbol=symbol,
        inspection=(
            _inspection(market=market, symbol=symbol) if inspection is ... else inspection
        ),
        fingerprints_before=fps,
        fingerprints_after=fps,
        reasons=(),
        receipt=(
            _fresh_receipt(checked_at=checked_at, market=market, symbol=symbol, fps=fps)
            if receipt is ...
            else receipt
        ),
    )
    base.update(overrides)
    return RuntimePrecheckResult(**base)


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
    market = overrides.get("market", "KR")
    symbol = overrides.get("symbol", "005930")
    sha = overrides.get("receipt_sha256", _SHA)
    fps = _fingerprints()
    checked_at = evaluated_at.isoformat()
    base = dict(
        outcome=ActivationCandidateFinalPreflightOutcome.PASS,
        receipt_sha256=_SHA,
        market="KR",
        symbol="005930",
        reasons=(),
        revalidation_result=_revalidation(sha=sha, market=market, symbol=symbol, fps=fps),
        current_precheck_result=_precheck(
            checked_at=checked_at, market=market, symbol=symbol, fps=fps
        ),
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


# --- machine-proof absence matrix (RTM-7c.4n fresh machine-proof closure) ---


def _ISO() -> str:
    return _EVAL_AT.isoformat()


@pytest.mark.parametrize(
    "final_overrides",
    [
        pytest.param({"revalidation_result": None}, id="revalidation_none"),
        pytest.param({"current_precheck_result": None}, id="current_precheck_none"),
        pytest.param(
            {"revalidation_result": None, "current_precheck_result": None},
            id="flag_only_both_none",
        ),
        pytest.param({"revalidation_result": object()}, id="revalidation_wrong_object"),
        pytest.param({"current_precheck_result": object()}, id="current_precheck_wrong_object"),
        pytest.param({"revalidation_result": "pass"}, id="revalidation_str"),
    ],
)
def test_machine_proof_absent_is_invalid(final_overrides: dict[str, Any]) -> None:
    # ``fresh_precheck_executed`` stays True; only a boolean flag without the real result
    # objects must fail closed (no created evidence).
    bad_final = _final_pass(**final_overrides)
    result = _build(_qualified_pass(final=bad_final))
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID
    assert result.evidence is None


def test_machine_proof_revalidation_subclass_is_invalid() -> None:
    class _SubReval(ActivationCandidateRevalidationResult):
        pass

    sub = _SubReval(**asdict(_revalidation()))  # type: ignore[arg-type]
    bad_final = _final_pass(revalidation_result=sub)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_machine_proof_precheck_subclass_is_invalid() -> None:
    class _SubPrecheck(RuntimePrecheckResult):
        pass

    sub = _SubPrecheck(**asdict(_precheck(checked_at=_ISO())))  # type: ignore[arg-type]
    bad_final = _final_pass(current_precheck_result=sub)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_machine_proof_revalidation_deleted_field_is_invalid() -> None:
    bad_reval = _revalidation()
    object.__delattr__(bad_reval, "outcome")
    bad_final = _final_pass(revalidation_result=bad_reval)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


# --- revalidation strict contract matrix ---


@pytest.mark.parametrize(
    "reval_overrides",
    [
        pytest.param(
            {"outcome": ActivationCandidateRevalidationOutcome.NO_GO}, id="reval_no_go"
        ),
        pytest.param({"reasons": ("unexpected",)}, id="reval_reasons_nonempty"),
        pytest.param({"sha": "b" * 64}, id="reval_sha_mismatch"),
        pytest.param({"market": "US"}, id="reval_market_mismatch"),
        pytest.param({"symbol": "000660"}, id="reval_symbol_mismatch"),
        pytest.param({"activation_authorized": True}, id="reval_activation_true"),
        pytest.param({"runtime_activation_outcome": "go"}, id="reval_runtime_go"),
        pytest.param({"explicit_operator_approval_required": False}, id="reval_approval_false"),
        pytest.param(
            {"writers_stopped_manual_confirmation_required": False}, id="reval_writers_false"
        ),
        pytest.param({"freshness_evaluated": True}, id="reval_freshness_evaluated"),
    ],
)
def test_revalidation_contract_violation_is_invalid(reval_overrides: dict[str, Any]) -> None:
    bad_reval = _revalidation(**reval_overrides)
    bad_final = _final_pass(revalidation_result=bad_reval)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_revalidation_fingerprints_before_after_differ_is_invalid() -> None:
    bad_reval = _revalidation(current_fingerprints_after=_fingerprints(variant=1))
    bad_final = _final_pass(revalidation_result=bad_reval)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


@pytest.mark.parametrize(
    "bad_fps",
    [
        pytest.param((), id="reval_fps_empty"),
        pytest.param(("x",), id="reval_fps_wrong_element"),
        pytest.param(_fingerprints()[:3], id="reval_fps_short"),
        pytest.param(object(), id="reval_fps_not_tuple"),
    ],
)
def test_revalidation_malformed_fingerprints_is_invalid(bad_fps: object) -> None:
    bad_reval = _revalidation(
        current_fingerprints_before=bad_fps, current_fingerprints_after=bad_fps
    )
    bad_final = _final_pass(revalidation_result=bad_reval)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_revalidation_fingerprint_subclass_is_invalid() -> None:
    class _SubFp(ArtifactFingerprint):
        pass

    base_fps = _fingerprints()
    bad_fps = (_SubFp(**asdict(base_fps[0])),) + base_fps[1:]  # type: ignore[arg-type]
    bad_reval = _revalidation(
        current_fingerprints_before=bad_fps, current_fingerprints_after=bad_fps
    )
    # precheck/receipt must mirror so the only deviation is the subclass element
    bad_pc = _precheck(
        checked_at=_ISO(),
        fingerprints_before=bad_fps,
        fingerprints_after=bad_fps,
        receipt=_fresh_receipt(checked_at=_ISO(), fps=bad_fps),
    )
    bad_final = _final_pass(revalidation_result=bad_reval, current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


# --- current precheck strict contract matrix ---


@pytest.mark.parametrize(
    "pc_overrides",
    [
        pytest.param({"machine_outcome": MachineCheckOutcome.NO_GO}, id="pc_machine_no_go"),
        pytest.param({"reasons": ("unexpected",)}, id="pc_reasons_nonempty"),
        pytest.param({"market": "US"}, id="pc_market_mismatch"),
        pytest.param({"symbol": "000660"}, id="pc_symbol_mismatch"),
        pytest.param({"activation_authorized": True}, id="pc_activation_true"),
        pytest.param({"runtime_activation_outcome": "go"}, id="pc_runtime_go"),
        pytest.param({"explicit_operator_approval_required": False}, id="pc_approval_false"),
        pytest.param(
            {"writers_stopped_manual_confirmation_required": False}, id="pc_writers_false"
        ),
    ],
)
def test_current_precheck_contract_violation_is_invalid(pc_overrides: dict[str, Any]) -> None:
    bad_pc = _precheck(checked_at=_ISO(), **pc_overrides)
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


@pytest.mark.parametrize(
    "insp",
    [
        pytest.param(object(), id="inspection_wrong_object"),
        pytest.param(None, id="inspection_none"),
    ],
)
def test_current_precheck_inspection_wrong_object_is_invalid(insp: object) -> None:
    bad_pc = _precheck(checked_at=_ISO(), inspection=insp)
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


@pytest.mark.parametrize(
    "insp_overrides",
    [
        pytest.param({"outcome": InspectionOutcome.NO_GO}, id="inspection_no_go"),
        pytest.param({"reasons": ("x",)}, id="inspection_reasons_nonempty"),
        pytest.param({"market": "US"}, id="inspection_market_mismatch"),
        pytest.param({"symbol": "000660"}, id="inspection_symbol_mismatch"),
    ],
)
def test_current_precheck_inspection_contract_violation_is_invalid(
    insp_overrides: dict[str, Any]
) -> None:
    bad_insp = _inspection(**insp_overrides)
    bad_pc = _precheck(checked_at=_ISO(), inspection=bad_insp)
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_current_precheck_fingerprints_differ_from_revalidation_is_invalid() -> None:
    # precheck internally consistent but its artifact observation differs from revalidation's
    other = _fingerprints(variant=2)
    bad_pc = _precheck(
        checked_at=_ISO(),
        fingerprints_before=other,
        fingerprints_after=other,
        receipt=_fresh_receipt(checked_at=_ISO(), fps=other),
    )
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_current_precheck_before_after_differ_is_invalid() -> None:
    bad_pc = _precheck(checked_at=_ISO(), fingerprints_after=_fingerprints(variant=3))
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


# --- fresh precheck receipt strict contract matrix ---


@pytest.mark.parametrize(
    "receipt_overrides",
    [
        pytest.param({"machine_outcome": "no_go"}, id="receipt_machine_not_pass"),
        pytest.param({"inspection_outcome": "no_go"}, id="receipt_inspection_not_ok"),
        pytest.param({"reasons": ("x",)}, id="receipt_reasons_nonempty"),
        pytest.param({"market": "US"}, id="receipt_market_mismatch"),
        pytest.param({"symbol": "000660"}, id="receipt_symbol_mismatch"),
        pytest.param({"enabled": False}, id="receipt_enabled_false"),
        pytest.param({"activation_authorized": True}, id="receipt_activation_true"),
        pytest.param({"runtime_activation_outcome": "go"}, id="receipt_runtime_go"),
        pytest.param(
            {"explicit_operator_approval_required": False}, id="receipt_approval_false"
        ),
        pytest.param(
            {"writers_stopped_manual_confirmation_required": False}, id="receipt_writers_false"
        ),
    ],
)
def test_fresh_receipt_contract_violation_is_invalid(receipt_overrides: dict[str, Any]) -> None:
    # Build a receipt whose stored hash matches its (tampered) payload would still fail the
    # strict field guards; here the canonical-hash recompute is left at the clean value so the
    # field guard — not the hash — is what fails closed.
    bad_receipt = _fresh_receipt(checked_at=_ISO(), **receipt_overrides)
    bad_pc = _precheck(checked_at=_ISO(), receipt=bad_receipt)
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_fresh_receipt_wrong_object_is_invalid() -> None:
    bad_pc = _precheck(checked_at=_ISO(), receipt=object())
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_fresh_receipt_checked_at_mismatch_is_invalid() -> None:
    # receipt stamped at a different instant than the caller now
    other_iso = (_EVAL_AT + timedelta(seconds=1)).isoformat()
    bad_receipt = _fresh_receipt(checked_at=other_iso)
    bad_pc = _precheck(checked_at=_ISO(), receipt=bad_receipt)
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_fresh_receipt_hash_mismatch_is_invalid() -> None:
    # stored sha is valid hex64 but not the canonical recomputation of the receipt payload
    bad_receipt = _fresh_receipt(checked_at=_ISO(), receipt_sha256="c" * 64)
    bad_pc = _precheck(checked_at=_ISO(), receipt=bad_receipt)
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


@pytest.mark.parametrize(
    "bad_sha",
    [
        pytest.param("ABC" + "0" * 61, id="receipt_sha_uppercase"),
        pytest.param("z" * 64, id="receipt_sha_non_hex"),
        pytest.param("a" * 63, id="receipt_sha_short"),
        pytest.param("", id="receipt_sha_empty"),
    ],
)
def test_fresh_receipt_non_hex_sha_is_invalid(bad_sha: str) -> None:
    bad_receipt = _fresh_receipt(checked_at=_ISO(), receipt_sha256=bad_sha)
    bad_pc = _precheck(checked_at=_ISO(), receipt=bad_receipt)
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


def test_fresh_receipt_malformed_fingerprints_is_invalid() -> None:
    bad_receipt = _fresh_receipt(
        checked_at=_ISO(), fingerprints_before=(), fingerprints_after=()
    )
    bad_pc = _precheck(checked_at=_ISO(), receipt=bad_receipt)
    bad_final = _final_pass(current_precheck_result=bad_pc)
    assert _build(_qualified_pass(final=bad_final)).outcome is (
        ActivationCandidateEvidenceOutcome.INVALID
    )


# --- schema v2 + fresh_precheck_receipt_sha256 binding ---


def test_evidence_schema_version_is_two() -> None:
    assert ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION == 2
    ev = _build().evidence
    assert ev is not None
    assert ev.schema_version == 2


def test_fresh_precheck_receipt_sha256_is_bound_and_recomputable() -> None:
    ev = _build().evidence
    assert ev is not None
    assert evidence_mod._is_lower_hex64(ev.fresh_precheck_receipt_sha256)
    # the bound fresh receipt hash is the independently-recomputed receipt sha of the default fps
    expected = _fresh_receipt(checked_at=_ISO()).receipt_sha256
    assert ev.fresh_precheck_receipt_sha256 == expected


def test_fresh_precheck_receipt_sha256_in_canonical_hash_payload() -> None:
    ev = _build().evidence
    assert ev is not None
    payload = asdict(ev)
    assert "fresh_precheck_receipt_sha256" in payload
    payload.pop("evidence_sha256")
    assert payload_sha256(payload) == ev.evidence_sha256


def test_original_receipt_hash_change_changes_digest() -> None:
    base = _digest()
    assert _digest(sha="b" * 64) != base


def test_fresh_receipt_hash_change_changes_digest() -> None:
    base = _build().evidence
    assert base is not None
    # perturb only the fresh precheck artifact observation → different fresh receipt hash
    fps2 = _fingerprints(variant=1)
    final = _final_pass(
        revalidation_result=_revalidation(fps=fps2),
        current_precheck_result=_precheck(checked_at=_ISO(), fps=fps2),
    )
    ev = _build(_qualified_pass(final=final)).evidence
    assert ev is not None
    assert ev.fresh_precheck_receipt_sha256 != base.fresh_precheck_receipt_sha256
    assert ev.evidence_sha256 != base.evidence_sha256
    # the original candidate receipt hash is unchanged — only the fresh binding moved
    assert ev.receipt_sha256 == base.receipt_sha256


# --- P2: evaluated_at datetime subclass fail-closed ---


def test_datetime_subclass_evaluated_at_is_invalid() -> None:
    class _DTSub(datetime):
        pass

    sub = _DTSub(2026, 6, 14, 12, 0, 0, tzinfo=_KST)
    result = _build(evaluated_at=sub)
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID
    assert result.evidence is None


def test_datetime_subclass_with_raising_isoformat_is_invalid() -> None:
    class _BadDT(datetime):
        def isoformat(self, *a: Any, **k: Any) -> str:  # noqa: D401
            raise RuntimeError("boom")

    sub = _BadDT(2026, 6, 14, 12, 0, 0, tzinfo=_KST)
    result = _build(evaluated_at=sub)
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID
    assert result.evidence is None


# --- real seeded PASS regression (no synthetic None fixtures) ---


def test_real_seeded_pass_creates_evidence(tmp_path: Path) -> None:
    # A genuinely seeded freshness-qualified PASS (real revalidation PASS + real fresh precheck
    # PASS + real fresh receipt) must still produce CREATED evidence with schema v2.
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
    assert combined.evidence_result is not None
    assert combined.evidence_result.outcome is ActivationCandidateEvidenceOutcome.CREATED
    ev = combined.evidence_result.evidence
    assert ev is not None
    assert ev.schema_version == 2
    assert evidence_mod._is_lower_hex64(ev.fresh_precheck_receipt_sha256)
    assert ev.fresh_precheck_executed is True
    assert ev.activation_authorized is False
    assert ev.runtime_activation_outcome == "no_go"


# --- evaluated_at <-> observed age exact binding (P1 closure) ---


def test_time_binding_exact_same_now_creates_evidence() -> None:
    assert _build(evaluated_at=_EVAL_AT).outcome is (
        ActivationCandidateEvidenceOutcome.CREATED
    )


def test_time_binding_utc_same_instant_creates_evidence() -> None:
    # The single caller ``now`` is used everywhere (qualified result and builder), so the fresh
    # precheck receipt's checked_at is that exact UTC instant's isoformat — a consistent UTC now
    # qualifies. (A cross-tz now whose receipt was stamped in another zone fails closed on the
    # strict ``checked_at == evaluated_at.isoformat()`` binding; that never occurs in composition.)
    utc = _EVAL_AT.astimezone(timezone.utc)
    qual, _ = _consistent(evaluated_at=utc)
    result = build_activation_candidate_evidence(qualified_result=qual, evaluated_at=utc)
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
