"""RTM-7c.4r — immutable verified Operator approval-intent snapshot tests."""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.operator_approval_intent import (
    OPERATOR_APPROVAL_INTENT_FIELD_NAMES,
    OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION,
    OperatorApprovalIntentOutcome,
    build_operator_approval_intent,
)
from composition.operator_approval_intent_verifier import (
    OperatorApprovalIntentVerificationOutcome,
    VerifiedOperatorApprovalIntent,
    verify_and_snapshot_operator_approval_intent,
    verify_operator_approval_intent_payload,
)

import test_operator_approval_intent as intent_helper
import test_operator_approval_intent_verifier as verify_helper

_CLI_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_paper_fast_loop.py"
_spec = importlib.util.spec_from_file_location("run_paper_fast_loop", _CLI_PATH)
assert _spec and _spec.loader
_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cli)


def _valid_payload() -> dict[str, Any]:
    return verify_helper._valid_intent_payload()


def _snapshot(payload: object) -> VerifiedOperatorApprovalIntent:
    result = verify_and_snapshot_operator_approval_intent(payload)
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.VALID
    assert result.reason_codes == ()
    assert result.snapshot is not None
    return result.snapshot


# --- normal: builder → snapshot ---


def test_builder_intent_produces_valid_snapshot() -> None:
    built = intent_helper._build_intent()
    assert built.outcome is OperatorApprovalIntentOutcome.CREATED
    assert built.intent is not None
    result = verify_and_snapshot_operator_approval_intent(asdict(built.intent))
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.VALID
    assert result.snapshot is not None
    for field in OPERATOR_APPROVAL_INTENT_FIELD_NAMES:
        assert getattr(result.snapshot, field) == getattr(built.intent, field)


def test_real_seeded_intent_produces_valid_snapshot(tmp_path: Path) -> None:
    import test_activation_candidate_freshness_preflight as fr_helper
    from composition.activation_candidate_evidence import (
        FreshnessQualifiedEvidenceOutcome,
        freshness_qualify_and_build_candidate_evidence,
    )
    from composition.receipt_freshness_policy import ReceiptFreshnessPolicy

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
    built = build_operator_approval_intent(
        combined_result=combined,
        declared_at=fr_helper._NOW,
        operator_approval_declared=True,
        writers_stopped_manually_confirmed=True,
        live_orders_forbidden_confirmed=True,
    )
    assert built.intent is not None
    result = verify_and_snapshot_operator_approval_intent(asdict(built.intent))
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.VALID
    assert result.snapshot is not None
    for field in OPERATOR_APPROVAL_INTENT_FIELD_NAMES:
        assert getattr(result.snapshot, field) == getattr(built.intent, field)


def test_snapshot_has_exactly_thirteen_fields() -> None:
    snap = _snapshot(_valid_payload())
    assert len(dataclasses.fields(snap)) == 13
    assert {f.name for f in dataclasses.fields(snap)} == set(OPERATOR_APPROVAL_INTENT_FIELD_NAMES)


def test_snapshot_dataclass_is_frozen() -> None:
    snap = _snapshot(_valid_payload())
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.market = "US"  # type: ignore[misc]


# --- verifier ↔ snapshot parity ---


def _parity_cases() -> list[tuple[str, Callable[[dict[str, Any]], object], str]]:
    return [
        ("root_null", lambda _: None, "approval_intent_not_object"),
        ("root_list", lambda _: [], "approval_intent_not_object"),
        ("unknown_field", lambda p: {**p, "extra": True}, "approval_intent_unknown_field"),
        (
            "missing_field",
            lambda p: {k: v for k, v in p.items() if k != "schema_version"},
            "approval_intent_missing_field",
        ),
        ("schema_2", lambda p: {**p, "schema_version": 2}, "approval_intent_unsupported_schema"),
        (
            "naive_declared_at",
            lambda p: {**p, "declared_at": "2026-06-14T12:00:00"},
            "approval_intent_invalid_declared_at",
        ),
        (
            "evidence_schema_mismatch",
            lambda p: {**p, "evidence_schema_version": 1},
            "approval_intent_invalid_evidence_binding",
        ),
        ("market_us", lambda p: {**p, "market": "US"}, "approval_intent_invalid_field"),
        (
            "scope_mismatch",
            lambda p: {**p, "approval_scope": "other"},
            "approval_intent_invalid_scope",
        ),
        (
            "declaration_false",
            lambda p: {**p, "operator_approval_declared": False},
            "approval_intent_invalid_declaration",
        ),
        (
            "activation_true",
            lambda p: {**p, "activation_authorized": True},
            "approval_intent_invalid_activation_posture",
        ),
        (
            "hash_mismatch",
            lambda p: {**p, "approval_intent_sha256": "a" * 64},
            "approval_intent_hash_mismatch",
        ),
    ]


