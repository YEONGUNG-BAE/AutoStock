"""RTM-7c.4s — Operator approval consumption eligibility preflight tests."""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.activation_candidate_evidence import (
    ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
    ActivationCandidateEvidence,
    FreshnessQualifiedEvidenceOutcome,
    build_activation_candidate_evidence,
    freshness_qualify_and_build_candidate_evidence,
    validate_activation_candidate_evidence_scalars,
)
from composition.operator_approval_consumption_eligibility import (
    OperatorApprovalConsumptionEligibilityOutcome,
    assess_operator_approval_consumption_eligibility,
)
from composition.operator_approval_intent import (
    OperatorApprovalIntentOutcome,
    build_operator_approval_intent,
    operator_approval_intent_hash_payload,
    validate_operator_approval_intent_scalars_detailed,
)
from composition.operator_approval_intent_verifier import (
    OperatorApprovalIntentVerificationOutcome,
    verify_and_snapshot_operator_approval_intent,
    verify_operator_approval_intent_payload,
)
from composition.receipt_freshness_policy import ReceiptFreshnessPolicy
from decision.canonical_json import payload_sha256

import test_activation_candidate_evidence as ev_helper
import test_operator_approval_intent as intent_helper
import test_operator_approval_intent_verifier as verify_helper

_KST = timezone(timedelta(hours=9))
_EVAL_AT = ev_helper._EVAL_AT
_DECL_AT = intent_helper._DECL_AT


def _eligible_inputs(
    *,
    declared_at: datetime = _DECL_AT,
    now: datetime = _DECL_AT,
) -> tuple[dict[str, Any], ActivationCandidateEvidence]:
    built = intent_helper._build_intent(declared_at=declared_at)
    assert built.outcome is OperatorApprovalIntentOutcome.CREATED
    assert built.intent is not None
    ev = build_activation_candidate_evidence(
        qualified_result=ev_helper._qualified_pass(),
        evaluated_at=_EVAL_AT,
    ).evidence
    assert ev is not None
    return asdict(built.intent), ev, now  # type: ignore[return-value]


def _assess(
    intent_payload: object,
    evidence: object,
    now: object,
) -> Any:
    return assess_operator_approval_consumption_eligibility(
        intent_payload=intent_payload,
        evidence=evidence,
        now=now,
    )


# --- normal ---


