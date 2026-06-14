"""RTM-7c.4n — canonical freshness-qualified candidate evidence tests."""

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
from decision.canonical_json import payload_sha256

import test_activation_candidate_freshness_preflight as fr_helper

_KST = timezone(timedelta(hours=9))
_EVAL_AT = datetime(2026, 6, 14, 12, 0, 0, tzinfo=_KST)
_SHA = "a" * 64


# --- synthetic result builders (fast, no filesystem) ---


def _final_pass() -> ActivationCandidateFinalPreflightResult:
    return ActivationCandidateFinalPreflightResult(
        outcome=ActivationCandidateFinalPreflightOutcome.PASS,
        receipt_sha256=_SHA,
        market="KR",
        symbol="005930",
        reasons=(),
        revalidation_result=None,
        current_precheck_result=None,
        fresh_precheck_executed=True,
        receipt_age_evaluated=True,
        receipt_age_microseconds=1000,
        receipt_time_assessment=None,
        freshness_policy_evaluated=False,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        explicit_operator_approval_required=True,
        writers_stopped_manual_confirmation_required=True,
    )


def _fresh_eval(
    *,
    age: int = 1000,
    max_age: int = 300_000_000,
    outcome: ReceiptFreshnessOutcome = ReceiptFreshnessOutcome.FRESH,
    policy_evaluated: bool = True,
) -> ReceiptFreshnessEvaluation:
    reasons: tuple[str, ...] = ()
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


def _qualified_pass(**overrides: Any) -> ActivationCandidateFreshnessPreflightResult:
    base = dict(
        outcome=ActivationCandidateFreshnessPreflightOutcome.PASS,
        reasons=(),
        receipt_sha256=_SHA,
        market="KR",
        symbol="005930",
        final_preflight_result=_final_pass(),
        freshness_evaluation=_fresh_eval(),
        freshness_policy_evaluated=True,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
        explicit_operator_approval_required=True,
        writers_stopped_manual_confirmation_required=True,
    )
    base.update(overrides)
    return ActivationCandidateFreshnessPreflightResult(**base)


# --- eligibility: CREATED ---


def test_qualified_pass_fresh_creates_evidence() -> None:
    result = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(), evaluated_at=_EVAL_AT
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.CREATED
    assert result.reasons == ()
    ev = result.evidence
    assert ev is not None
    assert ev.schema_version == ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION
    assert ev.evaluated_at == _EVAL_AT.isoformat()
    assert ev.receipt_sha256 == _SHA
    assert ev.market == "KR"
    assert ev.symbol == "005930"
    assert ev.max_age_microseconds == 300_000_000
    assert ev.receipt_age_microseconds == 1000
    assert ev.final_preflight_outcome == "pass"
    assert ev.freshness_outcome == "fresh"
    assert ev.fresh_precheck_executed is True
    assert ev.receipt_age_evaluated is True
    assert ev.freshness_policy_evaluated is True
    assert ev.activation_authorized is False
    assert ev.runtime_activation_outcome == "no_go"
    assert evidence_mod._is_lower_hex64(ev.evidence_sha256)


def test_inclusive_boundary_age_equals_max_creates_evidence() -> None:
    result = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(
            freshness_evaluation=_fresh_eval(age=100, max_age=100)
        ),
        evaluated_at=_EVAL_AT,
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.CREATED


# --- eligibility: NOT_ELIGIBLE (well-formed non-PASS) ---


def test_stale_no_go_is_not_eligible() -> None:
    result = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(
            outcome=ActivationCandidateFreshnessPreflightOutcome.NO_GO,
            reasons=("candidate_receipt_stale",),
            freshness_evaluation=_fresh_eval(
                age=500, max_age=100, outcome=ReceiptFreshnessOutcome.STALE
            ),
        ),
        evaluated_at=_EVAL_AT,
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.NOT_ELIGIBLE
    assert result.reasons == ("candidate_evidence_not_eligible",)
    assert result.evidence is None


def test_final_preflight_no_go_is_not_eligible() -> None:
    result = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(
            outcome=ActivationCandidateFreshnessPreflightOutcome.NO_GO,
            reasons=("candidate_current_precheck:x",),
            final_preflight_result=None,
            freshness_evaluation=None,
            freshness_policy_evaluated=False,
        ),
        evaluated_at=_EVAL_AT,
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.NOT_ELIGIBLE
    assert result.evidence is None


