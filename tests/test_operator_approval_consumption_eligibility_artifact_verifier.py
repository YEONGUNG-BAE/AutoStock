"""RTM-7c.4u — Operator approval consumption eligibility artifact verifier + snapshot tests."""

from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.activation_candidate_evidence import (
    ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
)
from composition.operator_approval_consumption_eligibility import (
    OperatorApprovalConsumptionEligibilityOutcome,
    assess_operator_approval_consumption_eligibility,
)
from composition.operator_approval_consumption_eligibility_artifact import (
    OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_FIELD_NAMES,
    build_operator_approval_consumption_eligibility_artifact,
    operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars,
)
from composition.operator_approval_consumption_eligibility_artifact_verifier import (
    OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome as VOut,
    VerifiedOperatorApprovalConsumptionEligibilityArtifact,
    verify_and_snapshot_operator_approval_consumption_eligibility_artifact as verify_snapshot,
    verify_operator_approval_consumption_eligibility_artifact_payload as verify,
)
from decision.canonical_json import payload_sha256

import composition.operator_approval_consumption_eligibility_artifact_verifier as ver_mod
import composition.operator_approval_consumption_eligibility_artifact as art_mod
import test_operator_approval_consumption_eligibility as elig_helper

_FIELDS = {
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
    "eligibility_artifact_sha256",
}


def _valid_payload() -> dict[str, object]:
    payload, ev, now = elig_helper._eligible_inputs()
    result = assess_operator_approval_consumption_eligibility(
        intent_payload=payload, evidence=ev, now=now
    )
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    art = build_operator_approval_consumption_eligibility_artifact(result).artifact
    assert art is not None
    return dataclasses.asdict(art)


def _payload_with(**overrides: object) -> dict[str, object]:
    d = _valid_payload()
    d.update(overrides)
    return d


def _rehashed(**overrides: object) -> dict[str, object]:
    """Apply overrides to the 12 content fields and recompute the digest to match."""

    d = _valid_payload()
    d.update(overrides)
    d["eligibility_artifact_sha256"] = payload_sha256(
        operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars(
            schema_version=d["schema_version"],
            checked_at=d["checked_at"],
            approval_intent_schema_version=d["approval_intent_schema_version"],
            approval_intent_sha256=d["approval_intent_sha256"],
            candidate_evidence_schema_version=d["candidate_evidence_schema_version"],
            candidate_evidence_sha256=d["candidate_evidence_sha256"],
            market=d["market"],
            symbol=d["symbol"],
            evidence_evaluated_at=d["evidence_evaluated_at"],
            intent_declared_at=d["intent_declared_at"],
            activation_authorized=d["activation_authorized"],
            runtime_activation_outcome=d["runtime_activation_outcome"],
        )
    )
    return d


# --- field-set parity ---


def test_field_names_set_exact() -> None:
    assert set(OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_FIELD_NAMES) == _FIELDS
    assert len(OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_FIELD_NAMES) == 13


# --- builder -> verifier invariant ---


def test_builder_to_verifier_valid() -> None:
    d = _valid_payload()
    assert set(d) == _FIELDS
    ver = verify(d)
    assert ver.outcome is VOut.VALID
    assert ver.reason_codes == ()
    assert ver.schema_version == 1
    assert ver.approval_intent_schema_version == 1
    assert ver.candidate_evidence_schema_version == ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION
    assert ver.eligibility_artifact_sha256 == d["eligibility_artifact_sha256"]


def test_builder_to_snapshot_exact_13_fields() -> None:
    d = _valid_payload()
    res = verify_snapshot(d)
    assert res.outcome is VOut.VALID
    assert res.reason_codes == ()
    assert res.snapshot is not None
    assert dataclasses.asdict(res.snapshot) == d


def test_snapshot_frozen() -> None:
    res = verify_snapshot(_valid_payload())
    assert res.snapshot is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.snapshot.market = "US"  # type: ignore[misc]


# --- verifier <-> snapshot parity ---


def test_verifier_snapshot_parity_valid() -> None:
    d = _valid_payload()
    a = verify(d)
    b = verify_snapshot(d)
    assert a.outcome is b.outcome
    assert a.reason_codes == b.reason_codes
    assert b.snapshot is not None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_payload_with(market="US"), id="market"),
        pytest.param(_payload_with(schema_version=2), id="schema"),
        pytest.param(_payload_with(eligibility_artifact_sha256="a" * 64), id="hash"),
    ],
)
def test_verifier_snapshot_parity_invalid(payload: dict[str, object]) -> None:
    a = verify(payload)
    b = verify_snapshot(payload)
    assert a.outcome is VOut.INVALID
    assert b.outcome is VOut.INVALID
    assert a.reason_codes == b.reason_codes
    assert b.snapshot is None


