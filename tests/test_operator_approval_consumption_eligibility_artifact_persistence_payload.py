"""RTM-7c.4w — Canonical eligibility-artifact persistence-payload encode/decode tests.

API-only. No filesystem read/write, no path handling, no persistence, no consumption, no
signing/authentication, no activation authorization. Decode VALID = schema/semantic/hash
consistency only, never authenticity/provenance/persistence.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.operator_approval_consumption_eligibility import (
    OperatorApprovalConsumptionEligibilityOutcome,
    assess_operator_approval_consumption_eligibility,
)
from composition.operator_approval_consumption_eligibility_artifact import (
    OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION,
    build_operator_approval_consumption_eligibility_artifact,
    operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars,
)
from composition.activation_candidate_evidence import ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION
from composition.operator_approval_intent import OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION
from composition.operator_approval_consumption_eligibility_artifact_verifier import (
    OperatorApprovalConsumptionEligibilityArtifactVerification as _Verif,
    OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome as _VerifOutcome,
    VerifiedOperatorApprovalConsumptionEligibilityArtifact,
    VerifiedOperatorApprovalConsumptionEligibilityArtifactResult as _SnapResult,
    validate_operator_approval_consumption_eligibility_artifact_verification_invariants,
    verify_and_snapshot_operator_approval_consumption_eligibility_artifact,
)
import composition.operator_approval_consumption_eligibility_artifact_persistence_payload as mod
from composition.operator_approval_consumption_eligibility_artifact_persistence_payload import (
    ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES,
    EligibilityArtifactPersistencePayloadOutcome,
    EligibilityArtifactPersistencePayloadVerificationOutcome,
    decode_operator_approval_consumption_eligibility_artifact_payload as decode,
    encode_verified_operator_approval_consumption_eligibility_artifact as encode,
)
from decision.canonical_json import canonical_json_dumps, payload_sha256

import test_operator_approval_consumption_eligibility as elig_helper

_HEX64 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _valid_payload() -> dict[str, object]:
    payload, ev, now = elig_helper._eligible_inputs()
    result = assess_operator_approval_consumption_eligibility(
        intent_payload=payload, evidence=ev, now=now
    )
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    art = build_operator_approval_consumption_eligibility_artifact(result).artifact
    assert art is not None
    return dataclasses.asdict(art)


def _valid_snapshot() -> VerifiedOperatorApprovalConsumptionEligibilityArtifact:
    snap = verify_and_snapshot_operator_approval_consumption_eligibility_artifact(
        _valid_payload()
    ).snapshot
    assert snap is not None
    return snap


def _rehashed(**overrides: object) -> dict[str, object]:
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


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return canonical_json_dumps(payload).encode("utf-8")


# --- encoder ---


def test_encode_valid_snapshot_is_created() -> None:
    snap = _valid_snapshot()
    res = encode(snap)
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.CREATED
    assert res.reason_codes == ()
    assert isinstance(res.payload_bytes, bytes)
    assert res.eligibility_artifact_sha256 == snap.eligibility_artifact_sha256


def test_encode_canonical_bytes_have_no_newline_or_bom() -> None:
    res = encode(_valid_snapshot())
    assert res.payload_bytes[:1] != b"\xef"  # no UTF-8 BOM
    assert not res.payload_bytes.startswith(b"\xef\xbb\xbf")
    assert not res.payload_bytes.endswith(b"\n")
    assert res.payload_bytes.startswith(b"{")
    assert res.payload_bytes.endswith(b"}")


def test_encode_is_deterministic() -> None:
    snap = _valid_snapshot()
    assert encode(snap).payload_bytes == encode(snap).payload_bytes


def test_encode_bytes_decode_to_exact_13_field_dict() -> None:
    res = encode(_valid_snapshot())
    d = json.loads(res.payload_bytes)
    assert isinstance(d, dict)
    assert len(d) == 13


def test_encode_bytes_have_no_path_or_secret() -> None:
    blob = encode(_valid_snapshot()).payload_bytes.decode("utf-8")
    assert "/" not in blob  # no raw path separators
    assert "secret" not in blob.lower()
    assert "config" not in blob.lower()


@pytest.mark.parametrize("bad", [None, object(), {}, 42, "x", b"bytes"])
def test_encode_wrong_object_is_invalid(bad: object) -> None:
    res = encode(bad)
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID
    assert res.reason_codes == ("eligibility_persistence_payload_invalid_snapshot",)
    assert res.payload_bytes is None
    assert res.eligibility_artifact_sha256 is None


def test_encode_subclass_is_invalid() -> None:
    class _Sub(VerifiedOperatorApprovalConsumptionEligibilityArtifact):
        pass

    base = _valid_snapshot()
    sub = _Sub(**dataclasses.asdict(base))
    res = encode(sub)
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID
    assert res.payload_bytes is None


def test_encode_deleted_field_is_invalid() -> None:
    snap = _valid_snapshot()
    object.__delattr__(snap, "market")
    res = encode(snap)
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID
    assert res.payload_bytes is None


def test_encode_malformed_scalar_is_invalid() -> None:
    snap = _valid_snapshot()
    object.__setattr__(snap, "market", "US")  # not KR
    res = encode(snap)
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID
    assert res.payload_bytes is None


def test_encode_stale_hash_is_invalid() -> None:
    snap = _valid_snapshot()
    object.__setattr__(snap, "eligibility_artifact_sha256", _HEX64)
    res = encode(snap)
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID
    assert res.payload_bytes is None


def test_encode_semantic_invalid_with_self_consistent_hash_is_invalid() -> None:
    # A snapshot tampered to an invalid posture, even with a recomputed digest, must not encode.
    snap = _valid_snapshot()
    object.__setattr__(snap, "activation_authorized", True)
    object.__setattr__(
        snap,
        "eligibility_artifact_sha256",
        payload_sha256(
            operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars(
                schema_version=snap.schema_version,
                checked_at=snap.checked_at,
                approval_intent_schema_version=snap.approval_intent_schema_version,
                approval_intent_sha256=snap.approval_intent_sha256,
                candidate_evidence_schema_version=snap.candidate_evidence_schema_version,
                candidate_evidence_sha256=snap.candidate_evidence_sha256,
                market=snap.market,
                symbol=snap.symbol,
                evidence_evaluated_at=snap.evidence_evaluated_at,
                intent_declared_at=snap.intent_declared_at,
                activation_authorized=True,
                runtime_activation_outcome=snap.runtime_activation_outcome,
            )
        ),
    )
    res = encode(snap)
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID


def test_encode_caller_mutation_after_call_does_not_change_result() -> None:
    snap = _valid_snapshot()
    res = encode(snap)
    object.__setattr__(snap, "market", "US")
    res2 = encode(_valid_snapshot())
    assert res.payload_bytes == res2.payload_bytes


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_encode_fatal_exception_propagates(
    monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
) -> None:
    def _raise(_p: object) -> object:
        raise exc()

    monkeypatch.setattr(
        mod, "verify_operator_approval_consumption_eligibility_artifact_payload", _raise
    )
    with pytest.raises(exc):
        encode(_valid_snapshot())


def test_encode_ordinary_exception_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_p: object) -> object:
        raise ValueError("SECRET_LEAK_/home/user/APP_SECRET")

    monkeypatch.setattr(
        mod, "verify_operator_approval_consumption_eligibility_artifact_payload", _raise
    )
    res = encode(_valid_snapshot())
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID
    blob = json.dumps([res.reason_codes, res.eligibility_artifact_sha256])
    assert "SECRET_LEAK" not in blob and "APP_SECRET" not in blob and "/home/" not in blob


def test_encode_call_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_verify = mod.verify_operator_approval_consumption_eligibility_artifact_payload
    real_dumps = mod.canonical_json_dumps

    def _v(p: object) -> object:
        calls.append("verify")
        return real_verify(p)

    def _d(v: object) -> str:
        calls.append("dumps")
        return real_dumps(v)

    monkeypatch.setattr(
        mod, "verify_operator_approval_consumption_eligibility_artifact_payload", _v
    )
    monkeypatch.setattr(mod, "canonical_json_dumps", _d)
    encode(_valid_snapshot())
    assert calls == ["verify", "dumps"]


# --- decoder ---


def test_decode_canonical_bytes_is_valid() -> None:
    res = encode(_valid_snapshot())
    dec = decode(res.payload_bytes)
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.VALID
    assert dec.reason_codes == ()
    assert dec.snapshot is not None


@pytest.mark.parametrize(
    "bad",
    [None, 42, "string", bytearray(b"{}"), memoryview(b"{}"), object()],
)
def test_decode_non_bytes_is_invalid(bad: object) -> None:
    dec = decode(bad)
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID
    assert dec.reason_codes == ("eligibility_persistence_payload_not_bytes",)
    assert dec.snapshot is None


def test_decode_bytes_subclass_is_invalid() -> None:
    class _B(bytes):
        pass

    dec = decode(_B(encode(_valid_snapshot()).payload_bytes))
    assert dec.reason_codes == ("eligibility_persistence_payload_not_bytes",)


def test_decode_empty_is_invalid() -> None:
    dec = decode(b"")
    assert dec.reason_codes == ("eligibility_persistence_payload_empty",)


def test_decode_invalid_utf8_is_invalid() -> None:
    dec = decode(b"\xff\xfe")
    assert dec.reason_codes == ("eligibility_persistence_payload_not_utf8",)


def test_decode_invalid_json_is_invalid() -> None:
    dec = decode(b"not json")
    assert dec.reason_codes == ("eligibility_persistence_payload_not_json",)


@pytest.mark.parametrize(
    "data",
    [b'{"a": 1, "a": 2}', b'{"a": {"b": 1, "b": 2}}'],
    ids=["top", "nested"],
)
def test_decode_duplicate_key_is_invalid(data: bytes) -> None:
    dec = decode(data)
    assert dec.reason_codes == ("eligibility_persistence_payload_duplicate_key",)


@pytest.mark.parametrize("data", [b"NaN", b"Infinity", b"-Infinity"])
def test_decode_non_finite_is_invalid(data: bytes) -> None:
    dec = decode(data)
    assert dec.reason_codes == ("eligibility_persistence_payload_not_json",)


def test_decode_too_deep_is_invalid() -> None:
    dec = decode(b"[" * 5000 + b"0" + b"]" * 5000)
    assert dec.reason_codes == ("eligibility_persistence_payload_too_deep",)


def test_decode_exact_limit_not_too_large() -> None:
    data = b" " * (ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES - 2) + b"{}"
    assert len(data) == ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES
    dec = decode(data)
    # Parsed (an empty object) then artifact verifier rejects → not a too_large boundary.
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID
    assert dec.reason_codes == ("eligibility_artifact_missing_field",)


def test_decode_over_limit_is_too_large() -> None:
    data = b"x" * (ELIGIBILITY_ARTIFACT_PERSISTENCE_PAYLOAD_LIMIT_BYTES + 1)
    dec = decode(data)
    assert dec.reason_codes == ("eligibility_persistence_payload_too_large",)


@pytest.mark.parametrize("data", [b"[]", b'"x"', b"null"], ids=["list", "string", "null"])
def test_decode_root_non_object_is_not_object(data: bytes) -> None:
    dec = decode(data)
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID
    assert dec.reason_codes == ("eligibility_artifact_not_object",)


def test_decode_missing_field_preserves_verifier_reason() -> None:
    d = _valid_payload()
    del d["market"]
    dec = decode(_canonical_bytes(d))
    assert dec.reason_codes == ("eligibility_artifact_missing_field",)


def test_decode_invalid_field_preserves_verifier_reason() -> None:
    dec = decode(_canonical_bytes(_rehashed(symbol="ABCDEF")))  # non-numeric symbol
    assert dec.reason_codes == ("eligibility_artifact_invalid_field",)
    assert dec.snapshot is None


def test_decode_hash_mismatch_preserves_verifier_reason() -> None:
    # Tamper a content field WITHOUT recomputing the digest → hash_mismatch.
    d = _valid_payload()
    d["symbol"] = "000001" if d["symbol"] != "000001" else "000002"
    dec = decode(_canonical_bytes(d))
    assert dec.reason_codes == ("eligibility_artifact_hash_mismatch",)


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_decode_fatal_exception_propagates(
    monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
) -> None:
    def _raise(_t: object) -> object:
        raise exc()

    monkeypatch.setattr(mod, "parse_receipt_stdin_json", _raise)
    with pytest.raises(exc):
        decode(encode(_valid_snapshot()).payload_bytes)


def test_decode_ordinary_exception_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_t: object) -> object:
        raise RuntimeError("SECRET_LEAK_/home/user/APP_SECRET")

    monkeypatch.setattr(mod, "parse_receipt_stdin_json", _raise)
    dec = decode(encode(_valid_snapshot()).payload_bytes)
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID
    assert "SECRET_LEAK" not in json.dumps(list(dec.reason_codes))


def test_decode_call_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_parse = mod.parse_receipt_stdin_json
    real_vs = mod.verify_and_snapshot_operator_approval_consumption_eligibility_artifact

    def _p(t: object) -> object:
        calls.append("parse")
        return real_parse(t)

    def _vs(p: object) -> object:
        calls.append("verify_and_snapshot")
        return real_vs(p)

    monkeypatch.setattr(mod, "parse_receipt_stdin_json", _p)
    monkeypatch.setattr(
        mod,
        "verify_and_snapshot_operator_approval_consumption_eligibility_artifact",
        _vs,
    )
    decode(encode(_valid_snapshot()).payload_bytes)
    assert calls == ["parse", "verify_and_snapshot"]


def test_decode_does_not_retain_raw_bytes() -> None:
    payload = encode(_valid_snapshot()).payload_bytes
    dec = decode(payload)
    for value in vars(dec).values():
        assert value is not payload


# --- round-trip / A-B-C consistency ---


def test_round_trip_13_field_equality() -> None:
    snap = _valid_snapshot()
    dec = decode(encode(snap).payload_bytes)
    assert dataclasses.asdict(dec.snapshot) == dataclasses.asdict(snap)


def test_round_trip_re_encode_byte_equality() -> None:
    snap = _valid_snapshot()
    enc = encode(snap)
    re_enc = encode(decode(enc.payload_bytes).snapshot)
    assert re_enc.payload_bytes == enc.payload_bytes


def test_consistency_A_original_round_trips() -> None:
    dec = decode(encode(_valid_snapshot()).payload_bytes)
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.VALID


def test_consistency_B_freshly_minted_self_consistent_payload_is_valid() -> None:
    # A different-symbol payload with a recomputed digest decodes VALID — that is schema/semantic/
    # hash CONSISTENCY, not proof of provenance/authenticity/persistence.
    minted = _rehashed(symbol="000020")
    dec = decode(_canonical_bytes(minted))
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.VALID
    assert dec.snapshot.symbol == "000020"


def test_consistency_C_tampered_without_rehash_is_invalid() -> None:
    d = _valid_payload()
    d["symbol"] = "000020"  # digest NOT recomputed
    dec = decode(_canonical_bytes(d))
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID
    assert dec.reason_codes == ("eligibility_artifact_hash_mismatch",)


# --- encoder malformed verifier-result matrix (P1-A) ---


def _verif(**kw: object) -> _Verif:
    base: dict[str, object] = {
        "outcome": _VerifOutcome.INVALID,
        "schema_version": None,
        "approval_intent_schema_version": None,
        "approval_intent_sha256": None,
        "candidate_evidence_schema_version": None,
        "candidate_evidence_sha256": None,
        "eligibility_artifact_sha256": None,
        "reason_codes": ("eligibility_artifact_invalid_field",),
    }
    base.update(kw)
    return _Verif(**base)  # type: ignore[arg-type]


def _valid_verif(**kw: object) -> _Verif:
    snap = _valid_snapshot()
    valid: dict[str, object] = {
        "outcome": _VerifOutcome.VALID,
        "schema_version": snap.schema_version,
        "approval_intent_schema_version": snap.approval_intent_schema_version,
        "approval_intent_sha256": snap.approval_intent_sha256,
        "candidate_evidence_schema_version": snap.candidate_evidence_schema_version,
        "candidate_evidence_sha256": snap.candidate_evidence_sha256,
        "eligibility_artifact_sha256": snap.eligibility_artifact_sha256,
        "reason_codes": (),
    }
    valid.update(kw)
    return _verif(**valid)


class _BadOutcome:
    pass


class _PropBoom:
    @property
    def outcome(self) -> object:
        raise RuntimeError("SECRET_LEAK_/home/user/APP_SECRET")


class _SecretSentinel:
    outcome = _VerifOutcome.VALID
    reason_codes = ()
    schema_version = 1
    approval_intent_schema_version = 1
    approval_intent_sha256 = _HEX64
    candidate_evidence_schema_version = 1
    candidate_evidence_sha256 = _HEX64
    eligibility_artifact_sha256 = _HEX64
    path = "/secret/config.toml"
    secret = "APP_SECRET"


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(None, id="none"),
        pytest.param(object(), id="object"),
        pytest.param({"outcome": "VALID"}, id="dict"),
        pytest.param(_BadOutcome(), id="wrong_outcome_object"),
        pytest.param(_PropBoom(), id="property_raises"),
        pytest.param(_SecretSentinel(), id="path_secret_sentinel"),
        pytest.param(_valid_verif(reason_codes=("x",)), id="valid_nonempty_reasons"),
        pytest.param(_valid_verif(schema_version=None), id="valid_null_schema"),
        pytest.param(_valid_verif(schema_version=True), id="valid_bool_schema"),
        pytest.param(_valid_verif(eligibility_artifact_sha256=None), id="valid_null_digest"),
        pytest.param(_valid_verif(eligibility_artifact_sha256="ZZ"), id="valid_bad_digest"),
        pytest.param(_verif(reason_codes=()), id="invalid_empty_reasons"),
        pytest.param(_verif(reason_codes=("a", "b")), id="invalid_two_reasons"),
    ],
)
def test_encode_malformed_verifier_result_is_invalid(
    result: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mod, "verify_operator_approval_consumption_eligibility_artifact_payload", lambda _p: result
    )
    res = encode(_valid_snapshot())
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID
    assert res.reason_codes == ("eligibility_persistence_payload_invalid_snapshot",)
    assert res.payload_bytes is None
    assert res.eligibility_artifact_sha256 is None
    blob = json.dumps([res.reason_codes, res.eligibility_artifact_sha256])
    assert "SECRET_LEAK" not in blob and "APP_SECRET" not in blob and "/home/" not in blob
    assert "/secret/" not in blob


def test_encode_verifier_subclass_result_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Sub(_Verif):
        pass

    sub = _Sub(**{f.name: getattr(_valid_verif(), f.name) for f in dataclasses.fields(_Verif)})
    monkeypatch.setattr(
        mod, "verify_operator_approval_consumption_eligibility_artifact_payload", lambda _p: sub
    )
    res = encode(_valid_snapshot())
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID
    assert res.payload_bytes is None


def test_encode_verifier_metadata_mismatch_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Verifier가 payload와 다른 valid-looking metadata를 반환하면 INVALID.
    wrong_digest = "0000000000000000000000000000000000000000000000000000000000000001"
    monkeypatch.setattr(
        mod,
        "verify_operator_approval_consumption_eligibility_artifact_payload",
        lambda _p: _valid_verif(eligibility_artifact_sha256=wrong_digest),
    )
    res = encode(_valid_snapshot())
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID
    assert res.payload_bytes is None


def test_encode_malformed_verifier_result_skips_canonical_dumps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _bad(_p: object) -> object:
        calls.append("verify")
        return None

    def _dumps(_v: object) -> str:
        calls.append("dumps")
        return "{}"

    monkeypatch.setattr(mod, "verify_operator_approval_consumption_eligibility_artifact_payload", _bad)
    monkeypatch.setattr(mod, "canonical_json_dumps", _dumps)
    encode(_valid_snapshot())
    assert calls == ["verify"]


# --- carry-over H1: expected schema constant matrix ---


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"schema_version": 2}, id="artifact_schema"),
        pytest.param({"approval_intent_schema_version": 2}, id="intent_schema"),
        pytest.param({"candidate_evidence_schema_version": 3}, id="evidence_schema"),
    ],
)
def test_verification_invariant_rejects_wrong_schema_constants(
    overrides: dict[str, int],
) -> None:
    snap = _valid_snapshot()
    result = _Verif(
        outcome=_VerifOutcome.VALID,
        schema_version=overrides.get("schema_version", snap.schema_version),
        approval_intent_schema_version=overrides.get(
            "approval_intent_schema_version", snap.approval_intent_schema_version
        ),
        approval_intent_sha256=snap.approval_intent_sha256,
        candidate_evidence_schema_version=overrides.get(
            "candidate_evidence_schema_version", snap.candidate_evidence_schema_version
        ),
        candidate_evidence_sha256=snap.candidate_evidence_sha256,
        eligibility_artifact_sha256=snap.eligibility_artifact_sha256,
        reason_codes=(),
    )
    assert validate_operator_approval_consumption_eligibility_artifact_verification_invariants(
        result
    ) is False


def test_verification_invariant_accepts_expected_schema_constants() -> None:
    snap = _valid_snapshot()
    result = _Verif(
        outcome=_VerifOutcome.VALID,
        schema_version=OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION,
        approval_intent_schema_version=OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
        approval_intent_sha256=snap.approval_intent_sha256,
        candidate_evidence_schema_version=ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
        candidate_evidence_sha256=snap.candidate_evidence_sha256,
        eligibility_artifact_sha256=snap.eligibility_artifact_sha256,
        reason_codes=(),
    )
    assert validate_operator_approval_consumption_eligibility_artifact_verification_invariants(
        result
    ) is True
    assert (
        OPERATOR_APPROVAL_CONSUMPTION_ELIGIBILITY_ARTIFACT_SCHEMA_VERSION == 1
        and OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION == 1
        and ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION == 2
    )


def test_encode_impossible_valid_wrong_schema_constants_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _valid_snapshot()
    impossible = _Verif(
        outcome=_VerifOutcome.VALID,
        schema_version=2,
        approval_intent_schema_version=2,
        approval_intent_sha256=snap.approval_intent_sha256,
        candidate_evidence_schema_version=3,
        candidate_evidence_sha256=snap.candidate_evidence_sha256,
        eligibility_artifact_sha256=snap.eligibility_artifact_sha256,
        reason_codes=(),
    )
    monkeypatch.setattr(
        mod, "verify_operator_approval_consumption_eligibility_artifact_payload", lambda _p: impossible
    )
    res = encode(_valid_snapshot())
    assert res.outcome is EligibilityArtifactPersistencePayloadOutcome.INVALID
    assert res.reason_codes == ("eligibility_persistence_payload_invalid_snapshot",)


# --- carry-over H2: alternate Unicode escape noncanonical ---


def test_decode_alternate_unicode_escape_noncanonical() -> None:
    canonical = _valid_canonical_bytes()
    parsed = json.loads(canonical)
    assert parsed["market"] == "KR"
    alternate = canonical.decode("utf-8").replace('"market":"KR"', '"market":"\\u004b\\u0052"').encode(
        "utf-8"
    )
    assert alternate != canonical
    assert json.loads(alternate) == parsed
    dec = decode(alternate)
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID
    assert dec.reason_codes == ("eligibility_persistence_payload_not_canonical",)
    assert dec.snapshot is None


def test_decode_symbol_unicode_escape_noncanonical() -> None:
    canonical = _valid_canonical_bytes()
    parsed = json.loads(canonical)
    symbol = parsed["symbol"]
    assert isinstance(symbol, str) and symbol.isascii()
    escaped_symbol = "".join(f"\\u{ord(ch):04x}" for ch in symbol)
    raw = canonical.decode("utf-8").replace(f'"symbol":"{symbol}"', f'"symbol":"{escaped_symbol}"')
    alternate = raw.encode("utf-8")
    assert alternate != canonical
    assert json.loads(alternate) == parsed
    dec = decode(alternate)
    assert dec.reason_codes == ("eligibility_persistence_payload_not_canonical",)


# --- decoder malformed snapshot-result matrix (P1-A) ---


def _snap_result(**kw: object) -> _SnapResult:
    base: dict[str, object] = {
        "outcome": _VerifOutcome.INVALID,
        "reason_codes": ("eligibility_artifact_invalid_field",),
        "snapshot": None,
    }
    base.update(kw)
    return _SnapResult(**base)  # type: ignore[arg-type]


def _valid_snap_result(**kw: object) -> _SnapResult:
    snap = _valid_snapshot()
    valid: dict[str, object] = {
        "outcome": _VerifOutcome.VALID,
        "reason_codes": (),
        "snapshot": snap,
    }
    valid.update(kw)
    return _snap_result(**valid)


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(None, id="none"),
        pytest.param(object(), id="object"),
        pytest.param({"outcome": "VALID"}, id="dict"),
        pytest.param(_BadOutcome(), id="wrong_outcome_object"),
        pytest.param(_PropBoom(), id="property_raises"),
        pytest.param(_valid_snap_result(reason_codes=("x",)), id="valid_nonempty_reasons"),
        pytest.param(_valid_snap_result(snapshot=None), id="valid_snapshot_none"),
        pytest.param(_valid_snap_result(snapshot=object()), id="valid_snapshot_wrong"),
        pytest.param(_snap_result(reason_codes=()), id="invalid_empty_reasons"),
        pytest.param(_snap_result(reason_codes=("a", "b")), id="invalid_two_reasons"),
        pytest.param(_snap_result(snapshot=_valid_snapshot()), id="invalid_snapshot_non_none"),
    ],
)
def test_decode_malformed_snapshot_result_is_invalid(
    result: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mod, "verify_and_snapshot_operator_approval_consumption_eligibility_artifact", lambda _p: result
    )
    dec = decode(encode(_valid_snapshot()).payload_bytes)
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID
    assert dec.reason_codes == ("eligibility_persistence_payload_invalid_artifact",)
    assert dec.snapshot is None
    blob = json.dumps(list(dec.reason_codes))
    assert "SECRET_LEAK" not in blob and "APP_SECRET" not in blob and "/home/" not in blob


def test_decode_snapshot_result_subclass_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Sub(_SnapResult):
        pass

    sub = _Sub(**{f.name: getattr(_valid_snap_result(), f.name) for f in dataclasses.fields(_SnapResult)})
    monkeypatch.setattr(
        mod, "verify_and_snapshot_operator_approval_consumption_eligibility_artifact", lambda _p: sub
    )
    dec = decode(encode(_valid_snapshot()).payload_bytes)
    assert dec.reason_codes == ("eligibility_persistence_payload_invalid_artifact",)


def test_decode_snapshot_subclass_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Sub(VerifiedOperatorApprovalConsumptionEligibilityArtifact):
        pass

    base = _valid_snapshot()
    sub = _Sub(**dataclasses.asdict(base))
    monkeypatch.setattr(
        mod,
        "verify_and_snapshot_operator_approval_consumption_eligibility_artifact",
        lambda _p: _valid_snap_result(snapshot=sub),
    )
    dec = decode(encode(_valid_snapshot()).payload_bytes)
    assert dec.reason_codes == ("eligibility_persistence_payload_invalid_artifact",)


def test_decode_malformed_snapshot_result_skips_canonical_dumps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_bytes = _valid_canonical_bytes()
    calls: list[str] = []

    def _bad(_p: object) -> object:
        calls.append("verify_and_snapshot")
        return None

    def _dumps(_v: object) -> str:
        calls.append("dumps")
        return "{}"

    monkeypatch.setattr(
        mod, "verify_and_snapshot_operator_approval_consumption_eligibility_artifact", _bad
    )
    monkeypatch.setattr(mod, "canonical_json_dumps", _dumps)
    decode(payload_bytes)
    assert calls == ["verify_and_snapshot"]


# --- canonical representation matrix (P1-B) ---


def _valid_canonical_bytes() -> bytes:
    return encode(_valid_snapshot()).payload_bytes


@pytest.mark.parametrize(
    "mutator,reason",
    [
        pytest.param(lambda b: json.dumps(json.loads(b), indent=2).encode("utf-8"), "pretty", id="pretty"),
        pytest.param(
            lambda b: json.dumps(
                {k: json.loads(b)[k] for k in reversed(list(json.loads(b)))},
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8"),
            "reordered",
            id="reordered",
        ),
        pytest.param(lambda b: b" " + b, "leading", id="leading_whitespace"),
        pytest.param(lambda b: b + b" ", "trailing_space", id="trailing_whitespace"),
        pytest.param(lambda b: b + b"\n", "trailing_newline", id="trailing_newline"),
    ],
)
def test_decode_noncanonical_semantic_valid_is_not_canonical(
    mutator: object, reason: str
) -> None:
    canonical = _valid_canonical_bytes()
    noncanonical = mutator(canonical)  # type: ignore[operator]
    dec = decode(noncanonical)
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.INVALID
    assert dec.reason_codes == ("eligibility_persistence_payload_not_canonical",)
    assert dec.snapshot is None


def test_decode_malformed_json_precedes_not_canonical() -> None:
    dec = decode(b"not json")
    assert dec.reason_codes == ("eligibility_persistence_payload_not_json",)


def test_decode_semantic_invalid_precedes_not_canonical() -> None:
    d = _valid_payload()
    d["market"] = "US"
    dec = decode(_canonical_bytes(d))
    assert dec.reason_codes == ("eligibility_artifact_invalid_field",)


def test_decode_hash_mismatch_precedes_not_canonical() -> None:
    d = _valid_payload()
    d["symbol"] = "000001" if d["symbol"] != "000001" else "000002"
    dec = decode(_canonical_bytes(d))
    assert dec.reason_codes == ("eligibility_artifact_hash_mismatch",)


# --- exception taxonomy (P2) ---


def test_decode_verify_and_snapshot_exception_is_invalid_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_p: object) -> object:
        raise ValueError("SECRET_LEAK_/home/user/APP_SECRET")

    monkeypatch.setattr(
        mod, "verify_and_snapshot_operator_approval_consumption_eligibility_artifact", _raise
    )
    dec = decode(encode(_valid_snapshot()).payload_bytes)
    assert dec.reason_codes == ("eligibility_persistence_payload_invalid_artifact",)
    assert "SECRET_LEAK" not in json.dumps(list(dec.reason_codes))


def test_decode_canonical_reencode_exception_is_invalid_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_bytes = _valid_canonical_bytes()

    def _raise(_v: object) -> str:
        raise ValueError("SECRET_LEAK_/home/user/APP_SECRET")

    monkeypatch.setattr(mod, "canonical_json_dumps", _raise)
    dec = decode(payload_bytes)
    assert dec.reason_codes == ("eligibility_persistence_payload_invalid_artifact",)


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_decode_verify_and_snapshot_fatal_propagates(
    monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
) -> None:
    def _raise(_p: object) -> object:
        raise exc()

    monkeypatch.setattr(
        mod, "verify_and_snapshot_operator_approval_consumption_eligibility_artifact", _raise
    )
    with pytest.raises(exc):
        decode(encode(_valid_snapshot()).payload_bytes)


def test_round_trip_digest_equality() -> None:
    snap = _valid_snapshot()
    enc = encode(snap)
    dec = decode(enc.payload_bytes)
    assert dec.snapshot is not None
    assert dec.snapshot.eligibility_artifact_sha256 == snap.eligibility_artifact_sha256
    assert enc.eligibility_artifact_sha256 == snap.eligibility_artifact_sha256


def test_round_trip_canonical_check_passes() -> None:
    enc = encode(_valid_snapshot())
    dec = decode(enc.payload_bytes)
    assert dec.outcome is EligibilityArtifactPersistencePayloadVerificationOutcome.VALID
    re_enc = encode(dec.snapshot)
    assert re_enc.payload_bytes == enc.payload_bytes
