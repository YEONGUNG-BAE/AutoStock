"""RTM-7c.4t — Operator approval consumption eligibility artifact builder tests."""

from __future__ import annotations

import dataclasses
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.activation_candidate_evidence import (
    ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
)
from composition.operator_approval_consumption_eligibility import (
    OperatorApprovalConsumptionEligibility,
    OperatorApprovalConsumptionEligibilityOutcome,
    OperatorApprovalConsumptionEligibilityResult,
    assess_operator_approval_consumption_eligibility,
)
from composition.operator_approval_consumption_eligibility_artifact import (
    OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION,
    OperatorApprovalConsumptionEligibilityArtifact,
    OperatorApprovalConsumptionEligibilityArtifactOutcome,
    build_operator_approval_consumption_eligibility_artifact,
    operator_approval_consumption_eligibility_artifact_hash_payload,
)
from composition.operator_approval_intent import OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION
from decision.canonical_json import payload_sha256

import composition.operator_approval_consumption_eligibility_artifact as artifact_mod
import test_operator_approval_consumption_eligibility as elig_helper

_EXPECTED_HASH_FIELDS = {
    "schema_version",
    "checked_at",
    "approval_intent_schema_version",
    "approval_intent_sha256",
    "candidate_evidence_schema_version",
    "candidate_evidence_sha256",
    "market",
    "symbol",
    "evidence_evaluated_at",
    "intent_declared_at",
    "activation_authorized",
    "runtime_activation_outcome",
}


def _eligible_result() -> OperatorApprovalConsumptionEligibilityResult:
    payload, ev, now = elig_helper._eligible_inputs()
    result = assess_operator_approval_consumption_eligibility(
        intent_payload=payload, evidence=ev, now=now
    )
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    assert result.eligibility is not None
    return result


def _build(result: object) -> Any:
    return build_operator_approval_consumption_eligibility_artifact(result)


# --- CREATED ---


def test_created_all_fields() -> None:
    result = _eligible_result()
    elig = result.eligibility
    assert elig is not None
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.CREATED
    assert out.reasons == ()
    art = out.artifact
    assert art is not None
    assert art.schema_version == OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION == 1
    assert art.checked_at == elig.checked_at
    assert art.approval_intent_schema_version == OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION == 1
    assert art.approval_intent_sha256 == elig.approval_intent_sha256
    assert (
        art.candidate_evidence_schema_version
        == ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION
        == 2
    )
    assert art.candidate_evidence_sha256 == elig.evidence_sha256
    assert art.market == elig.market == "KR"
    assert art.symbol == elig.symbol
    assert art.evidence_evaluated_at == elig.evidence_evaluated_at
    assert art.intent_declared_at == elig.intent_declared_at
    assert art.activation_authorized is False
    assert art.runtime_activation_outcome == "no_go"
    assert len(art.eligibility_artifact_sha256) == 64


def test_created_digests_lower_hex64() -> None:
    out = _build(_eligible_result())
    art = out.artifact
    assert art is not None
    for digest in (
        art.approval_intent_sha256,
        art.candidate_evidence_sha256,
        art.eligibility_artifact_sha256,
    ):
        assert len(digest) == 64
        assert digest == digest.lower()
        int(digest, 16)


def test_created_frozen_artifact_and_result() -> None:
    out = _build(_eligible_result())
    assert out.artifact is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.artifact.market = "US"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.outcome = OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID  # type: ignore[misc]


# --- NOT_ELIGIBLE ---


def test_not_eligible_valid_no_go_shape() -> None:
    result = OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.NO_GO,
        reasons=("approval_consumption_evidence_mismatch",),
        eligibility=None,
    )
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.NOT_ELIGIBLE
    assert out.reasons == ("approval_consumption_artifact_not_eligible",)
    assert out.artifact is None


def test_not_eligible_no_hash_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _boom(**_k: object) -> object:
        calls.append("hash")
        raise AssertionError("hash must not run for NO_GO")

    monkeypatch.setattr(
        artifact_mod, "operator_approval_consumption_eligibility_artifact_hash_payload", _boom
    )
    result = OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.NO_GO,
        reasons=("r",),
        eligibility=None,
    )
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.NOT_ELIGIBLE
    assert calls == []