# --- root / field set ---


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="list"),
        pytest.param("x", id="string"),
        pytest.param(None, id="none"),
        pytest.param(42, id="int"),
    ],
)
def test_root_not_object(payload: object) -> None:
    ver = verify(payload)
    assert ver.outcome is VOut.INVALID
    assert ver.reason_codes == ("eligibility_artifact_not_object",)


def test_dict_subclass_rejected() -> None:
    class _D(dict):  # type: ignore[type-arg]
        pass

    ver = verify(_D(_valid_payload()))
    assert ver.outcome is VOut.INVALID
    assert ver.reason_codes == ("eligibility_artifact_not_object",)


def test_unknown_field() -> None:
    ver = verify(_payload_with(extra="x"))
    assert ver.reason_codes == ("eligibility_artifact_unknown_field",)


def test_missing_field() -> None:
    d = _valid_payload()
    del d["symbol"]
    ver = verify(d)
    assert ver.reason_codes == ("eligibility_artifact_missing_field",)


def test_str_subclass_key() -> None:
    class _K(str):
        pass

    d = _valid_payload()
    val = d.pop("market")
    d[_K("market")] = val
    ver = verify(d)
    assert ver.outcome is VOut.INVALID
    assert ver.reason_codes == ("eligibility_artifact_unknown_field",)


def test_non_string_key() -> None:
    d = _valid_payload()
    d[123] = "x"  # type: ignore[index]
    ver = verify(d)
    assert ver.reason_codes == ("eligibility_artifact_unknown_field",)


# --- schema versions ---


@pytest.mark.parametrize(
    "value,reason",
    [
        pytest.param(2, "eligibility_artifact_unsupported_schema", id="schema2"),
        pytest.param(True, "eligibility_artifact_invalid_field", id="bool"),
        pytest.param("1", "eligibility_artifact_invalid_field", id="string"),
    ],
)
def test_artifact_schema_version(value: object, reason: str) -> None:
    ver = verify(_payload_with(schema_version=value))
    assert ver.reason_codes == (reason,)


@pytest.mark.parametrize(
    "field",
    ["approval_intent_schema_version", "candidate_evidence_schema_version"],
)
@pytest.mark.parametrize(
    "value",
    [pytest.param(99, id="mismatch"), pytest.param(True, id="bool")],
)
def test_binding_schema_versions(field: str, value: object) -> None:
    ver = verify(_payload_with(**{field: value}))
    assert ver.reason_codes == ("eligibility_artifact_invalid_binding",)


# --- digests ---


@pytest.mark.parametrize(
    "field,reason",
    [
        ("approval_intent_sha256", "eligibility_artifact_invalid_binding"),
        ("candidate_evidence_sha256", "eligibility_artifact_invalid_binding"),
        ("eligibility_artifact_sha256", "eligibility_artifact_invalid_field"),
    ],
)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("x" * 64, id="malformed"),
        pytest.param("A" * 64, id="uppercase"),
        pytest.param("a" * 63, id="short"),
        pytest.param(b"a" * 64, id="bytes"),
        pytest.param(None, id="none"),
        pytest.param(123, id="int"),
    ],
)
def test_digest_invalid(field: str, reason: str, value: object) -> None:
    ver = verify(_payload_with(**{field: value}))
    assert ver.reason_codes == (reason,)


@pytest.mark.parametrize(
    "field,reason",
    [
        ("approval_intent_sha256", "eligibility_artifact_invalid_binding"),
        ("candidate_evidence_sha256", "eligibility_artifact_invalid_binding"),
        ("eligibility_artifact_sha256", "eligibility_artifact_invalid_field"),
    ],
)
def test_digest_str_subclass(field: str, reason: str) -> None:
    class _Hex(str):
        pass

    d = _valid_payload()
    ver = verify(_payload_with(**{field: _Hex(d[field])}))
    assert ver.reason_codes == (reason,)