@pytest.mark.parametrize("case_id,mutator,expected_reason", [(c[0], c[1], c[2]) for c in _parity_cases()])
def test_verifier_snapshot_parity_matrix(
    case_id: str,
    mutator: Callable[[dict[str, Any]], object],
    expected_reason: str,
) -> None:
    del case_id
    base = _valid_payload()
    if expected_reason == "approval_intent_not_object" and mutator(base) is None:
        payload: object = None
    elif mutator(base) == []:
        payload = []
    else:
        payload = mutator(base)

    verify_result = verify_operator_approval_intent_payload(payload)
    snap_result = verify_and_snapshot_operator_approval_intent(payload)

    assert verify_result.outcome is OperatorApprovalIntentVerificationOutcome.INVALID
    assert snap_result.outcome is OperatorApprovalIntentVerificationOutcome.INVALID
    assert verify_result.reason_codes == (expected_reason,)
    assert snap_result.reason_codes == (expected_reason,)
    assert snap_result.snapshot is None
    if expected_reason == "approval_intent_hash_mismatch":
        assert verify_result.schema_version == OPERATOR_APPROVAL_INTENT_SCHEMA_VERSION
        assert verify_result.evidence_sha256 is not None


def test_valid_payload_parity() -> None:
    payload = _valid_payload()
    verify_result = verify_operator_approval_intent_payload(payload)
    snap_result = verify_and_snapshot_operator_approval_intent(payload)
    assert verify_result.outcome is snap_result.outcome
    assert verify_result.reason_codes == snap_result.reason_codes
    assert snap_result.snapshot is not None
    assert verify_result.approval_intent_sha256 == snap_result.snapshot.approval_intent_sha256


# --- call count ---


def test_snapshot_api_single_pass_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    import composition.operator_approval_intent_verifier as verifier_mod

    snap_calls: list[str] = []
    scalar_calls: list[str] = []
    hash_calls: list[str] = []
    public_verify_calls: list[str] = []

    real_snap = verifier_mod._snapshot_operator_approval_intent_payload
    real_scalars = verifier_mod.validate_operator_approval_intent_scalars
    real_hash_payload = verifier_mod.operator_approval_intent_hash_payload
    real_public = verifier_mod.verify_operator_approval_intent_payload

    def _spy_snap(payload: object) -> tuple[dict[str, object] | None, str | None]:
        snap_calls.append("snap")
        return real_snap(payload)

    def _spy_scalars(**kwargs: object) -> object:
        scalar_calls.append("scalars")
        return real_scalars(**kwargs)  # type: ignore[arg-type]

    def _spy_hash(**kwargs: object) -> dict[str, object]:
        hash_calls.append("hash")
        return real_hash_payload(**kwargs)  # type: ignore[arg-type]

    def _spy_public(payload: object) -> object:
        public_verify_calls.append("public")
        return real_public(payload)

    monkeypatch.setattr(verifier_mod, "_snapshot_operator_approval_intent_payload", _spy_snap)
    monkeypatch.setattr(verifier_mod, "validate_operator_approval_intent_scalars", _spy_scalars)
    monkeypatch.setattr(verifier_mod, "operator_approval_intent_hash_payload", _spy_hash)
    monkeypatch.setattr(verifier_mod, "verify_operator_approval_intent_payload", _spy_public)

    result = verify_and_snapshot_operator_approval_intent(_valid_payload())
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.VALID
    assert snap_calls == ["snap"]
    assert scalar_calls == ["scalars"]
    assert hash_calls == ["hash"]
    assert public_verify_calls == []