# --- malformed NO_GO matrix → INVALID ---


def test_malformed_no_go_empty_reasons_invalid() -> None:
    result = OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.NO_GO,
        reasons=(),
        eligibility=None,
    )
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID
    assert out.reasons == ("approval_consumption_artifact_invalid_input",)
    assert out.artifact is None


def test_malformed_no_go_with_eligibility_invalid() -> None:
    valid = _eligible_result().eligibility
    result = OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.NO_GO,
        reasons=("reason",),
        eligibility=valid,
    )
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID
    assert out.artifact is None


def test_malformed_no_go_list_reasons_invalid() -> None:
    result = OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.NO_GO,
        reasons=[],  # type: ignore[arg-type]
        eligibility=None,
    )
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID
    assert out.artifact is None


def test_malformed_no_go_object_eligibility_invalid() -> None:
    result = OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.NO_GO,
        reasons=("reason",),
        eligibility=object(),  # type: ignore[arg-type]
    )
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID
    assert out.artifact is None


def test_malformed_no_go_tuple_subclass_reasons_invalid() -> None:
    class _T(tuple):  # type: ignore[type-arg]
        pass

    result = OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.NO_GO,
        reasons=_T(("r",)),
        eligibility=None,
    )
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID
    assert out.artifact is None


# --- INVALID outcome / root type ---


def test_root_wrong_type_invalid() -> None:
    out = _build({"outcome": "eligible"})
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID
    assert out.reasons == ("approval_consumption_artifact_invalid_input",)
    assert out.artifact is None


def test_root_subclass_invalid() -> None:
    class _Sub(OperatorApprovalConsumptionEligibilityResult):
        pass

    result = _eligible_result()
    sub = _Sub(outcome=result.outcome, reasons=result.reasons, eligibility=result.eligibility)
    out = _build(sub)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID


def test_upstream_invalid_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _boom(**_k: object) -> object:
        calls.append("hash")
        raise AssertionError("hash must not run for INVALID")

    monkeypatch.setattr(
        artifact_mod, "operator_approval_consumption_eligibility_artifact_hash_payload", _boom
    )
    result = OperatorApprovalConsumptionEligibilityResult(
        outcome=OperatorApprovalConsumptionEligibilityOutcome.INVALID,
        reasons=("approval_consumption_invalid_now",),
        eligibility=None,
    )
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID
    assert out.artifact is None
    assert calls == []


# --- ELIGIBLE outer-shape rejection ---


@pytest.mark.parametrize(
    "reasons",
    [
        pytest.param(("nonempty",), id="nonempty_tuple"),
        pytest.param([], id="list"),
        pytest.param(None, id="none"),
    ],
)
def test_eligible_bad_reasons_invalid(reasons: object) -> None:
    result = replace(_eligible_result(), reasons=reasons)  # type: ignore[arg-type]
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID
    assert out.artifact is None


def test_eligible_missing_eligibility_invalid() -> None:
    result = replace(_eligible_result(), eligibility=None)
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID


def test_eligible_wrong_eligibility_type_invalid() -> None:
    result = replace(_eligible_result(), eligibility=object())  # type: ignore[arg-type]
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID


def test_eligible_eligibility_subclass_invalid() -> None:
    class _Sub(OperatorApprovalConsumptionEligibility):
        pass

    base = _eligible_result().eligibility
    assert base is not None
    sub = _Sub(**dataclasses.asdict(base))
    result = replace(_eligible_result(), eligibility=sub)
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID


# --- nested semantic validation ---