def test_builder_intent_matching_evidence_equal_now_eligible() -> None:
    payload, ev, now = _eligible_inputs()
    result = _assess(payload, ev, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    assert result.reasons == ()
    assert result.eligibility is not None
    assert result.eligibility.approval_intent_sha256 == payload["approval_intent_sha256"]
    assert result.eligibility.evidence_sha256 == ev.evidence_sha256
    assert result.eligibility.market == "KR"
    assert result.eligibility.symbol == "005930"
    assert result.eligibility.evidence_evaluated_at == _EVAL_AT.isoformat()
    assert result.eligibility.intent_declared_at == _DECL_AT.isoformat()
    assert result.eligibility.checked_at == _DECL_AT.isoformat()
    assert result.eligibility.activation_authorized is False
    assert result.eligibility.runtime_activation_outcome == "no_go"


def test_declared_at_before_now_still_eligible() -> None:
    payload, ev, _ = _eligible_inputs(declared_at=_DECL_AT)
    later_now = _DECL_AT + timedelta(hours=1)
    result = _assess(payload, ev, later_now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE


def test_real_seeded_pipeline_eligible(tmp_path: Path) -> None:
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
    assert combined.evidence_result is not None
    assert combined.evidence_result.evidence is not None
    built = build_operator_approval_intent(
        combined_result=combined,
        declared_at=fr_helper._NOW,
        operator_approval_declared=True,
        writers_stopped_manually_confirmed=True,
        live_orders_forbidden_confirmed=True,
    )
    assert built.intent is not None
    result = _assess(asdict(built.intent), combined.evidence_result.evidence, fr_helper._NOW)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE


def test_result_dataclasses_frozen() -> None:
    payload, ev, now = _eligible_inputs()
    result = _assess(payload, ev, now)
    assert result.eligibility is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.eligibility.market = "US"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = OperatorApprovalConsumptionEligibilityOutcome.NO_GO  # type: ignore[misc]


# --- invalid intent ---


@pytest.mark.parametrize(
    "mutator,expected",
    [
        pytest.param(lambda _: None, "approval_consumption_intent_invalid", id="root_null"),
        pytest.param(lambda _: [], "approval_consumption_intent_invalid", id="root_list"),
        pytest.param(
            lambda p: {**p, "schema_version": 2},
            "approval_consumption_intent_invalid",
            id="unsupported_schema",
        ),
        pytest.param(
            lambda p: {**p, "approval_intent_sha256": "b" * 64},
            "approval_consumption_intent_invalid",
            id="hash_mismatch",
        ),
        pytest.param(
            lambda p: {**p, "operator_approval_declared": False},
            "approval_consumption_intent_invalid",
            id="semantic_invalid",
        ),
        pytest.param(
            lambda p: {**p, "extra": True},
            "approval_consumption_intent_invalid",
            id="unknown_field",
        ),
    ],
)
def test_invalid_intent_matrix(
    mutator: Callable[[dict[str, Any]], object],
    expected: str,
) -> None:
    payload, ev, now = _eligible_inputs()
    if mutator(payload) is None:
        intent: object = None
    elif mutator(payload) == []:
        intent = []
    else:
        intent = mutator(payload)
    result = _assess(intent, ev, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.INVALID
    assert result.reasons == (expected,)
    assert result.eligibility is None


def test_intent_digest_str_subclass_invalid() -> None:
    class _HexStr(str):
        pass

    payload, ev, now = _eligible_inputs()
    payload = dict(payload)
    payload["approval_intent_sha256"] = _HexStr(payload["approval_intent_sha256"])
    result = _assess(payload, ev, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.INVALID
    assert result.reasons == ("approval_consumption_intent_invalid",)


# --- invalid evidence ---


def test_evidence_wrong_type_invalid() -> None:
    payload, _, now = _eligible_inputs()
    result = _assess(payload, {"not": "evidence"}, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.INVALID
    assert result.reasons == ("approval_consumption_evidence_invalid",)


def test_evidence_subclass_invalid() -> None:
    class _Sub(ActivationCandidateEvidence):
        pass

    payload, ev, now = _eligible_inputs()
    sub = _Sub(**asdict(ev))
    result = _assess(payload, sub, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.INVALID
    assert result.reasons == ("approval_consumption_evidence_invalid",)


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"final_preflight_outcome": "no_go"}, id="semantic_no_go"),
        pytest.param({"freshness_outcome": "stale"}, id="semantic_stale"),
        pytest.param({"fresh_precheck_executed": False}, id="false_observation"),
        pytest.param({"receipt_age_microseconds": 999_999_999_999}, id="age_exceeds_max"),
    ],
)
def test_invalid_evidence_semantic_matrix(override: dict[str, Any]) -> None:
    payload, _, now = _eligible_inputs()
    bad = intent_helper._evidence_with_matching_hash(**override)
    result = _assess(payload, bad, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.INVALID
    assert result.reasons == ("approval_consumption_evidence_invalid",)


def test_invalid_evidence_hash_mismatch() -> None:
    payload, ev, now = _eligible_inputs()
    bad = replace(ev, evidence_sha256="d" * 64)
    result = _assess(payload, bad, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.INVALID
    assert result.reasons == ("approval_consumption_evidence_invalid",)


def test_evidence_incomplete_object_invalid() -> None:
    payload, ev, now = _eligible_inputs()

    class _Broken:
        schema_version = ev.schema_version
        evaluated_at = ev.evaluated_at

    result = _assess(payload, _Broken(), now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.INVALID
    assert result.reasons == ("approval_consumption_evidence_invalid",)


# --- binding mismatch ---


def _other_valid_evidence() -> ActivationCandidateEvidence:
    ev = intent_helper._evidence_with_matching_hash(symbol="000660")
    return ev


def test_binding_digest_mismatch_no_go() -> None:
    payload, ev, now = _eligible_inputs()
    other = _other_valid_evidence()
    assert other.evidence_sha256 != ev.evidence_sha256
    result = _assess(payload, other, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.NO_GO
    assert result.reasons == ("approval_consumption_evidence_mismatch",)


def test_binding_market_mismatch_no_go() -> None:
    payload, ev, now = _eligible_inputs()
    tampered = replace(ev, market="KR", evidence_sha256=payload["evidence_sha256"])
    # evidence_sha256 no longer matches tampered content — rebuild valid other symbol instead
    other = _other_valid_evidence()
    result = _assess(payload, other, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.NO_GO
    assert result.reasons == ("approval_consumption_evidence_mismatch",)


def test_binding_symbol_mismatch_no_go() -> None:
    payload, _, now = _eligible_inputs()
    other = _other_valid_evidence()
    result = _assess(payload, other, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.NO_GO
    assert result.reasons == ("approval_consumption_evidence_mismatch",)


def test_binding_schema_field_mismatch_no_go() -> None:
    payload, ev, now = _eligible_inputs()
    # Intent binds schema v2 + digest; evidence with different digest covers schema+identity path.
    other = replace(ev, schema_version=ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION, symbol="000660")
    fields = asdict(other)
    fields["evidence_sha256"] = intent_helper._evidence_hash_from_fields(
        **{k: fields[k] for k in fields if k != "evidence_sha256"}
    )
    other = ActivationCandidateEvidence(**fields)
    assert other.evidence_sha256 != payload["evidence_sha256"]
    result = _assess(payload, other, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.NO_GO
    assert result.reasons == ("approval_consumption_evidence_mismatch",)


# --- time ordering ---


def test_equal_evidence_intent_now_eligible() -> None:
    payload, ev, now = _eligible_inputs(declared_at=_EVAL_AT, now=_EVAL_AT)
    result = _assess(payload, ev, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE


def test_evidence_before_intent_before_now_eligible() -> None:
    evidence_at = _EVAL_AT
    intent_at = _EVAL_AT + timedelta(microseconds=1)
    now_at = _EVAL_AT + timedelta(microseconds=2)
    built = intent_helper._build_intent(declared_at=intent_at)
    assert built.intent is not None
    ev = build_activation_candidate_evidence(
        qualified_result=ev_helper._qualified_pass(),
        evaluated_at=evidence_at,
    ).evidence
    assert ev is not None
    result = _assess(asdict(built.intent), ev, now_at)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE


def test_intent_one_microsecond_before_evidence_no_go() -> None:
    evidence_at = _EVAL_AT
    intent_at = _EVAL_AT - timedelta(microseconds=1)
    built = intent_helper._build_intent(declared_at=evidence_at)
    assert built.intent is not None
    payload = asdict(built.intent)
    payload["declared_at"] = intent_at.isoformat()
    hash_body = operator_approval_intent_hash_payload(
        declared_at=payload["declared_at"],
        evidence_schema_version=payload["evidence_schema_version"],
        evidence_sha256=payload["evidence_sha256"],
        market=payload["market"],
        symbol=payload["symbol"],
    )
    payload["approval_intent_sha256"] = payload_sha256(hash_body)
    ev = build_activation_candidate_evidence(
        qualified_result=ev_helper._qualified_pass(),
        evaluated_at=evidence_at,
    ).evidence
    assert ev is not None
    result = _assess(payload, ev, _EVAL_AT)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.NO_GO
    assert result.reasons == ("approval_consumption_intent_precedes_evidence",)


def test_intent_one_microsecond_after_now_no_go() -> None:
    now_at = _EVAL_AT
    intent_at = _EVAL_AT + timedelta(microseconds=1)
    payload, ev, _ = _eligible_inputs(declared_at=intent_at)
    result = _assess(payload, ev, now_at)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.NO_GO
    assert result.reasons == ("approval_consumption_intent_in_future",)


@pytest.mark.parametrize(
    "now_value",
    [
        pytest.param(None, id="none"),
        pytest.param("2026-06-14T12:00:00+09:00", id="str"),
        pytest.param(1, id="int"),
        pytest.param(datetime(2026, 6, 14, 12, 0, 0), id="naive"),
    ],
)
def test_invalid_now_matrix(now_value: object) -> None:
    payload, ev, _ = _eligible_inputs()
    result = _assess(payload, ev, now_value)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.INVALID
    assert result.reasons == ("approval_consumption_invalid_now",)


class _StatefulTz(tzinfo):
    def __init__(self, *, fail_at: int) -> None:
        self._calls = 0
        self._fail_at = fail_at

    def utcoffset(self, dt: datetime | None) -> timedelta:
        self._calls += 1
        if self._calls >= self._fail_at:
            raise RuntimeError("POISON_NOW_TZ")
        return timedelta(hours=9)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)


def test_stateful_now_tzinfo_invalid() -> None:
    payload, ev, _ = _eligible_inputs()

    class _PoisonNow(datetime):
        def isoformat(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("POISON_NOW_ISO")

    poison_now = _PoisonNow(2026, 6, 14, 12, 0, 0, tzinfo=_KST)
    result = _assess(payload, ev, poison_now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.INVALID
    assert result.reasons == ("approval_consumption_invalid_now",)


def test_no_ttl_or_freshness_recheck_on_eligible() -> None:
    payload, ev, now = _eligible_inputs()
    stale_now = _EVAL_AT + timedelta(days=30)
    result = _assess(payload, ev, stale_now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE


# --- precedence ---


def test_precedence_invalid_now_beats_invalid_intent() -> None:
    result = _assess(None, object(), None)
    assert result.reasons == ("approval_consumption_invalid_now",)


def test_precedence_invalid_intent_beats_invalid_evidence() -> None:
    payload, ev, now = _eligible_inputs()
    bad_payload = {**payload, "schema_version": 99}
    bad_ev = intent_helper._evidence_with_matching_hash(final_preflight_outcome="no_go")
    result = _assess(bad_payload, bad_ev, now)
    assert result.reasons == ("approval_consumption_intent_invalid",)


def test_precedence_valid_intent_invalid_evidence() -> None:
    payload, _, now = _eligible_inputs()
    bad_ev = intent_helper._evidence_with_matching_hash(freshness_outcome="stale")
    result = _assess(payload, bad_ev, now)
    assert result.reasons == ("approval_consumption_evidence_invalid",)


# --- call counts ---


def test_single_execution_call_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    import composition.operator_approval_consumption_eligibility as elig_mod
    import composition.operator_approval_intent_verifier as verifier_mod

    payload, ev, now = _eligible_inputs()

    intent_snap_calls: list[str] = []
    intent_scalar_calls: list[str] = []
    intent_hash_calls: list[str] = []
    public_verify_calls: list[str] = []
    public_snapshot_calls: list[str] = []
    evidence_scalar_calls: list[str] = []
    pipeline_calls: list[str] = []

    real_intent_snap = verifier_mod._snapshot_operator_approval_intent_payload
    real_intent_scalars = verifier_mod.validate_operator_approval_intent_scalars_detailed
    real_intent_hash = verifier_mod.operator_approval_intent_hash_payload
    real_public_verify = verifier_mod.verify_operator_approval_intent_payload
    real_public_snapshot = verifier_mod.verify_and_snapshot_operator_approval_intent
    real_evidence_scalars = elig_mod.validate_activation_candidate_evidence_scalars

    def _spy_intent_snap(p: object) -> tuple[dict[str, object] | None, str | None]:
        intent_snap_calls.append("snap")
        return real_intent_snap(p)

    def _spy_intent_scalars(**kwargs: object) -> object:
        intent_scalar_calls.append("scalars")
        return real_intent_scalars(**kwargs)  # type: ignore[arg-type]

    def _spy_intent_hash(**kwargs: object) -> dict[str, object]:
        intent_hash_calls.append("hash")
        return real_intent_hash(**kwargs)  # type: ignore[arg-type]

    def _spy_public_verify(p: object) -> object:
        public_verify_calls.append("verify")
        return real_public_verify(p)

    def _spy_public_snapshot(p: object) -> object:
        public_snapshot_calls.append("snapshot")
        return real_public_snapshot(p)

    def _spy_evidence_scalars(**kwargs: object) -> object:
        evidence_scalar_calls.append("evidence")
        return real_evidence_scalars(**kwargs)  # type: ignore[arg-type]

    def _boom_pipeline(*_a: object, **_k: object) -> object:
        pipeline_calls.append("pipeline")
        raise AssertionError("upstream pipeline must not run")

    monkeypatch.setattr(verifier_mod, "_snapshot_operator_approval_intent_payload", _spy_intent_snap)
    monkeypatch.setattr(
        verifier_mod, "validate_operator_approval_intent_scalars_detailed", _spy_intent_scalars
    )
    monkeypatch.setattr(verifier_mod, "operator_approval_intent_hash_payload", _spy_intent_hash)
    monkeypatch.setattr(verifier_mod, "verify_operator_approval_intent_payload", _spy_public_verify)
    monkeypatch.setattr(
        elig_mod, "verify_and_snapshot_operator_approval_intent", _spy_public_snapshot
    )
    monkeypatch.setattr(
        elig_mod, "validate_activation_candidate_evidence_scalars", _spy_evidence_scalars
    )
    import composition.activation_candidate_evidence as evidence_mod

    monkeypatch.setattr(
        evidence_mod, "freshness_qualify_and_build_candidate_evidence", _boom_pipeline
    )

    result = _assess(payload, ev, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    assert intent_snap_calls == ["snap"]
    assert intent_scalar_calls == ["scalars"]
    assert intent_hash_calls == ["hash"]
    assert public_verify_calls == []
    assert public_snapshot_calls == ["snapshot"]
    assert evidence_scalar_calls == ["evidence"]
    assert pipeline_calls == []


# --- mutation isolation ---


def test_post_intent_snapshot_payload_mutation_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import composition.operator_approval_intent_verifier as verifier_mod

    payload, ev, now = _eligible_inputs()
    caller = dict(payload)
    original_hash = payload["approval_intent_sha256"]
    real_snap = verifier_mod._snapshot_operator_approval_intent_payload

    def _spy_snap(p: object) -> tuple[dict[str, object] | None, str | None]:
        detached, reason = real_snap(p)
        if type(p) is dict:
            p.clear()
            p["approval_intent_sha256"] = "f" * 64
            p["market"] = "US"
            p["extra"] = True
        return detached, reason

    monkeypatch.setattr(verifier_mod, "_snapshot_operator_approval_intent_payload", _spy_snap)
    result = _assess(caller, ev, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    assert result.eligibility is not None
    assert result.eligibility.approval_intent_sha256 == original_hash


def test_post_evidence_snapshot_mutation_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    payload, ev, now = _eligible_inputs()
    original_digest = ev.evidence_sha256
    original_market = ev.market
    original_symbol = ev.symbol
    original_evaluated = ev.evaluated_at

    import composition.operator_approval_consumption_eligibility as elig_mod

    real_validate = elig_mod._validate_evidence_snapshot

    def _spy_validate(evidence: object) -> Any:
        validated = real_validate(evidence)
        if type(evidence) is ActivationCandidateEvidence:
            object.__setattr__(evidence, "evidence_sha256", "e" * 64)
            object.__setattr__(evidence, "market", "US")
            object.__setattr__(evidence, "symbol", "000001")
            object.__setattr__(evidence, "evaluated_at", "2099-01-01T00:00:00+09:00")
        return validated

    monkeypatch.setattr(elig_mod, "_validate_evidence_snapshot", _spy_validate)
    result = _assess(payload, ev, now)

    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    assert result.eligibility is not None
    assert result.eligibility.evidence_sha256 == original_digest
    assert result.eligibility.market == original_market
    assert result.eligibility.symbol == original_symbol
    assert result.eligibility.evidence_evaluated_at == original_evaluated


# --- detailed intent validator reason map (carry-over H1) ---


@pytest.mark.parametrize(
    "field_overrides,expected_reason",
    [
        pytest.param({"declared_at": "2026-06-14T12:00:00"}, "approval_intent_invalid_declared_at"),
        pytest.param({"evidence_schema_version": 1}, "approval_intent_invalid_evidence_binding"),
        pytest.param({"approval_scope": "other"}, "approval_intent_invalid_scope"),
        pytest.param({"market": "US"}, "approval_intent_invalid_field"),
        pytest.param({"operator_approval_declared": False}, "approval_intent_invalid_declaration"),
        pytest.param({"activation_authorized": True}, "approval_intent_invalid_activation_posture"),
    ],
)
def test_detailed_intent_validator_reason_map(
    field_overrides: dict[str, object],
    expected_reason: str,
) -> None:
    payload = verify_helper._valid_intent_payload()
    result = validate_operator_approval_intent_scalars_detailed(
        schema_version=field_overrides.get("schema_version", payload["schema_version"]),
        declared_at=field_overrides.get("declared_at", payload["declared_at"]),
        evidence_schema_version=field_overrides.get(
            "evidence_schema_version", payload["evidence_schema_version"]
        ),
        evidence_sha256=field_overrides.get("evidence_sha256", payload["evidence_sha256"]),
        market=field_overrides.get("market", payload["market"]),
        symbol=field_overrides.get("symbol", payload["symbol"]),
        approval_scope=field_overrides.get("approval_scope", payload["approval_scope"]),
        operator_approval_declared=field_overrides.get(
            "operator_approval_declared", payload["operator_approval_declared"]
        ),
        writers_stopped_manually_confirmed=field_overrides.get(
            "writers_stopped_manually_confirmed",
            payload["writers_stopped_manually_confirmed"],
        ),
        live_orders_forbidden_confirmed=field_overrides.get(
            "live_orders_forbidden_confirmed",
            payload["live_orders_forbidden_confirmed"],
        ),
        activation_authorized=field_overrides.get(
            "activation_authorized", payload["activation_authorized"]
        ),
        runtime_activation_outcome=field_overrides.get(
            "runtime_activation_outcome", payload["runtime_activation_outcome"]
        ),
        approval_intent_sha256=field_overrides.get(
            "approval_intent_sha256", payload["approval_intent_sha256"]
        ),
    )
    assert result.validated is None
    assert result.reason_code == expected_reason


def test_detailed_validator_single_owner_in_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    import composition.operator_approval_intent as intent_mod
    import composition.operator_approval_intent_verifier as verifier_mod

    calls: list[str] = []
    real = intent_mod.validate_operator_approval_intent_scalars_detailed

    def _spy(**kwargs: object) -> object:
        calls.append("detailed")
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(verifier_mod, "validate_operator_approval_intent_scalars_detailed", _spy)
    result = verify_operator_approval_intent_payload(verify_helper._valid_intent_payload())
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.VALID
    assert calls == ["detailed"]


# --- regression hooks ---


def test_regression_verified_snapshot_still_valid() -> None:
    payload = verify_helper._valid_intent_payload()
    snap = verify_and_snapshot_operator_approval_intent(payload)
    assert snap.outcome is OperatorApprovalIntentVerificationOutcome.VALID
    assert snap.snapshot is not None


# --- carry-over H1: exact built-in datetime + stateful custom tzinfo ---


class _StatefulNowTz(tzinfo):
    """Custom tzinfo that raises after a configurable number of utcoffset reads."""

    def __init__(self, *, fail_at: int) -> None:
        self.calls = 0
        self._fail_at = fail_at

    def utcoffset(self, dt: datetime | None) -> timedelta:
        self.calls += 1
        if self.calls >= self._fail_at:
            raise RuntimeError("POISON_STATEFUL_NOW_TZ")
        return timedelta(hours=9)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)


def test_exact_datetime_stateful_tz_first_read_fails_invalid() -> None:
    payload, ev, _ = _eligible_inputs()
    tz = _StatefulNowTz(fail_at=1)
    now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=tz)
    assert type(now) is datetime  # exact built-in, not a subclass
    result = _assess(payload, ev, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.INVALID
    assert result.reasons == ("approval_consumption_invalid_now",)
    assert result.eligibility is None


def test_exact_datetime_stateful_tz_detached_after_first_read() -> None:
    payload, ev, _ = _eligible_inputs()
    tz = _StatefulNowTz(fail_at=2)
    now = datetime(2026, 6, 14, 12, 0, 0, tzinfo=tz)
    assert type(now) is datetime
    result = _assess(payload, ev, now)
    # First isoformat() observation succeeds; detached parsed datetime is used afterward,
    # so the caller tzinfo is never read a second time even though it would now raise.
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    assert tz.calls == 1


# --- carry-over H2: _binding_matches single-field root-cause matrix ---


def _validated_binding_pair() -> tuple[Any, Any]:
    import composition.activation_candidate_evidence as evidence_mod

    payload, ev, _ = _eligible_inputs()
    snap = verify_and_snapshot_operator_approval_intent(payload)
    assert snap.snapshot is not None
    validated = evidence_mod.validate_activation_candidate_evidence_scalars(**asdict(ev))
    assert validated is not None
    return snap.snapshot, validated


def test_binding_matches_valid_pair_true() -> None:
    import composition.operator_approval_consumption_eligibility as elig_mod

    intent, evidence = _validated_binding_pair()
    assert elig_mod._binding_matches(intent, evidence) is True


def test_binding_intent_evidence_schema_version_mismatch() -> None:
    import composition.operator_approval_consumption_eligibility as elig_mod

    intent, evidence = _validated_binding_pair()
    tampered = replace(intent, evidence_schema_version=1)
    assert elig_mod._binding_matches(tampered, evidence) is False


def test_binding_evidence_schema_version_mismatch() -> None:
    import composition.operator_approval_consumption_eligibility as elig_mod

    intent, evidence = _validated_binding_pair()
    tampered = replace(evidence, schema_version=1)
    assert elig_mod._binding_matches(intent, tampered) is False


def test_binding_evidence_digest_mismatch() -> None:
    import composition.operator_approval_consumption_eligibility as elig_mod

    intent, evidence = _validated_binding_pair()
    tampered = replace(evidence, evidence_sha256="d" * 64)
    assert elig_mod._binding_matches(intent, tampered) is False


def test_binding_intent_market_mismatch() -> None:
    import composition.operator_approval_consumption_eligibility as elig_mod

    intent, evidence = _validated_binding_pair()
    tampered = replace(intent, market="US")
    assert elig_mod._binding_matches(tampered, evidence) is False


def test_binding_evidence_market_mismatch() -> None:
    import composition.operator_approval_consumption_eligibility as elig_mod

    intent, evidence = _validated_binding_pair()
    tampered = replace(evidence, market="US")
    assert elig_mod._binding_matches(intent, tampered) is False


def test_binding_intent_symbol_mismatch() -> None:
    import composition.operator_approval_consumption_eligibility as elig_mod

    intent, evidence = _validated_binding_pair()
    tampered = replace(intent, symbol="000660")
    assert elig_mod._binding_matches(tampered, evidence) is False


def test_binding_evidence_symbol_mismatch() -> None:
    import composition.operator_approval_consumption_eligibility as elig_mod

    intent, evidence = _validated_binding_pair()
    tampered = replace(evidence, symbol="000660")
    assert elig_mod._binding_matches(intent, tampered) is False


# --- carry-over H3: evidence canonical hash call count on ELIGIBLE path ---


def test_evidence_hash_call_count_single(monkeypatch: pytest.MonkeyPatch) -> None:
    import composition.activation_candidate_evidence as evidence_mod
    import composition.operator_approval_consumption_eligibility as elig_mod

    payload, ev, now = _eligible_inputs()

    evidence_validate_calls: list[str] = []
    evidence_hash_payload_calls: list[str] = []
    evidence_sha_calls: list[str] = []

    real_validate = elig_mod.validate_activation_candidate_evidence_scalars
    real_hash_payload = evidence_mod.activation_candidate_evidence_hash_payload
    real_sha = evidence_mod.payload_sha256

    def _spy_validate(**kwargs: object) -> object:
        evidence_validate_calls.append("validate")
        return real_validate(**kwargs)  # type: ignore[arg-type]

    def _spy_hash_payload(**kwargs: object) -> object:
        evidence_hash_payload_calls.append("hash_payload")
        return real_hash_payload(**kwargs)  # type: ignore[arg-type]

    def _spy_sha(value: object) -> str:
        evidence_sha_calls.append("sha")
        return real_sha(value)

    monkeypatch.setattr(
        elig_mod, "validate_activation_candidate_evidence_scalars", _spy_validate
    )
    monkeypatch.setattr(
        evidence_mod, "activation_candidate_evidence_hash_payload", _spy_hash_payload
    )
    monkeypatch.setattr(evidence_mod, "payload_sha256", _spy_sha)

    result = _assess(payload, ev, now)
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    assert evidence_validate_calls == ["validate"]
    assert evidence_hash_payload_calls == ["hash_payload"]
    # payload_sha256 in the evidence module is used exactly once for the evidence digest
    # (the intent digest uses the verifier module's own payload_sha256 binding).
    assert evidence_sha_calls == ["sha"]