def test_semantically_valid_digest_change_with_recomputed_artifact_hash_is_valid() -> None:
    # The verifier is a consistency checker, not an authenticator. A semantically valid
    # binding digest paired with a correctly recomputed artifact digest is VALID by design
    # (Case C — consistency, not authenticity/provenance).
    ver = verify(_rehashed(approval_intent_sha256="a" * 64))
    assert ver.outcome is VOut.VALID
    assert ver.reason_codes == ()


def test_semantically_valid_symbol_change_with_recomputed_hash_is_valid() -> None:
    ver = verify(_rehashed(symbol="000660"))
    assert ver.outcome is VOut.VALID
    assert ver.reason_codes == ()


def test_semantically_valid_checked_at_change_with_recomputed_hash_is_valid() -> None:
    ver = verify(_rehashed(checked_at="2099-01-01T00:00:00+00:00"))
    assert ver.outcome is VOut.VALID
    assert ver.reason_codes == ()


def test_semantically_valid_change_with_stale_hash_is_hash_mismatch() -> None:
    # Case B — a semantically valid content change whose stored digest was NOT recomputed
    # is INVALID/hash_mismatch (the consistency check fails).
    ver = verify(_payload_with(symbol="000660"))
    assert ver.reason_codes == ("eligibility_artifact_hash_mismatch",)


def test_stale_hash_mismatch() -> None:
    ver = verify(_payload_with(eligibility_artifact_sha256="a" * 64))
    assert ver.reason_codes == ("eligibility_artifact_hash_mismatch",)


# --- identity ---


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("US", id="us"),
        pytest.param("", id="empty"),
        pytest.param("kr", id="lower"),
    ],
)
def test_market_invalid(value: object) -> None:
    ver = verify(_payload_with(market=value))
    assert ver.reason_codes == ("eligibility_artifact_invalid_field",)


def test_market_str_subclass() -> None:
    class _S(str):
        pass

    ver = verify(_payload_with(market=_S("KR")))
    assert ver.reason_codes == ("eligibility_artifact_invalid_field",)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("00593", id="short"),
        pytest.param("0059300", id="long"),
        pytest.param("00593A", id="alpha"),
        pytest.param("００５９３０", id="fullwidth"),
        pytest.param("٠٠٥٩٣٠", id="arabic_indic"),
        pytest.param("0059 3", id="space"),
    ],
)
def test_symbol_invalid(value: object) -> None:
    ver = verify(_payload_with(symbol=value))
    assert ver.reason_codes == ("eligibility_artifact_invalid_field",)


# --- timestamps ---


@pytest.mark.parametrize(
    "field",
    ["checked_at", "evidence_evaluated_at", "intent_declared_at"],
)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("not-a-date", id="malformed"),
        pytest.param("2026-06-14T12:00:00", id="naive"),
        pytest.param(123, id="non_string"),
        pytest.param(None, id="none"),
    ],
)
def test_timestamp_invalid(field: str, value: object) -> None:
    ver = verify(_payload_with(**{field: value}))
    assert ver.reason_codes == ("eligibility_artifact_invalid_timestamp",)


def test_timestamp_str_subclass() -> None:
    class _S(str):
        pass

    d = _valid_payload()
    ver = verify(_payload_with(checked_at=_S(d["checked_at"])))
    assert ver.reason_codes == ("eligibility_artifact_invalid_timestamp",)


def test_time_ordering_evidence_after_intent() -> None:
    d = _valid_payload()
    intent_dt = datetime.fromisoformat(d["intent_declared_at"])  # type: ignore[arg-type]
    later = (intent_dt + timedelta(seconds=1)).isoformat()
    ver = verify(_rehashed(evidence_evaluated_at=later))
    assert ver.reason_codes == ("eligibility_artifact_invalid_time_ordering",)


def test_time_ordering_intent_after_checked() -> None:
    d = _valid_payload()
    checked_dt = datetime.fromisoformat(d["checked_at"])  # type: ignore[arg-type]
    later = (checked_dt + timedelta(seconds=1)).isoformat()
    ver = verify(_rehashed(intent_declared_at=later))
    assert ver.reason_codes == ("eligibility_artifact_invalid_time_ordering",)


# --- posture ---


@pytest.mark.parametrize(
    "value",
    [pytest.param(True, id="true"), pytest.param(0, id="0"), pytest.param(1, id="1"), pytest.param(None, id="none")],
)
def test_activation_posture_invalid(value: object) -> None:
    ver = verify(_payload_with(activation_authorized=value))
    assert ver.reason_codes == ("eligibility_artifact_invalid_activation_posture",)