def _eligible_with(**overrides: object) -> OperatorApprovalConsumptionEligibilityResult:
    result = _eligible_result()
    elig = replace(result.eligibility, **overrides)  # type: ignore[arg-type]
    return replace(result, eligibility=elig)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"approval_intent_sha256": "x" * 64}, id="intent_hash_nonhex"),
        pytest.param({"approval_intent_sha256": "A" * 64}, id="intent_hash_uppercase"),
        pytest.param({"approval_intent_sha256": "a" * 63}, id="intent_hash_short"),
        pytest.param({"evidence_sha256": "z" * 64}, id="evidence_hash_nonhex"),
        pytest.param({"market": "US"}, id="market_us"),
        pytest.param({"market": "kr"}, id="market_lower"),
        pytest.param({"symbol": "00593"}, id="symbol_short"),
        pytest.param({"symbol": "00593A"}, id="symbol_alpha"),
        pytest.param({"runtime_activation_outcome": "go"}, id="runtime_go"),
        pytest.param({"evidence_evaluated_at": "2026-06-14T12:00:00"}, id="evidence_naive"),
        pytest.param({"intent_declared_at": "not-a-date"}, id="intent_malformed"),
        pytest.param({"checked_at": "2026-06-14T12:00:00"}, id="checked_naive"),
    ],
)
def test_nested_invalid_matrix(overrides: dict[str, object]) -> None:
    out = _build(_eligible_with(**overrides))
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID
    assert out.reasons == ("approval_consumption_artifact_invalid_input",)
    assert out.artifact is None


def test_nested_activation_authorized_true_invalid() -> None:
    out = _build(_eligible_with(activation_authorized=True))
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID


def test_nested_hash_str_subclass_invalid() -> None:
    class _HexStr(str):
        pass

    base = _eligible_result().eligibility
    assert base is not None
    out = _build(_eligible_with(approval_intent_sha256=_HexStr(base.approval_intent_sha256)))
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID


# --- time ordering ---


def test_time_ordering_intent_before_evidence_invalid() -> None:
    result = _eligible_result()
    elig = result.eligibility
    assert elig is not None
    from datetime import datetime

    ev_dt = datetime.fromisoformat(elig.evidence_evaluated_at)
    earlier = (ev_dt - timedelta(seconds=1)).isoformat()
    out = _build(_eligible_with(intent_declared_at=earlier))
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID


def test_time_ordering_intent_after_checked_invalid() -> None:
    result = _eligible_result()
    elig = result.eligibility
    assert elig is not None
    from datetime import datetime

    chk_dt = datetime.fromisoformat(elig.checked_at)
    later = (chk_dt + timedelta(seconds=1)).isoformat()
    out = _build(_eligible_with(intent_declared_at=later))
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID


# --- canonical hash ---


def test_hash_payload_exact_fields() -> None:
    result = _eligible_result()
    elig = result.eligibility
    assert elig is not None
    payload = operator_approval_consumption_eligibility_artifact_hash_payload(
        checked_at=elig.checked_at,
        approval_intent_sha256=elig.approval_intent_sha256,
        candidate_evidence_sha256=elig.evidence_sha256,
        market=elig.market,
        symbol=elig.symbol,
        evidence_evaluated_at=elig.evidence_evaluated_at,
        intent_declared_at=elig.intent_declared_at,
    )
    assert set(payload.keys()) == _EXPECTED_HASH_FIELDS
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"
    assert payload["schema_version"] == 1
    assert payload["approval_intent_schema_version"] == OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION
    assert (
        payload["candidate_evidence_schema_version"]
        == ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION
    )


def test_hash_deterministic_and_independent_recomputation() -> None:
    result = _eligible_result()
    out1 = _build(result)
    out2 = _build(result)
    assert out1.artifact is not None and out2.artifact is not None
    assert out1.artifact.eligibility_artifact_sha256 == out2.artifact.eligibility_artifact_sha256

    art = out1.artifact
    recomputed = payload_sha256(
        operator_approval_consumption_eligibility_artifact_hash_payload(
            checked_at=art.checked_at,
            approval_intent_sha256=art.approval_intent_sha256,
            candidate_evidence_sha256=art.candidate_evidence_sha256,
            market=art.market,
            symbol=art.symbol,
            evidence_evaluated_at=art.evidence_evaluated_at,
            intent_declared_at=art.intent_declared_at,
        )
    )
    assert recomputed == art.eligibility_artifact_sha256