# --- mutation isolation ---


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: c.clear(), id="clear"),
        pytest.param(
            lambda c: (
                c.update({"schema_version": 99, "evidence_sha256": "f" * 64}),
            ),
            id="field_replacement",
        ),
        pytest.param(
            lambda c: c.__setitem__("approval_intent_sha256", "b" * 64),
            id="digest_replacement",
        ),
        pytest.param(lambda c: c.__setitem__("extra", True), id="unknown_field"),
    ],
)
def test_post_snapshot_caller_mutation_does_not_change_verdict(
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    import composition.operator_approval_intent_verifier as verifier_mod

    original = _valid_payload()
    caller = dict(original)
    real_snap = verifier_mod._snapshot_operator_approval_intent_payload

    def _spy_snap(payload: object) -> tuple[dict[str, object] | None, str | None]:
        detached, reason = real_snap(payload)
        if type(payload) is dict:
            mutate(payload)
        return detached, reason

    monkeypatch.setattr(verifier_mod, "_snapshot_operator_approval_intent_payload", _spy_snap)
    result = verify_and_snapshot_operator_approval_intent(caller)
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.VALID
    assert result.snapshot is not None
    assert result.snapshot.approval_intent_sha256 == original["approval_intent_sha256"]
    assert result.snapshot.schema_version == original["schema_version"]


def test_snapshot_result_has_no_mutable_references() -> None:
    payload = _valid_payload()
    result = verify_and_snapshot_operator_approval_intent(payload)
    assert result.snapshot is not None
    for field in dataclasses.fields(result.snapshot):
        value = getattr(result.snapshot, field.name)
        assert type(value) in (int, str, bool)
    assert not hasattr(result, "payload")
    assert not hasattr(result.snapshot, "payload")  # type: ignore[arg-type]


def test_caller_mutation_after_snapshot_does_not_change_returned_snapshot() -> None:
    payload = _valid_payload()
    result = verify_and_snapshot_operator_approval_intent(payload)
    assert result.snapshot is not None
    before = dataclasses.asdict(result.snapshot)
    payload.clear()
    payload["schema_version"] = 99
    after = dataclasses.asdict(result.snapshot)
    assert before == after


# --- exception contract (public API) ---


def test_runtime_error_in_detached_core_returns_stable_invalid(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import composition.operator_approval_intent_verifier as verifier_mod

    def _boom(_: dict[str, object]) -> tuple[object, object]:
        raise RuntimeError("POISON_VERIFY")

    monkeypatch.setattr(verifier_mod, "_verify_detached_operator_approval_intent", _boom)
    result = verify_and_snapshot_operator_approval_intent(_valid_payload())
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "POISON_VERIFY" not in combined
    assert "Traceback" not in combined
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.INVALID
    assert result.reason_codes == ("approval_intent_invalid_field",)
    assert result.snapshot is None


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_public_api_re_raises_non_exception_base(
    monkeypatch: pytest.MonkeyPatch,
    exc: type[BaseException],
) -> None:
    import composition.operator_approval_intent_verifier as verifier_mod

    def _boom(_: dict[str, object]) -> tuple[object, object]:
        raise exc()

    monkeypatch.setattr(verifier_mod, "_verify_detached_operator_approval_intent", _boom)
    with pytest.raises(exc):
        verify_and_snapshot_operator_approval_intent(_valid_payload())


def test_verifier_public_api_runtime_error_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import composition.operator_approval_intent_verifier as verifier_mod

    def _boom(_: dict[str, object]) -> tuple[object, object]:
        raise RuntimeError("POISON_VERIFY")

    monkeypatch.setattr(verifier_mod, "_verify_detached_operator_approval_intent", _boom)
    result = verify_operator_approval_intent_payload(_valid_payload())
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "POISON_VERIFY" not in combined
    assert result.outcome is OperatorApprovalIntentVerificationOutcome.INVALID
    assert result.reason_codes == ("approval_intent_invalid_field",)


# --- CLI MemoryError carry-over ---


def _run_verify_cli(payload: dict[str, Any] | None, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    if payload is not None:
        sys.stdin = type("_S", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()  # type: ignore[assignment]
    code = _cli.main(["--verify-operator-approval-intent", "--json"])
    captured = capsys.readouterr()
    body = json.loads(captured.out.strip().splitlines()[-1])
    return code, body


def test_cli_runtime_error_from_verifier_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _poison(_: object) -> object:
        raise RuntimeError("POISON_VERIFY")

    monkeypatch.setattr(_cli, "verify_operator_approval_intent_payload", _poison)
    code, body = _run_verify_cli(_valid_payload(), capsys)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert body["outcome"] == "INVALID"
    assert body["reason_codes"] == ["approval_intent_invalid_field"]
    assert "POISON_VERIFY" not in combined
    assert "Traceback" not in combined


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_cli_re_raises_non_exception_base(
    monkeypatch: pytest.MonkeyPatch,
    exc: type[BaseException],
) -> None:
    def _poison(_: object) -> object:
        raise exc()

    monkeypatch.setattr(_cli, "verify_operator_approval_intent_payload", _poison)
    sys.stdin = type("_S", (), {"buffer": io.BytesIO(json.dumps(_valid_payload()).encode())})()  # type: ignore[assignment]
    with pytest.raises(exc):
        _cli.main(["--verify-operator-approval-intent", "--json"])


# --- import / source guard ---


def test_module_exports() -> None:
    import composition.operator_approval_intent_verifier as mod

    assert "verify_and_snapshot_operator_approval_intent" in mod.__all__
    assert "VerifiedOperatorApprovalIntent" in mod.__all__
    assert "VerifiedOperatorApprovalIntentResult" in mod.__all__


def test_snapshot_source_does_not_call_public_verifier() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "operator_approval_intent_verifier.py"
    ).read_text(encoding="utf-8")
    snap_body = source.split("def verify_and_snapshot_operator_approval_intent", 1)[1]
    snap_body = snap_body.split("\ndef _verify_detached_operator_approval_intent", 1)[0]
    assert "verify_operator_approval_intent_payload(" not in snap_body
    assert "_verify_detached_operator_approval_intent" in source