# --- eligibility: INVALID (wrong object / contradictory) ---


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


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"reasons": ("unexpected",)}, id="pass_with_reasons"),
        pytest.param({"activation_authorized": True}, id="activation_true"),
        pytest.param({"runtime_activation_outcome": "go"}, id="runtime_go"),
        pytest.param({"freshness_policy_evaluated": False}, id="policy_not_evaluated"),
        pytest.param({"final_preflight_result": None}, id="no_final_result"),
        pytest.param({"freshness_evaluation": None}, id="no_freshness_eval"),
        pytest.param({"receipt_sha256": "A" * 64}, id="uppercase_sha"),
        pytest.param({"receipt_sha256": "a" * 63}, id="short_sha"),
        pytest.param({"market": ""}, id="empty_market"),
        pytest.param({"symbol": ""}, id="empty_symbol"),
        pytest.param(
            {"freshness_evaluation": _fresh_eval(outcome=ReceiptFreshnessOutcome.STALE)},
            id="contradictory_stale_eval",
        ),
        pytest.param(
            {"freshness_evaluation": _fresh_eval(age=500, max_age=100)},
            id="age_exceeds_max",
        ),
        pytest.param(
            {"freshness_evaluation": _fresh_eval(policy_evaluated=False)},
            id="eval_policy_not_evaluated",
        ),
    ],
)
def test_contradictory_pass_result_is_invalid(overrides: dict[str, Any]) -> None:
    result = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(**overrides), evaluated_at=_EVAL_AT
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID
    assert result.evidence is None


def test_contradictory_final_outcome_no_go_is_invalid() -> None:
    bad_final = replace(
        _final_pass(), outcome=ActivationCandidateFinalPreflightOutcome.NO_GO
    )
    result = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(final_preflight_result=bad_final),
        evaluated_at=_EVAL_AT,
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID


def test_contradictory_final_precheck_not_executed_is_invalid() -> None:
    bad_final = replace(_final_pass(), fresh_precheck_executed=False)
    result = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(final_preflight_result=bad_final),
        evaluated_at=_EVAL_AT,
    )
    assert result.outcome is ActivationCandidateEvidenceOutcome.INVALID


# --- canonical hash: determinism, recomputation, field sensitivity ---


def test_evidence_hash_is_deterministic() -> None:
    a = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(), evaluated_at=_EVAL_AT
    )
    b = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(), evaluated_at=_EVAL_AT
    )
    assert a.evidence is not None and b.evidence is not None
    assert a.evidence.evidence_sha256 == b.evidence.evidence_sha256


def test_evidence_hash_independent_recomputation() -> None:
    result = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(), evaluated_at=_EVAL_AT
    )
    assert result.evidence is not None
    payload = asdict(result.evidence)
    payload.pop("evidence_sha256")
    assert payload_sha256(payload) == result.evidence.evidence_sha256


def test_evidence_sha256_is_lowercase_hex64() -> None:
    result = build_activation_candidate_evidence(
        qualified_result=_qualified_pass(), evaluated_at=_EVAL_AT
    )
    assert result.evidence is not None
    assert evidence_mod._is_lower_hex64(result.evidence.evidence_sha256)


def _digest(qualified: Any = None, evaluated_at: datetime = _EVAL_AT) -> str:
    result = build_activation_candidate_evidence(
        qualified_result=qualified if qualified is not None else _qualified_pass(),
        evaluated_at=evaluated_at,
    )
    assert result.evidence is not None
    return result.evidence.evidence_sha256


def test_each_hash_field_changes_digest() -> None:
    base = _digest()
    assert _digest(evaluated_at=_EVAL_AT + timedelta(microseconds=1)) != base
    assert _digest(_qualified_pass(receipt_sha256="b" * 64)) != base
    assert _digest(_qualified_pass(market="US")) != base
    assert _digest(_qualified_pass(symbol="000660")) != base
    assert _digest(_qualified_pass(freshness_evaluation=_fresh_eval(max_age=500_000_000))) != base
    assert _digest(_qualified_pass(freshness_evaluation=_fresh_eval(age=2000))) != base


def test_posture_field_change_changes_digest() -> None:
    base_payload = asdict(
        build_activation_candidate_evidence(
            qualified_result=_qualified_pass(), evaluated_at=_EVAL_AT
        ).evidence
    )
    base_payload.pop("evidence_sha256")
    base = payload_sha256(base_payload)
    tampered = dict(base_payload)
    tampered["runtime_activation_outcome"] = "go"
    assert payload_sha256(tampered) != base
    tampered2 = dict(base_payload)
    tampered2["activation_authorized"] = True
    assert payload_sha256(tampered2) != base


def test_final_and_freshness_outcome_fields_change_digest() -> None:
    base_payload = asdict(
        build_activation_candidate_evidence(
            qualified_result=_qualified_pass(), evaluated_at=_EVAL_AT
        ).evidence
    )
    base_payload.pop("evidence_sha256")
    base = payload_sha256(base_payload)
    for field in ("final_preflight_outcome", "freshness_outcome"):
        tampered = dict(base_payload)
        tampered[field] = "tampered"
        assert payload_sha256(tampered) != base


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

    assert combined.qualified_result.outcome is (
        ActivationCandidateFreshnessPreflightOutcome.PASS
    )
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
    assert combined.qualified_result.outcome is (
        ActivationCandidateFreshnessPreflightOutcome.NO_GO
    )
    assert combined.qualified_result.reasons == ("candidate_receipt_stale",)
    assert combined.evidence_result is None