def test_hash_field_sensitivity() -> None:
    result = _eligible_result()
    elig = result.eligibility
    assert elig is not None
    base_kwargs = dict(
        checked_at=elig.checked_at,
        approval_intent_sha256=elig.approval_intent_sha256,
        candidate_evidence_sha256=elig.evidence_sha256,
        market=elig.market,
        symbol=elig.symbol,
        evidence_evaluated_at=elig.evidence_evaluated_at,
        intent_declared_at=elig.intent_declared_at,
    )
    base_digest = payload_sha256(
        operator_approval_consumption_eligibility_artifact_hash_payload(**base_kwargs)
    )
    variants = {
        "checked_at": "2030-01-01T00:00:00+09:00",
        "approval_intent_sha256": "a" * 64,
        "candidate_evidence_sha256": "b" * 64,
        "market": "US",
        "symbol": "000660",
        "evidence_evaluated_at": "2030-01-01T00:00:00+09:00",
        "intent_declared_at": "2030-01-01T00:00:00+09:00",
    }
    for field, value in variants.items():
        kwargs = dict(base_kwargs)
        kwargs[field] = value
        digest = payload_sha256(
            operator_approval_consumption_eligibility_artifact_hash_payload(**kwargs)
        )
        assert digest != base_digest, field


# --- single-observation / mutation isolation ---


def test_single_read_mutation_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _eligible_result()
    elig = result.eligibility
    assert elig is not None
    original_intent = elig.approval_intent_sha256
    original_evidence = elig.evidence_sha256
    original_market = elig.market
    original_symbol = elig.symbol
    original_checked = elig.checked_at

    real = artifact_mod.operator_approval_consumption_eligibility_artifact_hash_payload

    def _spy(**kwargs: object) -> object:
        # Mutate caller objects AFTER locals were captured; artifact must reflect pre-mutation.
        object.__setattr__(result, "outcome", OperatorApprovalConsumptionEligibilityOutcome.NO_GO)
        object.__setattr__(result, "reasons", ("late",))
        object.__setattr__(elig, "approval_intent_sha256", "f" * 64)
        object.__setattr__(elig, "evidence_sha256", "e" * 64)
        object.__setattr__(elig, "market", "US")
        object.__setattr__(elig, "symbol", "000001")
        object.__setattr__(elig, "checked_at", "2099-01-01T00:00:00+09:00")
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        artifact_mod, "operator_approval_consumption_eligibility_artifact_hash_payload", _spy
    )
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.CREATED
    art = out.artifact
    assert art is not None
    assert art.approval_intent_sha256 == original_intent
    assert art.candidate_evidence_sha256 == original_evidence
    assert art.market == original_market
    assert art.symbol == original_symbol
    assert art.checked_at == original_checked


def test_build_emits_from_validated_content_not_raw_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Carry-over H1: hash + artifact must be emitted from content.validated, not the raw
    # eligibility locals. Force the content owner to return a validated snapshot whose values
    # differ from the raw input and assert the artifact (and its digest) reflect the validated
    # snapshot — proving a single emission source.
    from composition.operator_approval_consumption_eligibility_artifact import (
        OperatorApprovalConsumptionEligibilityArtifactContentScalarValidation,
        ValidatedOperatorApprovalConsumptionEligibilityArtifactContent,
        operator_approval_consumption_eligibility_artifact_hash_payload,
    )

    result = _eligible_result()
    elig = result.eligibility
    assert elig is not None

    validated = ValidatedOperatorApprovalConsumptionEligibilityArtifactContent(
        schema_version=1,
        checked_at=elig.checked_at,
        approval_intent_schema_version=OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
        approval_intent_sha256="a" * 64,
        candidate_evidence_schema_version=2,
        candidate_evidence_sha256="b" * 64,
        market="KR",
        symbol="000660",
        evidence_evaluated_at=elig.evidence_evaluated_at,
        intent_declared_at=elig.intent_declared_at,
        activation_authorized=False,
        runtime_activation_outcome="no_go",
    )

    def _fake_content(**_k: object) -> object:
        return OperatorApprovalConsumptionEligibilityArtifactContentScalarValidation(
            validated=validated, reason_code=None
        )

    monkeypatch.setattr(
        artifact_mod,
        "validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed",
        _fake_content,
    )
    out = _build(result)
    art = out.artifact
    assert art is not None
    assert art.approval_intent_sha256 == "a" * 64
    assert art.candidate_evidence_sha256 == "b" * 64
    assert art.symbol == "000660"
    expected_digest = payload_sha256(
        operator_approval_consumption_eligibility_artifact_hash_payload(
            checked_at=validated.checked_at,
            approval_intent_sha256="a" * 64,
            candidate_evidence_sha256="b" * 64,
            market="KR",
            symbol="000660",
            evidence_evaluated_at=validated.evidence_evaluated_at,
            intent_declared_at=validated.intent_declared_at,
        )
    )
    assert art.eligibility_artifact_sha256 == expected_digest


