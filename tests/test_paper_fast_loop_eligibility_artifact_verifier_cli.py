"""RTM-7c.4v — Operator approval consumption eligibility-artifact verification CLI tests.

stdin-only, read-only. No config, no env, no clock, no DB, no filesystem write, no network,
no persistence, no consumption, no activation authorization. VALID means schema/semantic/hash
consistency only — never authenticity/provenance.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.operator_approval_consumption_eligibility import (
    OperatorApprovalConsumptionEligibilityOutcome,
    assess_operator_approval_consumption_eligibility,
)
from composition.operator_approval_consumption_eligibility_artifact import (
    build_operator_approval_consumption_eligibility_artifact,
    operator_approval_consumption_eligibility_artifact_hash_payload_from_scalars,
)
from composition.activation_candidate_evidence import ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION
from decision.canonical_json import payload_sha256

import test_operator_approval_consumption_eligibility as elig_helper

_CLI_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_paper_fast_loop.py"
_spec = importlib.util.spec_from_file_location("run_paper_fast_loop", _CLI_PATH)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

_MODE = "verify-approval-consumption-eligibility-artifact"
_FLAG = "--verify-approval-consumption-eligibility-artifact"

_ENVELOPE_KEYS = frozenset(
    {
        "outcome",
        "mode",
        "schema_version",
        "approval_intent_schema_version",
        "approval_intent_sha256",
        "candidate_evidence_schema_version",
        "candidate_evidence_sha256",
        "eligibility_artifact_sha256",
        "reason_codes",
        "activation_authorized",
        "runtime_activation_outcome",
        "artifact_authenticated",
        "artifact_persisted",
        "approval_consumed",
        "replay_prevented",
    }
)


def _valid_artifact_payload() -> dict[str, object]:
    payload, ev, now = elig_helper._eligible_inputs()
    result = assess_operator_approval_consumption_eligibility(
        intent_payload=payload, evidence=ev, now=now
    )
    assert result.outcome is OperatorApprovalConsumptionEligibilityOutcome.ELIGIBLE
    art = build_operator_approval_consumption_eligibility_artifact(result).artifact
    assert art is not None
    return dataclasses.asdict(art)


def _payload_with(**overrides: object) -> dict[str, object]:
    d = _valid_artifact_payload()
    d.update(overrides)
    return d


def _rehashed(**overrides: object) -> dict[str, object]:
    d = _valid_artifact_payload()
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


def _stdin_bytes(data: bytes) -> object:
    class _Stdin:
        buffer = io.BytesIO(data)

    return _Stdin()


def _run_cli(
    argv: list[str],
    stdin: bytes | dict[str, object] | None,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any]]:
    if stdin is not None:
        data = stdin if isinstance(stdin, bytes) else json.dumps(stdin).encode("utf-8")
        sys.stdin = _stdin_bytes(data)  # type: ignore[assignment]
    code = cli.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return code, payload


def _assert_constant_posture(payload: dict[str, Any]) -> None:
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"
    assert payload["artifact_authenticated"] is False
    assert payload["artifact_persisted"] is False
    assert payload["approval_consumed"] is False
    assert payload["replay_prevented"] is False


# --- VALID / verdict matrix ---


def test_builder_artifact_is_valid(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_cli([_FLAG, "--json"], _valid_artifact_payload(), capsys)
    assert code == 0
    assert payload["outcome"] == "VALID"
    assert payload["mode"] == _MODE
    assert payload["reason_codes"] == []
    assert frozenset(payload) == _ENVELOPE_KEYS
    _assert_constant_posture(payload)


def test_semantic_invalid_is_invalid(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_cli([_FLAG, "--json"], _payload_with(market="US"), capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["eligibility_artifact_invalid_field"]
    _assert_constant_posture(payload)


def test_stale_hash_is_hash_mismatch(capsys: pytest.CaptureFixture[str]) -> None:
    # Category B: semantic-valid content change, stale stored digest.
    code, payload = _run_cli([_FLAG, "--json"], _payload_with(symbol="000660"), capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["eligibility_artifact_hash_mismatch"]
    _assert_constant_posture(payload)


def test_semantic_valid_recomputed_hash_is_valid(capsys: pytest.CaptureFixture[str]) -> None:
    # Category C: semantic-valid content change with a correctly recomputed digest → VALID.
    code, payload = _run_cli([_FLAG, "--json"], _rehashed(symbol="000660"), capsys)
    assert code == 0
    assert payload["outcome"] == "VALID"
    assert payload["reason_codes"] == []
    _assert_constant_posture(payload)


def test_valid_never_claims_authenticity_or_consumption(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload = _run_cli([_FLAG, "--json"], _valid_artifact_payload(), capsys)
    assert code == 0
    assert payload["artifact_authenticated"] is False
    assert payload["approval_consumed"] is False
    assert payload["artifact_persisted"] is False
    assert payload["replay_prevented"] is False


# --- argument contract ---


def _assert_null_metadata(payload: dict[str, Any]) -> None:
    assert payload["schema_version"] is None
    assert payload["approval_intent_schema_version"] is None
    assert payload["approval_intent_sha256"] is None
    assert payload["candidate_evidence_schema_version"] is None
    assert payload["candidate_evidence_sha256"] is None
    assert payload["eligibility_artifact_sha256"] is None


def test_missing_json_is_fail(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_cli([_FLAG], None, capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["mode"] == _MODE
    assert payload["reason_codes"] == ["eligibility_artifact_verification_json_required"]
    assert frozenset(payload) == _ENVELOPE_KEYS
    _assert_null_metadata(payload)
    _assert_constant_posture(payload)


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(["--config", "config/config.toml.example"], id="config"),
        pytest.param(["--max-age-microseconds", "100"], id="max_age"),
        pytest.param(["--operator-approval-declared"], id="approval_declared"),
        pytest.param(["--writers-stopped-manually-confirmed"], id="writer_stop"),
        pytest.param(["--live-orders-forbidden-confirmed"], id="live_forbidden"),
    ],
)
def test_forbidden_arguments_are_not_applicable(
    extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run_cli([_FLAG, "--json", *extra], None, capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["mode"] == _MODE
    assert payload["reason_codes"] == [
        "eligibility_artifact_verification_argument_not_applicable"
    ]
    assert frozenset(payload) == _ENVELOPE_KEYS
    _assert_null_metadata(payload)
    _assert_constant_posture(payload)


@pytest.mark.parametrize(
    "conflicting_flag",
    [
        "--verify-operator-approval-intent",
        "--build-operator-approval-intent",
        "--validate-only",
        "--inspect-existing",
        "--precheck-runtime",
        "--verify-precheck-receipt",
        "--revalidate-activation-candidate",
        "--final-preflight-activation-candidate",
        "--freshness-preflight-activation-candidate",
        "--replay",
    ],
)
def test_mode_conflict_is_fail(
    conflicting_flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [_FLAG, conflicting_flag, "--json"]
    if conflicting_flag == "--replay":
        argv = [_FLAG, "--replay", "buy_fill", "--json"]
    code, payload = _run_cli(argv, None, capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["mode"] == _MODE
    assert payload["reason_codes"] == ["eligibility_artifact_verification_mode_conflict"]
    assert frozenset(payload) == _ENVELOPE_KEYS
    _assert_null_metadata(payload)
    _assert_constant_posture(payload)


def test_mode_conflict_run_takes_precedence_exit_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload = _run_cli(
        [_FLAG, "--verify-operator-approval-intent", "--run", "--json"], None, capsys
    )
    assert code == 2
    assert payload["outcome"] == "NO_GO"
    assert payload["reason_code"] == "live_run_not_implemented"


def test_mode_conflict_artifact_precedes_approval_intent_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # artifact flag + both approval-intent flags → artifact conflict envelope, not approval-intent.
    code, payload = _run_cli(
        [
            _FLAG,
            "--build-operator-approval-intent",
            "--verify-operator-approval-intent",
            "--json",
        ],
        None,
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["mode"] == _MODE
    assert payload["reason_codes"] == ["eligibility_artifact_verification_mode_conflict"]
    assert frozenset(payload) == _ENVELOPE_KEYS


def test_run_precedence_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_cli([_FLAG, "--run", "--json"], None, capsys)
    assert code == 2
    assert payload["outcome"] == "NO_GO"
    assert payload["reason_code"] == "live_run_not_implemented"


# --- stdin boundary ---


@pytest.mark.parametrize(
    ("stdin_data", "expected_reason"),
    [
        (b"", "eligibility_artifact_input_empty"),
        (b"\xff\xfe", "eligibility_artifact_input_not_utf8"),
        (b"not json", "eligibility_artifact_input_not_json"),
        (b'{"a": 1, "a": 2}', "eligibility_artifact_input_duplicate_key"),
        (b'{"a": {"b": 1, "b": 2}}', "eligibility_artifact_input_duplicate_key"),
        (b"NaN", "eligibility_artifact_input_not_json"),
        (b"Infinity", "eligibility_artifact_input_not_json"),
        (b"[" * 5000 + b"0" + b"]" * 5000, "eligibility_artifact_input_too_deep"),
    ],
    ids=[
        "empty",
        "invalid_utf8",
        "invalid_json",
        "duplicate_top",
        "duplicate_nested",
        "nan",
        "infinity",
        "too_deep",
    ],
)
def test_stdin_input_errors(
    stdin_data: bytes, expected_reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run_cli([_FLAG, "--json"], stdin_data, capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["mode"] == _MODE
    assert payload["reason_codes"] == [expected_reason]
    assert frozenset(payload) == _ENVELOPE_KEYS
    _assert_null_metadata(payload)
    _assert_constant_posture(payload)


def test_stdin_exact_1mib_not_too_large(capsys: pytest.CaptureFixture[str]) -> None:
    # Exactly the limit must NOT be rejected as too_large; it is parsed then the verifier rejects
    # it (missing fields) → INVALID, not a FAIL input-boundary outcome.
    data = b" " * (cli._VERIFY_RECEIPT_STDIN_LIMIT - 2) + b"{}"
    assert len(data) == cli._VERIFY_RECEIPT_STDIN_LIMIT
    code, payload = _run_cli([_FLAG, "--json"], data, capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["eligibility_artifact_missing_field"]


def test_stdin_over_1mib_is_too_large(capsys: pytest.CaptureFixture[str]) -> None:
    data = b"x" * (cli._VERIFY_RECEIPT_STDIN_LIMIT + 1)
    code, payload = _run_cli([_FLAG, "--json"], data, capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reason_codes"] == ["eligibility_artifact_input_too_large"]


def test_stdin_oserror_is_read_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _read_raises(_size: int) -> bytes:
        raise OSError("simulated stdin read failure")

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_read_raises)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    code = cli.main([_FLAG, "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reason_codes"] == ["eligibility_artifact_input_read_error"]


def test_stdin_reads_exactly_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reads: list[int] = []

    def _spy_read(size: int) -> bytes:
        reads.append(size)
        return json.dumps(_valid_artifact_payload()).encode("utf-8")

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_spy_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    code = cli.main([_FLAG, "--json"])
    assert code == 0
    assert reads == [cli._VERIFY_RECEIPT_STDIN_LIMIT + 1]


# --- isolation / single execution ---


def test_verifier_called_exactly_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    real = cli.verify_operator_approval_consumption_eligibility_artifact_payload

    def _spy(payload: object) -> object:
        calls.append("verify")
        return real(payload)

    monkeypatch.setattr(
        cli, "verify_operator_approval_consumption_eligibility_artifact_payload", _spy
    )
    code, _ = _run_cli([_FLAG, "--json"], _valid_artifact_payload(), capsys)
    assert code == 0
    assert calls == ["verify"]


def test_no_config_env_clock_db_fs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom_settings(*_a: object, **_k: object) -> object:
        raise AssertionError("load_settings must not be called in verify mode")

    def _boom_now(*_a: object, **_k: object) -> object:
        raise AssertionError("datetime.now must not be called in verify mode")

    monkeypatch.setattr(cli, "load_settings", _boom_settings)
    monkeypatch.setattr(cli, "datetime", type("_DT", (), {"now": staticmethod(_boom_now)}))
    code, payload = _run_cli([_FLAG, "--json"], _valid_artifact_payload(), capsys)
    assert code == 0
    assert payload["outcome"] == "VALID"


# --- exception contract ---


def test_verifier_exception_is_invalid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(_payload: object) -> object:
        raise ValueError("SECRET_LEAK_/home/user/APP_SECRET")

    monkeypatch.setattr(
        cli, "verify_operator_approval_consumption_eligibility_artifact_payload", _raise
    )
    code, payload = _run_cli([_FLAG, "--json"], _valid_artifact_payload(), capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["eligibility_artifact_invalid_field"]
    blob = json.dumps(payload)
    assert "SECRET_LEAK" not in blob
    assert "APP_SECRET" not in blob
    assert "/home/" not in blob
    assert "Traceback" not in blob


@pytest.mark.parametrize("exc", [MemoryError, KeyboardInterrupt, SystemExit])
def test_fatal_exceptions_reraise(
    monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
) -> None:
    def _raise(_payload: object) -> object:
        raise exc()

    monkeypatch.setattr(
        cli, "verify_operator_approval_consumption_eligibility_artifact_payload", _raise
    )
    sys.stdin = _stdin_bytes(json.dumps(_valid_artifact_payload()).encode("utf-8"))  # type: ignore[assignment]
    with pytest.raises(exc):
        cli.main([_FLAG, "--json"])


# --- sanitization ---


def test_invalid_digest_original_not_echoed(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_cli(
        [_FLAG, "--json"], _payload_with(eligibility_artifact_sha256="ZZZ_not_hex"), capsys
    )
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert "ZZZ_not_hex" not in json.dumps(payload)


# --- carry-over H1: malformed verifier result fails closed ---

from composition.operator_approval_consumption_eligibility_artifact_verifier import (  # noqa: E402
    OperatorApprovalConsumptionEligibilityArtifactVerification as _Verif,
    OperatorApprovalConsumptionEligibilityArtifactVerificationOutcome as _Outcome,
)

_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _verif(**kw: Any) -> _Verif:
    base: dict[str, Any] = {
        "outcome": _Outcome.INVALID,
        "schema_version": None,
        "approval_intent_schema_version": None,
        "approval_intent_sha256": None,
        "candidate_evidence_schema_version": None,
        "candidate_evidence_sha256": None,
        "eligibility_artifact_sha256": None,
        "reason_codes": ("eligibility_artifact_invalid_field",),
    }
    base.update(kw)
    return _Verif(**base)


def _valid_verif(**kw: Any) -> _Verif:
    valid: dict[str, Any] = {
        "outcome": _Outcome.VALID,
        "schema_version": 1,
        "approval_intent_schema_version": 1,
        "approval_intent_sha256": _HEX,
        "candidate_evidence_schema_version": ACTIVATION_CANDIDATE_EVIDENCE_SCHEMA_VERSION,
        "candidate_evidence_sha256": _HEX,
        "eligibility_artifact_sha256": _HEX,
        "reason_codes": (),
    }
    valid.update(kw)
    return _verif(**valid)


class _BadOutcome:
    pass


class _PropBoom:
    @property
    def outcome(self) -> Any:
        raise RuntimeError("SECRET_LEAK_/home/user/APP_SECRET")


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(None, id="none"),
        pytest.param(object(), id="object"),
        pytest.param({"outcome": "VALID"}, id="dict"),
        pytest.param(_BadOutcome(), id="wrong_outcome_object"),
        pytest.param(_PropBoom(), id="property_raises"),
        pytest.param(_valid_verif(reason_codes=("x",)), id="valid_nonempty_reasons"),
        pytest.param(_verif(reason_codes=()), id="invalid_empty_reasons"),
        pytest.param(_verif(reason_codes=("a", "b")), id="invalid_two_reasons"),
        pytest.param(_valid_verif(schema_version=None), id="valid_null_schema"),
        pytest.param(_valid_verif(eligibility_artifact_sha256=None), id="valid_null_digest"),
        pytest.param(_valid_verif(eligibility_artifact_sha256="ZZ"), id="valid_bad_digest"),
        pytest.param(_valid_verif(schema_version=True), id="valid_bool_schema"),
    ],
)
def test_malformed_verifier_result_fails_closed(
    result: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "verify_operator_approval_consumption_eligibility_artifact_payload",
        lambda _p: result,
    )
    code, payload = _run_cli([_FLAG, "--json"], _valid_artifact_payload(), capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["eligibility_artifact_invalid_field"]
    assert frozenset(payload) == _ENVELOPE_KEYS
    _assert_null_metadata(payload)
    _assert_constant_posture(payload)
    blob = json.dumps(payload)
    assert "SECRET_LEAK" not in blob and "APP_SECRET" not in blob and "/home/" not in blob


def test_subclass_verifier_result_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Sub(_Verif):
        pass

    sub = _Sub(**{f.name: getattr(_valid_verif(), f.name) for f in dataclasses.fields(_Verif)})
    monkeypatch.setattr(
        cli, "verify_operator_approval_consumption_eligibility_artifact_payload",
        lambda _p: sub,
    )
    code, payload = _run_cli([_FLAG, "--json"], _valid_artifact_payload(), capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["eligibility_artifact_invalid_field"]
    _assert_null_metadata(payload)


def test_wellformed_valid_verifier_result_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "verify_operator_approval_consumption_eligibility_artifact_payload",
        lambda _p: _valid_verif(),
    )
    code, payload = _run_cli([_FLAG, "--json"], _valid_artifact_payload(), capsys)
    assert code == 0
    assert payload["outcome"] == "VALID"
    assert payload["eligibility_artifact_sha256"] == _HEX


# --- carry-over H2: early-failure call counts ---


def _stdin_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []

    def _read(_size: int) -> bytes:
        reads.append("stdin")
        return b"{}"

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    return reads


def _verifier_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = cli.verify_operator_approval_consumption_eligibility_artifact_payload

    def _spy(payload: object) -> object:
        calls.append("verify")
        return real(payload)

    monkeypatch.setattr(
        cli, "verify_operator_approval_consumption_eligibility_artifact_payload", _spy
    )
    return calls


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([_FLAG, "--verify-operator-approval-intent", "--json"], id="mode_conflict"),
        pytest.param([_FLAG], id="missing_json"),
        pytest.param([_FLAG, "--json", "--config", "x"], id="forbidden_argument"),
    ],
)
def test_early_failure_no_stdin_no_verifier(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reads = _stdin_spy(monkeypatch)
    calls = _verifier_spy(monkeypatch)
    code = cli.main(argv)
    capsys.readouterr()
    assert code == 1
    assert reads == []
    assert calls == []


def test_stdin_boundary_failure_no_verifier(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _verifier_spy(monkeypatch)
    # Empty stdin → input-boundary FAIL after exactly one read, verifier never called.
    code, payload = _run_cli([_FLAG, "--json"], b"", capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert calls == []


def test_artifact_invalid_calls_verifier_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _verifier_spy(monkeypatch)
    code, payload = _run_cli([_FLAG, "--json"], _payload_with(market="US"), capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert calls == ["verify"]