@pytest.mark.parametrize(
    "value",
    [pytest.param("go", id="go"), pytest.param(123, id="non_string"), pytest.param("NO_GO", id="case")],
)
def test_runtime_outcome_invalid(value: object) -> None:
    ver = verify(_payload_with(runtime_activation_outcome=value))
    assert ver.reason_codes == ("eligibility_artifact_invalid_activation_posture",)


def test_runtime_outcome_str_subclass() -> None:
    class _S(str):
        pass

    ver = verify(_payload_with(runtime_activation_outcome=_S("no_go")))
    assert ver.reason_codes == ("eligibility_artifact_invalid_activation_posture",)


# --- canonical hash: semantic-invalid + recomputed digest still INVALID ---


@pytest.mark.parametrize(
    "overrides,reason",
    [
        pytest.param({"market": "US"}, "eligibility_artifact_invalid_field", id="market"),
        pytest.param({"symbol": "00593"}, "eligibility_artifact_invalid_field", id="symbol"),
        pytest.param({"schema_version": 2}, "eligibility_artifact_unsupported_schema", id="schema"),
        pytest.param({"runtime_activation_outcome": "go"}, "eligibility_artifact_invalid_activation_posture", id="runtime"),
        pytest.param({"activation_authorized": True}, "eligibility_artifact_invalid_activation_posture", id="activation"),
        pytest.param({"approval_intent_schema_version": 99}, "eligibility_artifact_invalid_binding", id="intent_schema"),
    ],
)
def test_semantic_invalid_recomputed_hash_still_invalid(
    overrides: dict[str, object], reason: str
) -> None:
    ver = verify(_rehashed(**overrides))
    assert ver.outcome is VOut.INVALID
    assert ver.reason_codes == (reason,)


def test_each_content_field_changes_digest() -> None:
    base = _valid_payload()
    base_digest = base["eligibility_artifact_sha256"]
    variants = {
        "checked_at": "2030-01-01T00:00:00+09:00",
        "approval_intent_sha256": "a" * 64,
        "candidate_evidence_sha256": "b" * 64,
        "market": "US",
        "symbol": "000660",
        "evidence_evaluated_at": "2030-01-01T00:00:00+09:00",
        "intent_declared_at": "2030-01-01T00:00:00+09:00",
        "schema_version": 2,
        "approval_intent_schema_version": 9,
        "candidate_evidence_schema_version": 9,
        "activation_authorized": True,
        "runtime_activation_outcome": "go",
    }
    for field, value in variants.items():
        d = _rehashed(**{field: value})
        assert d["eligibility_artifact_sha256"] != base_digest, field


# --- detached snapshot / mutation isolation ---


def test_caller_mutation_after_snapshot_no_effect() -> None:
    d = _valid_payload()
    res = verify_snapshot(d)
    assert res.snapshot is not None
    before = dataclasses.asdict(res.snapshot)
    d.clear()
    d["market"] = "US"
    d["eligibility_artifact_sha256"] = "f" * 64
    assert dataclasses.asdict(res.snapshot) == before
    assert res.outcome is VOut.VALID


def test_observes_items_once() -> None:
    calls: list[str] = []
    d = _valid_payload()

    class _Spy(dict):  # type: ignore[type-arg]
        def items(self):  # type: ignore[no-untyped-def]
            calls.append("items")
            return super().items()

    # _Spy is a dict subclass → rejected as not_object before items() is consulted.
    ver = verify(_Spy(d))
    assert ver.reason_codes == ("eligibility_artifact_not_object",)
    assert calls == []


def test_single_hash_computation(monkeypatch: pytest.MonkeyPatch) -> None:
    sha_calls: list[str] = []
    real = ver_mod.payload_sha256

    def _spy(value: object) -> str:
        sha_calls.append("sha")
        return real(value)

    monkeypatch.setattr(ver_mod, "payload_sha256", _spy)
    ver = verify(_valid_payload())
    assert ver.outcome is VOut.VALID
    assert sha_calls == ["sha"]


def test_no_validator_double_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = ver_mod.validate_operator_approval_consumption_eligibility_artifact_scalars_detailed

    def _spy(**kwargs: object) -> object:
        calls.append("validate")
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        ver_mod,
        "validate_operator_approval_consumption_eligibility_artifact_scalars_detailed",
        _spy,
    )
    verify(_valid_payload())
    assert calls == ["validate"]