# --- isolation ---


def test_no_upstream_or_io(monkeypatch: pytest.MonkeyPatch) -> None:
    import composition.activation_candidate_evidence as evidence_mod
    import composition.operator_approval_consumption_eligibility as elig_mod
    import composition.operator_approval_intent_verifier as verifier_mod

    def _boom_assess(*_a: object, **_k: object) -> object:
        raise AssertionError("upstream eligibility API must not run")

    def _boom_verify(*_a: object, **_k: object) -> object:
        raise AssertionError("intent verifier must not run")

    def _boom_evidence(*_a: object, **_k: object) -> object:
        raise AssertionError("evidence validator must not run")

    monkeypatch.setattr(
        elig_mod, "assess_operator_approval_consumption_eligibility", _boom_assess
    )
    monkeypatch.setattr(
        verifier_mod, "verify_and_snapshot_operator_approval_intent", _boom_verify
    )
    monkeypatch.setattr(
        evidence_mod, "validate_activation_candidate_evidence_scalars", _boom_evidence
    )

    sha_calls: list[str] = []
    hash_payload_calls: list[str] = []
    real_sha = artifact_mod.payload_sha256
    real_hash = artifact_mod.operator_approval_consumption_eligibility_artifact_hash_payload

    def _spy_sha(value: object) -> str:
        sha_calls.append("sha")
        return real_sha(value)

    def _spy_hash(**kwargs: object) -> object:
        hash_payload_calls.append("hash")
        return real_hash(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(artifact_mod, "payload_sha256", _spy_sha)
    monkeypatch.setattr(
        artifact_mod, "operator_approval_consumption_eligibility_artifact_hash_payload", _spy_hash
    )

    result = _eligible_result()
    out = _build(result)
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.CREATED
    assert sha_calls == ["sha"]
    assert hash_payload_calls == ["hash"]


# --- exception contract ---


def test_normal_exception_sanitized_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(value: object) -> str:
        raise ValueError("SECRET_LEAK_/etc/passwd")

    monkeypatch.setattr(artifact_mod, "_parse_aware", lambda v: _raise(v))
    out = _build(_eligible_result())
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.INVALID
    assert out.reasons == ("approval_consumption_artifact_invalid_input",)
    assert "SECRET_LEAK" not in repr(out.reasons)
    assert "passwd" not in repr(out.reasons)


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_fatal_exceptions_reraise(monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]) -> None:
    def _raise(**_k: object) -> object:
        raise exc()

    monkeypatch.setattr(
        artifact_mod, "operator_approval_consumption_eligibility_artifact_hash_payload", _raise
    )
    with pytest.raises(exc):
        _build(_eligible_result())


# --- shared content owner: single-call discipline ---


def test_build_calls_content_validator_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = artifact_mod.validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed

    def _spy(**kwargs: object) -> object:
        calls.append("content")
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        artifact_mod,
        "validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed",
        _spy,
    )
    out = _build(_eligible_result())
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.CREATED
    assert calls == ["content"]


def test_build_single_hash_and_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    hash_calls: list[str] = []
    sha_calls: list[str] = []
    real_hash = artifact_mod.operator_approval_consumption_eligibility_artifact_hash_payload
    real_sha = artifact_mod.payload_sha256

    def _hash_spy(**kwargs: object) -> object:
        hash_calls.append("hash")
        return real_hash(**kwargs)  # type: ignore[arg-type]

    def _sha_spy(value: object) -> str:
        sha_calls.append("sha")
        return real_sha(value)

    monkeypatch.setattr(
        artifact_mod, "operator_approval_consumption_eligibility_artifact_hash_payload", _hash_spy
    )
    monkeypatch.setattr(artifact_mod, "payload_sha256", _sha_spy)
    out = _build(_eligible_result())
    assert out.outcome is OperatorApprovalConsumptionEligibilityArtifactOutcome.CREATED
    assert hash_calls == ["hash"]
    assert sha_calls == ["sha"]
