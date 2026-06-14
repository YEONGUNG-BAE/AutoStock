"""RTM-7c.4o — canonical Operator approval intent binding tests.

Covers eligibility, manual declaration strict-type matrix, evidence integrity/hash binding,
declared-at time binding, canonical intent hash determinism, single-observation invariants,
and isolation (no clock read, no upstream re-invocation, no persistence)."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition import activation_candidate_evidence as evidence_mod
from composition import operator_approval_intent as intent_mod
from composition.activation_candidate_evidence import (
    ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
    ActivationCandidateEvidence,
    ActivationCandidateEvidenceOutcome,
    ActivationCandidateEvidenceResult,
    FreshnessQualifiedEvidenceOutcome,
    FreshnessQualifiedEvidenceResult,
    activation_candidate_evidence_hash_payload,
    build_activation_candidate_evidence,
    freshness_qualify_and_build_candidate_evidence,
    validate_activation_candidate_evidence_object,
    validate_activation_candidate_evidence_scalars,
)
from composition.activation_candidate_freshness_preflight import (
    ActivationCandidateFreshnessPreflightOutcome,
)
from composition.operator_approval_intent import (
    APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE,
    OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
    OperatorApprovalIntentOutcome,
    build_operator_approval_intent,
)
from composition.receipt_freshness_policy import ReceiptFreshnessPolicy
from decision.canonical_json import payload_sha256

import test_activation_candidate_evidence as ev_helper

_KST = timezone(timedelta(hours=9))
_EVAL_AT = ev_helper._EVAL_AT
_DECL_AT = _EVAL_AT
_SHA = ev_helper._SHA

_CLI_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_paper_fast_loop.py"
_spec = importlib.util.spec_from_file_location("run_paper_fast_loop", _CLI_PATH)
assert _spec and _spec.loader
_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cli)


def _load_cli_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = _cli.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return code, payload


def _combined_pass(**overrides: Any) -> FreshnessQualifiedEvidenceResult:
    qualified = ev_helper._qualified_pass()
    evidence_result = build_activation_candidate_evidence(
        qualified_result=qualified, evaluated_at=_EVAL_AT
    )
    base = dict(
        outcome=FreshnessQualifiedEvidenceOutcome.PASS,
        reasons=(),
        qualified_result=qualified,
        evidence_result=evidence_result,
    )
    base.update(overrides)
    return FreshnessQualifiedEvidenceResult(**base)


def _combined_no_go(**overrides: Any) -> FreshnessQualifiedEvidenceResult:
    base = dict(
        outcome=FreshnessQualifiedEvidenceOutcome.NO_GO,
        reasons=("candidate_receipt_stale",),
        qualified_result=ev_helper._qualified_pass(
            outcome=ActivationCandidateFreshnessPreflightOutcome.NO_GO,
            reasons=("candidate_receipt_stale",),
        ),
        evidence_result=None,
    )
    base.update(overrides)
    return FreshnessQualifiedEvidenceResult(**base)


def _build_intent(
    combined: Any = ...,
    declared_at: datetime = _DECL_AT,
    **kwargs: Any,
) -> Any:
    return build_operator_approval_intent(
        combined_result=_combined_pass() if combined is ... else combined,
        declared_at=declared_at,
        operator_approval_declared=kwargs.pop("operator_approval_declared", True),
        writers_stopped_manually_confirmed=kwargs.pop(
            "writers_stopped_manually_confirmed", True
        ),
        live_orders_forbidden_confirmed=kwargs.pop("live_orders_forbidden_confirmed", True),
        **kwargs,
    )


def _intent_hash_from(intent: Any) -> str:
    payload = asdict(intent)
    payload.pop("approval_intent_sha256")
    return payload_sha256(payload)


def _evidence_hash_from_fields(**fields: Any) -> str:
    """Independent evidence hash recomputation for test fixtures only."""

    payload = activation_candidate_evidence_hash_payload(
        evaluated_at=fields["evaluated_at"],
        receipt_sha256=fields["receipt_sha256"],
        fresh_precheck_receipt_sha256=fields["fresh_precheck_receipt_sha256"],
        market=fields["market"],
        symbol=fields["symbol"],
        max_age_microseconds=fields["max_age_microseconds"],
        receipt_age_microseconds=fields["receipt_age_microseconds"],
        final_preflight_outcome=fields["final_preflight_outcome"],
        freshness_outcome=fields["freshness_outcome"],
        fresh_precheck_executed=fields["fresh_precheck_executed"],
        receipt_age_evaluated=fields["receipt_age_evaluated"],
        freshness_policy_evaluated=fields["freshness_policy_evaluated"],
    )
    return payload_sha256(payload)


def _evidence_with_matching_hash(**overrides: Any) -> ActivationCandidateEvidence:
    """Tamper evidence fields then recompute ``evidence_sha256`` for semantic-invalid matrix."""

    ev = ev_helper._build().evidence
    assert ev is not None
    fields = asdict(ev)
    fields.update(overrides)
    hash_fields = {k: fields[k] for k in fields if k != "evidence_sha256"}
    try:
        new_hash = _evidence_hash_from_fields(**hash_fields)
    except (TypeError, ValueError):
        # Non-JSON-serializable tamper (e.g. arbitrary object outcomes) — hash parity is moot.
        new_hash = "c" * 64
    fields["evidence_sha256"] = new_hash
    return ActivationCandidateEvidence(**fields)


def _intent_from_evidence(
    ev: ActivationCandidateEvidence,
    *,
    declared_at: datetime = _DECL_AT,
    **kwargs: Any,
) -> Any:
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=ev
    )
    return _build_intent(_combined_pass(evidence_result=er), declared_at=declared_at, **kwargs)


# --- eligibility: CREATED ---


def test_combined_pass_created_evidence_creates_intent() -> None:
    result = _build_intent()
    assert result.outcome is OperatorApprovalIntentOutcome.CREATED
    assert result.reasons == ()
    intent = result.intent
    assert intent is not None
    assert intent.schema_version == OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION
    assert intent.declared_at == _DECL_AT.isoformat()
    assert intent.evidence_schema_version == ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION
    assert intent.approval_scope == APPROVAL_SCOPE_ATTENDED_PAPER_FAST_LOOP_CANDIDATE
    assert intent.operator_approval_declared is True
    assert intent.writers_stopped_manually_confirmed is True
    assert intent.live_orders_forbidden_confirmed is True
    assert intent.activation_authorized is False
    assert intent.runtime_activation_outcome == "no_go"
    assert evidence_mod._is_lower_hex64(intent.approval_intent_sha256)
    assert evidence_mod._is_lower_hex64(intent.evidence_sha256)


def test_real_seeded_combined_pass_creates_intent(tmp_path: Path) -> None:
    import test_activation_candidate_freshness_preflight as fr_helper

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
    result = build_operator_approval_intent(
        combined_result=combined,
        declared_at=fr_helper._NOW,
        operator_approval_declared=True,
        writers_stopped_manually_confirmed=True,
        live_orders_forbidden_confirmed=True,
    )
    assert result.outcome is OperatorApprovalIntentOutcome.CREATED
    assert result.intent is not None
    assert result.intent.evidence_sha256 == combined.evidence_result.evidence.evidence_sha256


# --- eligibility: NOT_ELIGIBLE ---


def test_combined_no_go_is_not_eligible() -> None:
    result = _build_intent(_combined_no_go())
    assert result.outcome is OperatorApprovalIntentOutcome.NOT_ELIGIBLE
    assert result.reasons == ("approval_intent_not_eligible",)
    assert result.intent is None


def test_combined_no_go_with_evidence_result_none_is_not_eligible() -> None:
    result = _build_intent(
        _combined_no_go(evidence_result=None, reasons=("candidate_receipt_stale",))
    )
    assert result.outcome is OperatorApprovalIntentOutcome.NOT_ELIGIBLE


def test_evidence_not_eligible_on_no_go_combined_is_not_eligible() -> None:
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.NOT_ELIGIBLE,
        reasons=("candidate_evidence_not_eligible",),
        evidence=None,
    )
    result = _build_intent(_combined_no_go(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.NOT_ELIGIBLE


# --- eligibility: INVALID (wrong object / contradictory PASS) ---


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(object(), id="arbitrary_object"),
        pytest.param(None, id="none"),
        pytest.param("pass", id="str"),
    ],
)
def test_wrong_combined_object_is_invalid(bad: object) -> None:
    result = build_operator_approval_intent(
        combined_result=bad,  # type: ignore[arg-type]
        declared_at=_DECL_AT,
        operator_approval_declared=True,
        writers_stopped_manually_confirmed=True,
        live_orders_forbidden_confirmed=True,
    )
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID
    assert result.reasons == ("approval_intent_invalid_input",)


def test_combined_subclass_is_invalid() -> None:
    class _Sub(FreshnessQualifiedEvidenceResult):
        pass

    sub = _Sub(**asdict(_combined_pass()))  # type: ignore[arg-type]
    result = _build_intent(sub)
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_combined_pass_with_reasons_is_invalid() -> None:
    result = _build_intent(
        _combined_pass(reasons=("candidate_evidence_generation_invalid",))
    )
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_combined_pass_evidence_result_none_is_invalid() -> None:
    result = _build_intent(_combined_pass(evidence_result=None))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_combined_pass_evidence_not_created_is_invalid() -> None:
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.INVALID,
        reasons=("candidate_evidence_invalid_input",),
        evidence=None,
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_combined_pass_evidence_not_eligible_is_invalid() -> None:
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.NOT_ELIGIBLE,
        reasons=("candidate_evidence_not_eligible",),
        evidence=None,
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_combined_pass_missing_evidence_result_attr_is_invalid() -> None:
    class _BrokenCombined:
        outcome = FreshnessQualifiedEvidenceOutcome.PASS
        reasons = ()

    result = build_operator_approval_intent(
        combined_result=_BrokenCombined(),  # type: ignore[arg-type]
        declared_at=_DECL_AT,
        operator_approval_declared=True,
        writers_stopped_manually_confirmed=True,
        live_orders_forbidden_confirmed=True,
    )
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


# --- manual declaration matrix ---


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(False, id="false"),
        pytest.param(0, id="zero"),
        pytest.param(1, id="one"),
        pytest.param(None, id="none"),
        pytest.param("true", id="str_true"),
        pytest.param(object(), id="object"),
    ],
)
def test_operator_approval_declared_must_be_exact_true(bad: object) -> None:
    result = _build_intent(operator_approval_declared=bad)  # type: ignore[arg-type]
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(False, id="false"),
        pytest.param(0, id="zero"),
        pytest.param(1, id="one"),
        pytest.param(None, id="none"),
        pytest.param("true", id="str_true"),
        pytest.param(object(), id="object"),
    ],
)
def test_writers_stopped_must_be_exact_true(bad: object) -> None:
    result = _build_intent(writers_stopped_manually_confirmed=bad)  # type: ignore[arg-type]
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(False, id="false"),
        pytest.param(0, id="zero"),
        pytest.param(1, id="one"),
        pytest.param(None, id="none"),
        pytest.param("true", id="str_true"),
        pytest.param(object(), id="object"),
    ],
)
def test_live_orders_forbidden_must_be_exact_true(bad: object) -> None:
    result = _build_intent(live_orders_forbidden_confirmed=bad)  # type: ignore[arg-type]
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_non_exact_bool_true_declaration_is_invalid() -> None:
    # Python 3 forbids bool subclasses; numpy.bool_(True) is truthy but not type bool.
    np = pytest.importorskip("numpy")
    result = _build_intent(operator_approval_declared=np.bool_(True))  # type: ignore[arg-type]
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


# --- evidence integrity ---


def test_evidence_schema_mismatch_is_invalid() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    tampered = replace(ev, schema_version=1)
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=tampered
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_evidence_hash_malformed_is_invalid() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    tampered = replace(ev, evidence_sha256="NOT_HEX")
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=tampered
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_evidence_hash_recomputation_mismatch_is_invalid() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    tampered = replace(ev, evidence_sha256="b" * 64)
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=tampered
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_evidence_market_invalid_is_invalid() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    tampered = replace(ev, market="US")
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=tampered
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_evidence_symbol_invalid_is_invalid() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    tampered = replace(ev, symbol="ABC")
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=tampered
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_evidence_activation_true_is_invalid() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    tampered = replace(ev, activation_authorized=True)
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=tampered
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_evidence_runtime_go_is_invalid() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    tampered = replace(ev, runtime_activation_outcome="go")
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=tampered
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_evidence_subclass_is_invalid() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None

    class _Sub(ActivationCandidateEvidence):
        pass

    sub = _Sub(**asdict(ev))  # type: ignore[arg-type]
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=sub
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


# --- declared-at binding ---


def test_declared_at_equal_evidence_evaluated_at_creates_intent() -> None:
    result = _build_intent(declared_at=_EVAL_AT)
    assert result.outcome is OperatorApprovalIntentOutcome.CREATED


def test_declared_at_after_evidence_evaluated_at_creates_intent() -> None:
    later = _EVAL_AT + timedelta(hours=1)
    result = _build_intent(declared_at=later)
    assert result.outcome is OperatorApprovalIntentOutcome.CREATED


def test_declared_at_one_microsecond_before_evidence_is_invalid() -> None:
    earlier = _EVAL_AT - timedelta(microseconds=1)
    result = _build_intent(declared_at=earlier)
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


@pytest.mark.parametrize(
    "bad_now",
    [
        pytest.param(datetime(2026, 6, 14, 12, 0, 0), id="naive"),  # noqa: DTZ001
        pytest.param("2026-06-14T12:00:00+09:00", id="str"),
        pytest.param(None, id="none"),
    ],
)
def test_declared_at_invalid_type_is_invalid(bad_now: object) -> None:
    result = build_operator_approval_intent(
        combined_result=_combined_pass(),
        declared_at=bad_now,  # type: ignore[arg-type]
        operator_approval_declared=True,
        writers_stopped_manually_confirmed=True,
        live_orders_forbidden_confirmed=True,
    )
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_declared_at_datetime_subclass_is_invalid() -> None:
    class _DTSub(datetime):
        pass

    sub = _DTSub(2026, 6, 14, 12, 0, 0, tzinfo=_KST)
    result = _build_intent(declared_at=sub)
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


def test_malformed_evidence_evaluated_at_is_invalid() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    tampered = replace(ev, evaluated_at="not-an-iso-datetime")
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=tampered
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID


# --- canonical hash determinism ---


def test_same_input_same_intent_hash() -> None:
    a = _build_intent().intent
    b = _build_intent().intent
    assert a is not None and b is not None
    assert a.approval_intent_sha256 == b.approval_intent_sha256


def test_independent_intent_hash_recomputation_matches() -> None:
    intent = _build_intent().intent
    assert intent is not None
    assert _intent_hash_from(intent) == intent.approval_intent_sha256


def test_evidence_hash_change_changes_intent_hash() -> None:
    base = _build_intent().intent
    assert base is not None
    qual, evaluated_at = ev_helper._consistent(sha="b" * 64)
    er = build_activation_candidate_evidence(qualified_result=qual, evaluated_at=evaluated_at)
    other = _build_intent(_combined_pass(evidence_result=er)).intent
    assert other is not None
    assert other.approval_intent_sha256 != base.approval_intent_sha256


def test_declared_at_change_changes_intent_hash() -> None:
    base = _build_intent(declared_at=_EVAL_AT).intent
    later = _build_intent(declared_at=_EVAL_AT + timedelta(seconds=1)).intent
    assert base is not None and later is not None
    assert base.approval_intent_sha256 != later.approval_intent_sha256


def test_confirmation_false_prevents_intent_not_hash_sensitivity() -> None:
    result = _build_intent(operator_approval_declared=False)
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID
    assert result.intent is None


# --- single-observation ---


def test_builder_uses_single_read_locals_for_combined_and_evidence() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "operator_approval_intent.py"
    ).read_text(encoding="utf-8")
    for snippet in (
        "combined_outcome = combined_result.outcome",
        "combined_reasons = combined_result.reasons",
        "evidence_result = combined_result.evidence_result",
        "er_outcome = evidence_result.outcome",
        "er_reasons = evidence_result.reasons",
        "evidence = evidence_result.evidence",
        "ev_schema_version = evidence.schema_version",
        "ev_evaluated_at = evidence.evaluated_at",
    ):
        assert snippet in source
    assert "asdict(evidence)" not in source


# --- isolation ---


def test_build_intent_reads_no_clock() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "operator_approval_intent.py"
    ).read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "time.time" not in source
    result = _build_intent()
    assert result.outcome is OperatorApprovalIntentOutcome.CREATED


def test_build_intent_does_not_rerun_upstream_stages() -> None:
    combined = _combined_pass()

    with (
        patch.object(evidence_mod, "build_activation_candidate_evidence") as spy_builder,
        patch.object(evidence_mod, "freshness_qualify_activation_candidate") as spy_qualify,
    ):
        result = build_operator_approval_intent(
            combined_result=combined,
            declared_at=_DECL_AT,
            operator_approval_declared=True,
            writers_stopped_manually_confirmed=True,
            live_orders_forbidden_confirmed=True,
        )
    assert result.outcome is OperatorApprovalIntentOutcome.CREATED
    assert spy_builder.call_count == 0
    assert spy_qualify.call_count == 0


def test_no_go_never_creates_intent() -> None:
    result = _build_intent(_combined_no_go())
    assert result.outcome is OperatorApprovalIntentOutcome.NOT_ELIGIBLE
    assert result.intent is None


def test_created_intent_never_sets_activation_authorized_true() -> None:
    result = _build_intent()
    assert result.intent is not None
    assert result.intent.activation_authorized is False
    assert result.intent.runtime_activation_outcome == "no_go"


# --- regression: --run exit 2 ---


def test_run_flag_still_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _load_cli_json(["--run", "--json"], capsys)
    assert code == 2
    assert payload["runtime_activation_outcome"] == "no_go"
    assert payload["activation_authorized"] is False


# --- stable reasons never leak raw values ---


def test_invalid_reason_is_stable_without_raw_hash() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    tampered = replace(ev, evidence_sha256="b" * 64)
    er = ActivationCandidateEvidenceResult(
        outcome=ActivationCandidateEvidenceOutcome.CREATED, reasons=(), evidence=tampered
    )
    result = _build_intent(_combined_pass(evidence_result=er))
    assert result.reasons == ("approval_intent_invalid_input",)
    assert _SHA not in result.reasons[0]
    assert "b" * 64 not in result.reasons[0]


# --- RTM-7c.4o semantic-parity closure: matching hash alone is insufficient ---


def test_p1_semantic_invalid_matching_hash_is_invalid() -> None:
    ev = _evidence_with_matching_hash(
        receipt_sha256="not-a-hash",
        fresh_precheck_receipt_sha256="also-not-a-hash",
        max_age_microseconds=0,
        receipt_age_microseconds=1,
        final_preflight_outcome="no_go",
        freshness_outcome="stale",
        fresh_precheck_executed=False,
        receipt_age_evaluated=False,
        freshness_policy_evaluated=False,
    )
    result = _intent_from_evidence(ev)
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID
    assert result.reasons == ("approval_intent_invalid_input",)
    assert result.intent is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"receipt_sha256": "not-a-hash"}, id="original_receipt_invalid"),
        pytest.param({"fresh_precheck_receipt_sha256": "also-not-a-hash"}, id="fresh_receipt_invalid"),
        pytest.param({"schema_version": True}, id="schema_bool"),
        pytest.param({"schema_version": 2.0}, id="schema_float"),
        pytest.param({"schema_version": "2"}, id="schema_string"),
        pytest.param({"max_age_microseconds": True}, id="max_age_bool"),
        pytest.param({"max_age_microseconds": 1.0}, id="max_age_float"),
        pytest.param({"max_age_microseconds": "300000000"}, id="max_age_string"),
        pytest.param({"max_age_microseconds": -1}, id="max_age_negative"),
        pytest.param({"receipt_age_microseconds": True}, id="receipt_age_bool"),
        pytest.param({"receipt_age_microseconds": 1.0}, id="receipt_age_float"),
        pytest.param({"receipt_age_microseconds": "1000"}, id="receipt_age_string"),
        pytest.param({"receipt_age_microseconds": -1}, id="receipt_age_negative"),
        pytest.param({"max_age_microseconds": 0, "receipt_age_microseconds": 1}, id="age_above_max"),
        pytest.param({"final_preflight_outcome": "no_go"}, id="final_outcome_no_go"),
        pytest.param({"final_preflight_outcome": "unknown"}, id="final_outcome_unknown"),
        pytest.param({"final_preflight_outcome": object()}, id="final_outcome_object"),
        pytest.param({"freshness_outcome": "stale"}, id="freshness_outcome_stale"),
        pytest.param({"freshness_outcome": "no_go"}, id="freshness_outcome_no_go"),
        pytest.param({"freshness_outcome": "unknown"}, id="freshness_outcome_unknown"),
        pytest.param({"freshness_outcome": object()}, id="freshness_outcome_object"),
        pytest.param({"fresh_precheck_executed": False}, id="fresh_executed_false"),
        pytest.param({"fresh_precheck_executed": 0}, id="fresh_executed_zero"),
        pytest.param({"fresh_precheck_executed": 1}, id="fresh_executed_one"),
        pytest.param({"fresh_precheck_executed": None}, id="fresh_executed_none"),
        pytest.param({"fresh_precheck_executed": "true"}, id="fresh_executed_string"),
        pytest.param({"receipt_age_evaluated": False}, id="age_evaluated_false"),
        pytest.param({"receipt_age_evaluated": 0}, id="age_evaluated_zero"),
        pytest.param({"freshness_policy_evaluated": False}, id="policy_evaluated_false"),
        pytest.param({"freshness_policy_evaluated": 1}, id="policy_evaluated_one"),
        pytest.param({"activation_authorized": True}, id="activation_true"),
        pytest.param({"runtime_activation_outcome": "go"}, id="runtime_outcome_go"),
    ],
)
def test_semantic_invalid_matching_hash_matrix(overrides: dict[str, Any]) -> None:
    ev = _evidence_with_matching_hash(**overrides)
    result = _intent_from_evidence(ev)
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID
    assert result.reasons == ("approval_intent_invalid_input",)
    assert result.intent is None


def test_valid_evidence_independent_hash_recomputation_matches() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    fields = asdict(ev)
    fields.pop("evidence_sha256")
    assert _evidence_hash_from_fields(**fields) == ev.evidence_sha256
    validated = validate_activation_candidate_evidence_object(ev)
    assert validated is not None
    assert validated.evidence_sha256 == ev.evidence_sha256


def test_shared_validator_accepts_real_builder_output() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    assert validate_activation_candidate_evidence_object(ev) is not None


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(object(), id="arbitrary_object"),
        pytest.param(None, id="none"),
    ],
)
def test_shared_validator_rejects_wrong_object(bad: object) -> None:
    assert validate_activation_candidate_evidence_object(bad) is None


def test_shared_validator_rejects_evidence_subclass() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None

    class _Sub(ActivationCandidateEvidence):
        pass

    sub = _Sub(**asdict(ev))
    assert validate_activation_candidate_evidence_object(sub) is None


def test_shared_validator_rejects_deleted_field() -> None:
    ev = ev_helper._build().evidence
    assert ev is not None

    class _Broken:
        schema_version = ev.schema_version
        evaluated_at = ev.evaluated_at

    assert validate_activation_candidate_evidence_object(_Broken()) is None


# --- malformed / custom object / deepcopy hook matrix ---


class _PoisonDeepcopy:
    def __deepcopy__(self, memo: dict[int, object]) -> object:
        raise RuntimeError("POISON_EVIDENCE_DEEPCOPY")


class _DeepcopyReturnsSelf:
    def __deepcopy__(self, memo: dict[int, object]) -> "_DeepcopyReturnsSelf":
        return self


class _DeepcopyReturnsOther:
    def __deepcopy__(self, memo: dict[int, object]) -> int:
        return 42


class _Cyclic:
    def __init__(self) -> None:
        self.ref: _Cyclic | None = self


class _VeryDeep:
    def __init__(self, depth: int) -> None:
        self.child = _VeryDeep(depth - 1) if depth > 0 else None


@pytest.mark.parametrize(
    "scalar",
    [
        pytest.param(_PoisonDeepcopy(), id="poison_deepcopy"),
        pytest.param(_DeepcopyReturnsSelf(), id="deepcopy_returns_self"),
        pytest.param(_DeepcopyReturnsOther(), id="deepcopy_returns_other"),
        pytest.param([1, 2, 3], id="list_scalar"),
        pytest.param({"k": "v"}, id="dict_scalar"),
        pytest.param(_Cyclic(), id="cyclic_object"),
        pytest.param(_VeryDeep(50), id="very_deep_object"),
    ],
)
def test_custom_scalar_on_max_age_is_invalid_without_exception(
    scalar: object, capsys: pytest.CaptureFixture[str]
) -> None:
    ev = ev_helper._build().evidence
    assert ev is not None
    tampered = replace(ev, max_age_microseconds=scalar)  # type: ignore[arg-type]
    result = _intent_from_evidence(tampered)
    captured = capsys.readouterr()
    assert result.outcome is OperatorApprovalIntentOutcome.INVALID
    assert result.reasons == ("approval_intent_invalid_input",)
    assert result.intent is None
    combined = captured.out + captured.err
    for forbidden in (
        "POISON_EVIDENCE_DEEPCOPY",
        "RuntimeError",
        "Traceback",
        "/home/",
        "KIS_",
        "APP_KEY",
        "APP_SECRET",
    ):
        assert forbidden not in combined


def test_production_path_has_no_asdict_on_evidence() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "operator_approval_intent.py"
    ).read_text(encoding="utf-8")
    assert "asdict(evidence)" not in source
    assert "from copy import" not in source
    assert "deepcopy(" not in source
    assert "from dataclasses import asdict" not in source


def test_builder_single_read_evidence_field_snippets() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "operator_approval_intent.py"
    ).read_text(encoding="utf-8")
    for snippet in (
        "ev_schema_version = evidence.schema_version",
        "ev_evaluated_at = evidence.evaluated_at",
        "ev_receipt_sha256 = evidence.receipt_sha256",
        "ev_fresh_receipt_sha256 = evidence.fresh_precheck_receipt_sha256",
        "market = evidence.market",
        "symbol = evidence.symbol",
        "ev_max_age = evidence.max_age_microseconds",
        "ev_receipt_age = evidence.receipt_age_microseconds",
        "ev_final_outcome = evidence.final_preflight_outcome",
        "ev_freshness_outcome = evidence.freshness_outcome",
        "ev_fresh_executed = evidence.fresh_precheck_executed",
        "ev_age_evaluated = evidence.receipt_age_evaluated",
        "ev_policy_evaluated = evidence.freshness_policy_evaluated",
        "ev_activation = evidence.activation_authorized",
        "ev_runtime = evidence.runtime_activation_outcome",
        "evidence_sha256 = evidence.evidence_sha256",
        "validate_activation_candidate_evidence_scalars(",
    ):
        assert snippet in source


def test_validate_scalars_rejects_matching_hash_semantic_invalid() -> None:
    ev = _evidence_with_matching_hash(
        receipt_sha256="not-a-hash",
        fresh_precheck_receipt_sha256="also-not-a-hash",
        max_age_microseconds=0,
        receipt_age_microseconds=1,
        final_preflight_outcome="no_go",
        freshness_outcome="stale",
        fresh_precheck_executed=False,
        receipt_age_evaluated=False,
        freshness_policy_evaluated=False,
    )
    assert validate_activation_candidate_evidence_object(ev) is None
    assert (
        validate_activation_candidate_evidence_scalars(
            schema_version=ev.schema_version,
            evaluated_at=ev.evaluated_at,
            receipt_sha256=ev.receipt_sha256,
            fresh_precheck_receipt_sha256=ev.fresh_precheck_receipt_sha256,
            market=ev.market,
            symbol=ev.symbol,
            max_age_microseconds=ev.max_age_microseconds,
            receipt_age_microseconds=ev.receipt_age_microseconds,
            final_preflight_outcome=ev.final_preflight_outcome,
            freshness_outcome=ev.freshness_outcome,
            fresh_precheck_executed=ev.fresh_precheck_executed,
            receipt_age_evaluated=ev.receipt_age_evaluated,
            freshness_policy_evaluated=ev.freshness_policy_evaluated,
            activation_authorized=ev.activation_authorized,
            runtime_activation_outcome=ev.runtime_activation_outcome,
            evidence_sha256=ev.evidence_sha256,
        )
        is None
    )