# --- exceptions / sanitization ---


def test_normal_exception_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(value: object) -> str:
        raise ValueError("SECRET_LEAK_/etc/passwd")

    monkeypatch.setattr(ver_mod, "payload_sha256", lambda v: _raise(v))
    ver = verify(_valid_payload())
    assert ver.outcome is VOut.INVALID
    assert ver.reason_codes == ("eligibility_artifact_invalid_field",)
    assert "SECRET_LEAK" not in repr(ver.reason_codes)
    assert "passwd" not in repr(ver.reason_codes)


def test_normal_exception_snapshot_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(value: object) -> str:
        raise KeyError("boom")

    monkeypatch.setattr(ver_mod, "payload_sha256", lambda v: _raise(v))
    res = verify_snapshot(_valid_payload())
    assert res.outcome is VOut.INVALID
    assert res.reason_codes == ("eligibility_artifact_invalid_field",)
    assert res.snapshot is None


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_fatal_exceptions_reraise(
    monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
) -> None:
    def _raise(value: object) -> str:
        raise exc()

    monkeypatch.setattr(ver_mod, "payload_sha256", lambda v: _raise(v))
    with pytest.raises(exc):
        verify(_valid_payload())
    with pytest.raises(exc):
        verify_snapshot(_valid_payload())


# --- snapshot holds no raw reference ---


def test_snapshot_scalars_only() -> None:
    res = verify_snapshot(_valid_payload())
    assert res.snapshot is not None
    for value in dataclasses.asdict(res.snapshot).values():
        assert type(value) in (int, str, bool)
    assert isinstance(res.snapshot, VerifiedOperatorApprovalConsumptionEligibilityArtifact)


# --- shared content owner: verifier path single-call discipline ---


def test_verify_uses_shared_content_owner_once(monkeypatch: pytest.MonkeyPatch) -> None:
    full_calls: list[str] = []
    content_calls: list[str] = []
    real_full = art_mod.validate_operator_approval_consumption_eligibility_artifact_scalars_detailed
    real_content = (
        art_mod.validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed
    )

    def _full_spy(**kwargs: object) -> object:
        full_calls.append("full")
        return real_full(**kwargs)  # type: ignore[arg-type]

    def _content_spy(**kwargs: object) -> object:
        content_calls.append("content")
        return real_content(**kwargs)  # type: ignore[arg-type]

    payload = _valid_payload()  # build before patching so only verify is counted
    monkeypatch.setattr(
        ver_mod,
        "validate_operator_approval_consumption_eligibility_artifact_scalars_detailed",
        _full_spy,
    )
    monkeypatch.setattr(
        art_mod,
        "validate_operator_approval_consumption_eligibility_artifact_content_scalars_detailed",
        _content_spy,
    )
    ver = verify(payload)
    assert ver.outcome is VOut.VALID
    assert full_calls == ["full"]
    assert content_calls == ["content"]


# --- builder -> verifier parity matrix ---


@pytest.mark.parametrize(
    "payload_factory,expected",
    [
        pytest.param(lambda: _valid_payload(), VOut.VALID, id="builder_created"),
        pytest.param(lambda: _rehashed(approval_intent_sha256="a" * 64), VOut.VALID, id="alt_digest"),
        pytest.param(lambda: _rehashed(symbol="000660"), VOut.VALID, id="alt_symbol"),
        pytest.param(
            lambda: _rehashed(checked_at="2099-01-01T00:00:00+00:00"), VOut.VALID, id="alt_checked_at"
        ),
        pytest.param(lambda: _payload_with(schema_version=2), VOut.INVALID, id="unsupported_schema"),
        pytest.param(lambda: _payload_with(market="US"), VOut.INVALID, id="invalid_market"),
        pytest.param(lambda: _payload_with(symbol="000660"), VOut.INVALID, id="stale_hash"),
        pytest.param(
            lambda: _payload_with(eligibility_artifact_sha256="a" * 64), VOut.INVALID, id="bad_digest"
        ),
    ],
)
def test_builder_verifier_parity_matrix(payload_factory, expected) -> None:
    payload = payload_factory()
    a = verify(payload)
    b = verify_snapshot(payload)
    assert a.outcome is expected
    assert b.outcome is expected
    assert a.reason_codes == b.reason_codes
    if expected is VOut.VALID:
        assert a.reason_codes == ()
        assert b.snapshot is not None
    else:
        assert b.snapshot is None
