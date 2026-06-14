"""RTM-7c.4p — Operator approval-intent CLI input tests.

No network, no credentials, no intent persistence, no activation authorization.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config.settings as _settings_mod
from composition.operator_approval_intent import OperatorApprovalIntentOutcome

import test_activation_candidate_final_preflight as fp_helper
import test_activation_candidate_freshness_preflight as fr_helper
import test_precheck_receipt_verifier as vrf_helper

_CLI_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_paper_fast_loop.py"
_spec = importlib.util.spec_from_file_location("run_paper_fast_loop", _CLI_PATH)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

_KST = cli._KST
_NOW = fp_helper._NOW
_SYMBOL = fp_helper._SYMBOL
_HEX64_RE = re.compile(r"[0-9a-f]{64}")

_CONFIRM_FLAGS = [
    "--operator-approval-declared",
    "--writers-stopped-manually-confirmed",
    "--live-orders-forbidden-confirmed",
]

_ENVELOPE_KEYS = frozenset(
    {
        "outcome",
        "mode",
        "reasons",
        "candidate_evidence_schema_version",
        "candidate_evidence_sha256",
        "approval_intent_schema_version",
        "approval_intent_sha256",
        "declared_at",
        "activation_authorized",
        "runtime_activation_outcome",
        "approval_intent_authenticated",
        "approval_intent_consumed",
        "approval_intent_persisted",
    }
)


def _stdin_bytes(data: bytes) -> object:
    class _Stdin:
        buffer = io.BytesIO(data)

    return _Stdin()


def _write_config(tmp_path: Path, *, enabled: bool = True, symbol: str = _SYMBOL) -> Path:
    return fr_helper._write_config(tmp_path, enabled=enabled, symbol=symbol)


def _approval_argv(
    config_path: Path,
    *,
    max_age: str | None = "100",
    include_confirmations: bool = True,
) -> list[str]:
    argv = [
        "--config",
        str(config_path),
        "--build-operator-approval-intent",
        "--json",
    ]
    if max_age is not None:
        argv.extend(["--max-age-microseconds", max_age])
    if include_confirmations:
        argv.extend(_CONFIRM_FLAGS)
    return argv


def _run_approval_cli(
    argv: list[str], receipt: dict[str, Any] | None, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    if receipt is not None:
        sys.stdin = _stdin_bytes(json.dumps(receipt).encode("utf-8"))  # type: ignore[assignment]
    code = cli.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return code, payload


def _assert_null_intent_digest(payload: dict[str, Any]) -> None:
    assert payload["approval_intent_schema_version"] is None
    assert payload["approval_intent_sha256"] is None
    assert payload["declared_at"] is None


def _assert_constant_posture(payload: dict[str, Any]) -> None:
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"
    assert payload["approval_intent_authenticated"] is False
    assert payload["approval_intent_consumed"] is False
    assert payload["approval_intent_persisted"] is False


def _assert_stable_envelope(payload: dict[str, Any]) -> None:
    assert payload["mode"] == "build-operator-approval-intent"
    assert isinstance(payload["reasons"], list)
    assert "reason_code" not in payload
    for key in _ENVELOPE_KEYS:
        assert key in payload
    _assert_constant_posture(payload)


def test_approval_cli_pass_with_all_required_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._FixedClock)
    config_path = _write_config(tmp_path)
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)
    code, payload = _run_approval_cli(_approval_argv(config_path), receipt, capsys)
    pinned_now = fr_helper._FixedClock.now(_KST).isoformat()
    assert code == 0
    assert payload["outcome"] == "PASS"
    assert payload["reasons"] == []
    assert payload["candidate_evidence_schema_version"] == 2
    assert _HEX64_RE.fullmatch(payload["candidate_evidence_sha256"])
    assert payload["approval_intent_schema_version"] == 1
    assert _HEX64_RE.fullmatch(payload["approval_intent_sha256"])
    assert payload["approval_scope"] == "attended_paper_fast_loop_candidate"
    assert payload["declared_at"] == pinned_now
    assert payload["operator_approval_declared"] is True
    assert payload["writers_stopped_manually_confirmed"] is True
    assert payload["live_orders_forbidden_confirmed"] is True
    _assert_stable_envelope(payload)
    _assert_constant_posture(payload)


def test_approval_cli_missing_json_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path)
    argv = [
        "--config",
        str(config_path),
        "--build-operator-approval-intent",
        "--max-age-microseconds",
        "100",
        *_CONFIRM_FLAGS,
    ]
    code = cli.main(argv)
    captured = capsys.readouterr()
    assert "paper fast-loop:" not in captured.out
    payload = json.loads(captured.out.strip())
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reasons"] == ["approval_intent_json_required"]
    _assert_stable_envelope(payload)
    _assert_null_intent_digest(payload)


def test_approval_cli_missing_json_early_isolation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin_reads: list[int] = []

    def _spy_read(size: int = -1) -> bytes:
        stdin_reads.append(size)
        return b""

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_spy_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    monkeypatch.setattr(cli, "load_settings", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("config must not load when json missing")
    ))
    monkeypatch.setattr(
        cli,
        "datetime",
        type("_D", (), {"now": staticmethod(lambda tz=None: (_ for _ in ()).throw(
            AssertionError("clock must not read when json missing")
        ))})(),
    )

    argv = [
        "--config",
        "any.toml",
        "--build-operator-approval-intent",
        "--max-age-microseconds",
        "100",
        *_CONFIRM_FLAGS,
    ]
    code, payload = _run_approval_cli(argv, None, capsys)
    assert code == 1
    assert payload["reasons"] == ["approval_intent_json_required"]
    assert stdin_reads == []


def test_approval_cli_missing_config_fail(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "--build-operator-approval-intent",
        "--json",
        "--max-age-microseconds",
        "100",
        *_CONFIRM_FLAGS,
    ]
    code, payload = _run_approval_cli(argv, None, capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reasons"] == ["approval_intent_config_required"]
    _assert_stable_envelope(payload)
    _assert_null_intent_digest(payload)


def test_approval_cli_missing_json_and_config_precedence(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "--build-operator-approval-intent",
        "--max-age-microseconds",
        "100",
        *_CONFIRM_FLAGS,
    ]
    code, payload = _run_approval_cli(argv, None, capsys)
    assert code == 1
    assert payload["reasons"] == ["approval_intent_json_required"]


def test_approval_cli_explicit_default_config_path_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._FixedClock)
    config_path = _write_config(tmp_path)
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)
    argv = [
        "--config",
        str(config_path),
        "--build-operator-approval-intent",
        "--json",
        "--max-age-microseconds",
        "100",
        *_CONFIRM_FLAGS,
    ]
    code, payload = _run_approval_cli(argv, receipt, capsys)
    assert code == 0
    assert payload["outcome"] == "PASS"


def test_other_modes_without_config_keep_default(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_approval_cli(["--json"], None, capsys)
    assert code == 1
    assert payload["mode"] == "validate-only"
    assert payload["config"] == cli.DEFAULT_CONFIG_PATH


@pytest.mark.parametrize(
    "missing_flag,expected_reason",
    [
        ("--operator-approval-declared", "approval_intent_operator_declaration_missing"),
        (
            "--writers-stopped-manually-confirmed",
            "approval_intent_writer_stop_confirmation_missing",
        ),
        (
            "--live-orders-forbidden-confirmed",
            "approval_intent_live_order_prohibition_confirmation_missing",
        ),
    ],
)
def test_approval_cli_missing_confirmation_fail(
    missing_flag: str,
    expected_reason: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = [
        "--config",
        "unused.toml",
        "--build-operator-approval-intent",
        "--max-age-microseconds",
        "100",
        "--json",
    ]
    for flag in _CONFIRM_FLAGS:
        if flag != missing_flag:
            argv.append(flag)
    code, payload = _run_approval_cli(argv, None, capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reasons"] == [expected_reason]
    _assert_stable_envelope(payload)
    _assert_null_intent_digest(payload)
    _assert_constant_posture(payload)


def test_approval_cli_missing_max_age_fail(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "--config",
        "unused.toml",
        "--build-operator-approval-intent",
        "--json",
        *_CONFIRM_FLAGS,
    ]
    code, payload = _run_approval_cli(argv, None, capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reasons"] == ["freshness_policy_input_missing"]
    _assert_stable_envelope(payload)
    _assert_null_intent_digest(payload)


def test_approval_cli_confirmation_not_applicable_on_validate_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload = _run_approval_cli(
        ["--validate-only", "--operator-approval-declared", "--json"], None, capsys
    )
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reasons"] == ["approval_intent_argument_not_applicable"]
    assert "reason_code" not in payload


def test_approval_cli_run_with_flags_still_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_approval_cli(
        ["--run", "--max-age-microseconds", "100", "--json", *_CONFIRM_FLAGS],
        None,
        capsys,
    )
    assert code == 2
    assert payload["reason_code"] == "live_run_not_implemented"


def test_approval_cli_mutually_exclusive_modes(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_approval_cli(
        [
            "--build-operator-approval-intent",
            "--freshness-preflight-activation-candidate",
            "--max-age-microseconds",
            "100",
            "--json",
            *_CONFIRM_FLAGS,
        ],
        vrf_helper._valid_receipt(),
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["mode"] == "build-operator-approval-intent"
    assert payload["reasons"] == ["approval_intent_mode_conflict"]


@pytest.mark.parametrize(
    "bad_token",
    ["-1", "1\n", "abc"],
    ids=["negative", "trailing_lf", "alpha"],
)
def test_approval_cli_invalid_max_age_early_isolation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_token: str,
) -> None:
    stdin_reads: list[int] = []

    def _spy_read(size: int = -1) -> bytes:
        stdin_reads.append(size)
        return b""

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_spy_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())

    def _spy_load(*_a: object, **_k: object) -> object:
        raise AssertionError("config must not load on invalid max-age")

    monkeypatch.setattr(cli, "load_settings", _spy_load)

    class _RaisingDatetime:
        @staticmethod
        def now(tz: object = None) -> datetime:
            raise AssertionError("clock must not read on invalid max-age")

    monkeypatch.setattr(cli, "datetime", _RaisingDatetime)

    argv = [
        "--config",
        "unused.toml",
        "--build-operator-approval-intent",
        "--max-age-microseconds",
        bad_token,
        "--json",
        *_CONFIRM_FLAGS,
    ]
    code, payload = _run_approval_cli(argv, None, capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reasons"] == ["freshness_policy_input_invalid"]
    assert stdin_reads == []


@pytest.mark.parametrize(
    "missing_flag,expected_reason",
    [
        ("--operator-approval-declared", "approval_intent_operator_declaration_missing"),
        (
            "--writers-stopped-manually-confirmed",
            "approval_intent_writer_stop_confirmation_missing",
        ),
        (
            "--live-orders-forbidden-confirmed",
            "approval_intent_live_order_prohibition_confirmation_missing",
        ),
    ],
)
def test_approval_cli_missing_confirmation_early_isolation(
    missing_flag: str,
    expected_reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_reads: list[int] = []

    def _spy_read(size: int = -1) -> bytes:
        stdin_reads.append(size)
        return b""

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_spy_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    monkeypatch.setattr(cli, "load_settings", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("config must not load on missing confirmation")
    ))
    monkeypatch.setattr(
        cli,
        "datetime",
        type("_D", (), {"now": staticmethod(lambda tz=None: (_ for _ in ()).throw(
            AssertionError("clock must not read on missing confirmation")
        ))})(),
    )

    argv = [
        "--config",
        "unused.toml",
        "--build-operator-approval-intent",
        "--max-age-microseconds",
        "100",
        "--json",
    ]
    for flag in _CONFIRM_FLAGS:
        if flag != missing_flag:
            argv.append(flag)

    code, payload = _run_approval_cli(argv, None, capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reasons"] == [expected_reason]
    assert stdin_reads == []


def test_approval_cli_stale_receipt_no_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._AgedClock(101))
    config_path = _write_config(tmp_path)
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)
    code, payload = _run_approval_cli(_approval_argv(config_path), receipt, capsys)
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["candidate_receipt_stale"]
    _assert_stable_envelope(payload)
    _assert_null_intent_digest(payload)
    _assert_constant_posture(payload)


def test_approval_cli_future_receipt_no_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._PastClock)
    config_path = _write_config(tmp_path)
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)
    code, payload = _run_approval_cli(_approval_argv(config_path, max_age="999999999999"), receipt, capsys)
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["candidate_receipt_time_in_future"]
    _assert_null_intent_digest(payload)


def test_approval_cli_symbol_mismatch_no_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._FixedClock)
    config_path = _write_config(tmp_path)
    receipt = vrf_helper._valid_receipt(symbol="000660")
    code, payload = _run_approval_cli(_approval_argv(config_path), receipt, capsys)
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["candidate_symbol_mismatch"]
    _assert_null_intent_digest(payload)


def test_approval_cli_expired_snapshot_no_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._FutureClock)
    config_path = _write_config(tmp_path)
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)
    code, payload = _run_approval_cli(_approval_argv(config_path, max_age="999999999999"), receipt, capsys)
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert any(r.startswith("candidate_current_precheck:") for r in payload["reasons"])
    _assert_null_intent_digest(payload)


def test_approval_cli_intent_builder_invalid_no_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import composition.operator_approval_intent as intent_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._FixedClock)
    config_path = _write_config(tmp_path)
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)

    def _invalid_builder(**_kwargs: object) -> Any:
        return intent_mod.OperatorApprovalIntentResult(
            outcome=OperatorApprovalIntentOutcome.INVALID,
            reasons=("approval_intent_invalid_input",),
            intent=None,
        )

    monkeypatch.setattr(cli, "build_operator_approval_intent", _invalid_builder)

    code, payload = _run_approval_cli(_approval_argv(config_path), receipt, capsys)
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["approval_intent_generation_invalid"]
    _assert_stable_envelope(payload)
    _assert_null_intent_digest(payload)
    _assert_constant_posture(payload)


def test_approval_cli_intent_builder_exception_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import composition.operator_approval_intent as intent_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._FixedClock)
    config_path = _write_config(tmp_path)
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)

    def _raising_builder(**_kwargs: object) -> Any:
        raise RuntimeError("POISON_INTENT_BUILDER")

    monkeypatch.setattr(cli, "build_operator_approval_intent", _raising_builder)

    code, payload = _run_approval_cli(_approval_argv(config_path), receipt, capsys)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["approval_intent_generation_invalid"]
    assert "POISON_INTENT_BUILDER" not in combined
    assert "Traceback" not in combined


def test_approval_cli_semantic_invalid_evidence_no_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import composition.activation_candidate_evidence as evidence_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._FixedClock)
    config_path = _write_config(tmp_path)
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)

    def _fake_builder(**kwargs: object) -> Any:
        return evidence_mod.ActivationCandidateEvidenceResult(
            outcome=evidence_mod.ActivationCandidateEvidenceOutcome.INVALID,
            reasons=("candidate_evidence_invalid_input",),
            evidence=None,
        )

    monkeypatch.setattr(evidence_mod, "build_activation_candidate_evidence", _fake_builder)

    code, payload = _run_approval_cli(_approval_argv(config_path), receipt, capsys)
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["candidate_evidence_generation_invalid"]
    _assert_null_intent_digest(payload)


def test_approval_cli_single_execution_call_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._FixedClock)
    config_path = _write_config(tmp_path)
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)

    stdin_reads: list[int] = []

    def _spy_read(size: int = -1) -> bytes:
        stdin_reads.append(size)
        return json.dumps(receipt).encode("utf-8")

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_spy_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())

    load_calls: list[str] = []
    real_load = cli.load_settings

    def _spy_load(*args: object, **kwargs: object) -> object:
        load_calls.append("load")
        return real_load(*args, **kwargs)

    monkeypatch.setattr(cli, "load_settings", _spy_load)

    clock_calls: list[str] = []
    real_datetime = cli.datetime

    class _CountingDatetime:
        @staticmethod
        def now(tz: object = None) -> datetime:
            clock_calls.append("now")
            return real_datetime.now(tz)

    monkeypatch.setattr(cli, "datetime", _CountingDatetime)

    pipeline_calls: list[str] = []
    real_pipeline = cli.freshness_qualify_and_build_candidate_evidence

    def _spy_pipeline(*args: object, **kwargs: object) -> object:
        pipeline_calls.append("pipeline")
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(cli, "freshness_qualify_and_build_candidate_evidence", _spy_pipeline)

    intent_calls: list[str] = []
    real_intent = cli.build_operator_approval_intent

    def _spy_intent(*args: object, **kwargs: object) -> object:
        intent_calls.append("intent")
        return real_intent(*args, **kwargs)

    monkeypatch.setattr(cli, "build_operator_approval_intent", _spy_intent)

    code, payload = _run_approval_cli(_approval_argv(config_path), None, capsys)
    assert code == 0
    assert payload["outcome"] == "PASS"
    _assert_stable_envelope(payload)
    assert len(stdin_reads) == 1
    assert load_calls == ["load"]
    assert clock_calls == ["now"]
    assert pipeline_calls == ["pipeline"]
    assert intent_calls == ["intent"]


def test_approval_cli_no_go_skips_intent_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._AgedClock(101))
    config_path = _write_config(tmp_path)
    settings = fr_helper._settings(tmp_path)
    receipt = fr_helper._pass_receipt(tmp_path, settings)

    intent_calls: list[str] = []

    def _spy_intent(*_a: object, **_k: object) -> object:
        intent_calls.append("intent")
        raise AssertionError("intent builder must not run on upstream NO_GO")

    monkeypatch.setattr(cli, "build_operator_approval_intent", _spy_intent)

    code, payload = _run_approval_cli(_approval_argv(config_path), receipt, capsys)
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert intent_calls == []


def test_approval_cli_sanitizes_poison_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", fr_helper._FixedClock)
    poison_path = tmp_path / "poison_config.toml"
    poison_path.write_text(
        """
[runtime.paper_fast_loop]
enabled = true
market = "KR"
symbol = "005930"
""",
        encoding="utf-8",
    )
    receipt = vrf_helper._valid_receipt()
    receipt["reasons"] = ["/home/user/KIS_LIVE_APP_KEY/secret"]
    argv = _approval_argv(poison_path.resolve())
    code, payload = _run_approval_cli(argv, receipt, capsys)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert "KIS_" not in combined
    assert "/home/" not in combined
    assert str(poison_path) not in combined
    assert "Traceback" not in combined
    assert json.dumps(receipt) not in combined
    assert payload["outcome"] in {"NO_GO", "FAIL"}


def test_approval_cli_max_age_not_applicable_on_final_preflight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload = _run_approval_cli(
        [
            "--final-preflight-activation-candidate",
            "--max-age-microseconds",
            "100",
            "--json",
        ],
        None,
        capsys,
    )
    assert code == 1
    assert payload["reason_code"] == "freshness_policy_argument_not_applicable"


def _approval_base_argv(config_path: Path | str) -> list[str]:
    return [
        "--config",
        str(config_path),
        "--build-operator-approval-intent",
        "--json",
        "--max-age-microseconds",
        "100",
        *_CONFIRM_FLAGS,
    ]


@pytest.mark.parametrize(
    ("stdin_data", "expected_reason"),
    [
        (None, "receipt_input_empty"),
        (b"\xff\xfe", "receipt_input_not_utf8"),
        (b"{not json", "receipt_input_not_json"),
        (b'{"schema_version": 1, "schema_version": 2}', "receipt_input_duplicate_key"),
        (b"[" * 5000 + b"0" + b"]" * 5000, "receipt_input_too_deep"),
        (b"x" * (cli._VERIFY_RECEIPT_STDIN_LIMIT + 1), "receipt_input_too_large"),
    ],
    ids=["empty", "invalid_utf8", "invalid_json", "duplicate_key", "too_deep", "too_large"],
)
def test_approval_cli_stdin_errors_are_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stdin_data: bytes | None,
    expected_reason: str,
) -> None:
    config_path = _write_config(tmp_path)
    if stdin_data is not None:
        sys.stdin = _stdin_bytes(stdin_data)  # type: ignore[assignment]
    code = cli.main(_approval_base_argv(config_path))
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reasons"] == [expected_reason]
    _assert_stable_envelope(payload)
    _assert_null_intent_digest(payload)


def test_approval_cli_stdin_read_oserror_is_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path)

    def _read_raises(_size: int = -1) -> bytes:
        raise OSError("simulated stdin read failure")

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_read_raises)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    code = cli.main(_approval_base_argv(config_path))
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    payload = json.loads(captured.out.strip())
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reasons"] == ["receipt_input_read_error"]
    assert "OSError" not in combined
    assert "simulated" not in combined
    assert "Traceback" not in combined


@pytest.mark.parametrize(
    "config_factory",
    [
        "missing_file",
        "malformed_toml",
        "invalid_settings",
        "env_placeholder",
        "live_gate",
    ],
)
def test_approval_cli_config_errors_are_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config_factory: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_settings_mod, "os", fr_helper._OsShim())
    receipt = vrf_helper._valid_receipt()

    if config_factory == "missing_file":
        config_path = tmp_path / "does_not_exist.toml"
    elif config_factory == "malformed_toml":
        config_path = tmp_path / "bad.toml"
        config_path.write_text("[runtime.paper_fast_loop\nenabled = true\n", encoding="utf-8")
    elif config_factory == "invalid_settings":
        config_path = tmp_path / "invalid.toml"
        config_path.write_text(
            """
[runtime.paper_fast_loop]
enabled = true
market = "US"
symbol = "005930"
""",
            encoding="utf-8",
        )
    elif config_factory == "env_placeholder":
        config_path = tmp_path / "env.toml"
        config_path.write_text(
            """
[runtime.paper_fast_loop]
enabled = true
market = "KR"
symbol = "005930"
ledger_path = "${LEDGER_PATH}/ledger.sqlite3"
""",
            encoding="utf-8",
        )
    else:
        config_path = tmp_path / "live.toml"
        config_path.write_text(
            """
[trading]
mode = "live"
allow_live_trading = false

[broker]
adapter = "kis_live"

[runtime.paper_fast_loop]
enabled = true
market = "KR"
symbol = "005930"
""",
            encoding="utf-8",
        )

    code, payload = _run_approval_cli(_approval_base_argv(config_path), receipt, capsys)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reasons"] == ["approval_intent_config_invalid"]
    _assert_stable_envelope(payload)
    _assert_null_intent_digest(payload)
    assert str(config_path) not in combined
    assert "SettingsError" not in combined
    assert "RuntimeGateError" not in combined
    assert "ConfigEnvironmentError" not in combined
    assert "Traceback" not in combined


def test_approval_cli_missing_config_early_isolation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin_reads: list[int] = []

    def _spy_read(size: int = -1) -> bytes:
        stdin_reads.append(size)
        return b""

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_spy_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    monkeypatch.setattr(cli, "load_settings", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("config must not load when config missing")
    ))

    argv = [
        "--build-operator-approval-intent",
        "--json",
        "--max-age-microseconds",
        "100",
        *_CONFIRM_FLAGS,
    ]
    code, payload = _run_approval_cli(argv, None, capsys)
    assert code == 1
    assert payload["reasons"] == ["approval_intent_config_required"]
    assert stdin_reads == []


def test_approval_cli_module_import_guard() -> None:
    source = _CLI_PATH.read_text(encoding="utf-8")
    for forbidden in ("socket", "websocket", "websockets", "http", "httpx", "urllib", "requests"):
        assert f"import {forbidden}" not in source
        assert f"from {forbidden}" not in source


# --- RTM-7c.4q verify-operator-approval-intent CLI ---


import test_operator_approval_intent_verifier as verify_helper

_VERIFY_ENVELOPE_KEYS = frozenset(
    {
        "outcome",
        "mode",
        "schema_version",
        "evidence_schema_version",
        "evidence_sha256",
        "approval_intent_sha256",
        "reason_codes",
        "activation_authorized",
        "runtime_activation_outcome",
        "approval_intent_authenticated",
        "approval_intent_consumed",
        "approval_intent_persisted",
    }
)


def _verify_intent_argv(*extra: str) -> list[str]:
    return ["--verify-operator-approval-intent", "--json", *extra]


def _run_verify_intent_cli(
    argv: list[str],
    payload: dict[str, Any] | None,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any]]:
    if payload is not None:
        sys.stdin = _stdin_bytes(json.dumps(payload).encode("utf-8"))  # type: ignore[assignment]
    code = cli.main(argv)
    captured = capsys.readouterr()
    body = json.loads(captured.out.strip().splitlines()[-1])
    return code, body


def _assert_verify_posture(payload: dict[str, Any]) -> None:
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"
    assert payload["approval_intent_authenticated"] is False
    assert payload["approval_intent_consumed"] is False
    assert payload["approval_intent_persisted"] is False


def test_verify_intent_cli_valid_builder_output(capsys: pytest.CaptureFixture[str]) -> None:
    intent = verify_helper._valid_intent_payload()
    code, payload = _run_verify_intent_cli(_verify_intent_argv(), intent, capsys)
    assert code == 0
    assert payload["outcome"] == "VALID"
    assert payload["mode"] == "verify-operator-approval-intent"
    assert payload["schema_version"] == 1
    assert payload["evidence_schema_version"] == 2
    assert _HEX64_RE.fullmatch(payload["evidence_sha256"])
    assert _HEX64_RE.fullmatch(payload["approval_intent_sha256"])
    assert payload["reason_codes"] == []
    _assert_verify_posture(payload)
    assert set(payload.keys()) == _VERIFY_ENVELOPE_KEYS


def test_verify_intent_cli_missing_json_early(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin_reads: list[int] = []

    def _spy_read(size: int = -1) -> bytes:
        stdin_reads.append(size)
        return b""

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_spy_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    monkeypatch.setattr(cli, "load_settings", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("config must not load")
    ))
    monkeypatch.setattr(
        cli,
        "datetime",
        type("_D", (), {"now": staticmethod(lambda tz=None: (_ for _ in ()).throw(
            AssertionError("clock must not read")
        ))})(),
    )

    code, payload = _run_verify_intent_cli(["--verify-operator-approval-intent"], None, capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["approval_intent_verification_json_required"]
    assert stdin_reads == []
    _assert_verify_posture(payload)


@pytest.mark.parametrize(
    "extra_argv",
    [
        pytest.param(["--config", "unused.toml"], id="config"),
        pytest.param(["--max-age-microseconds", "100"], id="max_age"),
        pytest.param(["--operator-approval-declared"], id="approval_flag"),
    ],
)
def test_verify_intent_cli_argument_not_applicable(
    extra_argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin_reads: list[int] = []

    def _spy_read(size: int = -1) -> bytes:
        stdin_reads.append(size)
        return b""

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_spy_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    code, payload = _run_verify_intent_cli(_verify_intent_argv(*extra_argv), None, capsys)
    assert code == 1
    assert payload["reason_codes"] == ["approval_intent_verification_argument_not_applicable"]
    assert stdin_reads == []


def test_verify_intent_cli_mode_conflict(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_verify_intent_cli(
        _verify_intent_argv("--build-operator-approval-intent"), None, capsys
    )
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["approval_intent_verification_mode_conflict"]


@pytest.mark.parametrize(
    ("stdin_data", "expected_reason"),
    [
        (None, "approval_intent_input_empty"),
        (b"\xff\xfe", "approval_intent_input_not_utf8"),
        (b"{not json", "approval_intent_input_not_json"),
        (b'{"schema_version": 1, "schema_version": 2}', "approval_intent_input_duplicate_key"),
        (b"[" * 5000 + b"0" + b"]" * 5000, "approval_intent_input_too_deep"),
        (b"x" * (cli._VERIFY_RECEIPT_STDIN_LIMIT + 1), "approval_intent_input_too_large"),
    ],
    ids=["empty", "invalid_utf8", "invalid_json", "duplicate_key", "too_deep", "too_large"],
)
def test_verify_intent_cli_stdin_failures(
    capsys: pytest.CaptureFixture[str],
    stdin_data: bytes | None,
    expected_reason: str,
) -> None:
    if stdin_data is not None:
        sys.stdin = _stdin_bytes(stdin_data)  # type: ignore[assignment]
    code, payload = _run_verify_intent_cli(_verify_intent_argv(), None, capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == [expected_reason]
    assert payload["schema_version"] is None
    assert payload["evidence_sha256"] is None
    _assert_verify_posture(payload)


def test_verify_intent_cli_tampered_invalid(capsys: pytest.CaptureFixture[str]) -> None:
    intent = verify_helper._valid_intent_payload()
    intent["operator_approval_declared"] = False
    code, payload = _run_verify_intent_cli(_verify_intent_argv(), intent, capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["approval_intent_invalid_declaration"]


def test_verify_intent_cli_isolation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    intent = verify_helper._valid_intent_payload()
    stdin_reads: list[int] = []

    def _spy_read(size: int = -1) -> bytes:
        stdin_reads.append(size)
        return json.dumps(intent).encode("utf-8")

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_spy_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())

    load_calls: list[str] = []
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda *_a, **_k: load_calls.append("load") or (_ for _ in ()).throw(
            AssertionError("load_settings must not run")
        ),
    )

    clock_calls: list[str] = []
    monkeypatch.setattr(
        cli,
        "datetime",
        type("_D", (), {"now": staticmethod(lambda tz=None: clock_calls.append("now") or (_ for _ in ()).throw(
            AssertionError("clock must not read")
        ))})(),
    )

    verifier_calls: list[str] = []
    real_verify = cli.verify_operator_approval_intent_payload

    def _spy_verify(payload: object) -> object:
        verifier_calls.append("verify")
        return real_verify(payload)

    monkeypatch.setattr(cli, "verify_operator_approval_intent_payload", _spy_verify)

    pipeline_calls: list[str] = []
    monkeypatch.setattr(
        cli,
        "freshness_qualify_and_build_candidate_evidence",
        lambda *_a, **_k: pipeline_calls.append("pipeline"),
    )
    builder_calls: list[str] = []
    monkeypatch.setattr(
        cli,
        "build_operator_approval_intent",
        lambda *_a, **_k: builder_calls.append("builder"),
    )

    code, payload = _run_verify_intent_cli(_verify_intent_argv(), None, capsys)
    assert code == 0
    assert payload["outcome"] == "VALID"
    assert len(stdin_reads) == 1
    assert verifier_calls == ["verify"]
    assert load_calls == []
    assert clock_calls == []
    assert pipeline_calls == []
    assert builder_calls == []


def test_verify_intent_cli_early_input_failure_skips_verifier(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    verifier_calls: list[str] = []
    monkeypatch.setattr(
        cli,
        "verify_operator_approval_intent_payload",
        lambda *_a, **_k: verifier_calls.append("verify"),
    )
    code, payload = _run_verify_intent_cli(_verify_intent_argv(), None, capsys)
    assert code == 1
    assert payload["reason_codes"] == ["approval_intent_input_empty"]
    assert verifier_calls == []


def test_verify_intent_cli_sanitizes_poison_output(capsys: pytest.CaptureFixture[str]) -> None:
    intent = verify_helper._valid_intent_payload()
    intent["evidence_sha256"] = "b" * 64
    intent["approval_intent_sha256"] = "c" * 64
    code, payload = _run_verify_intent_cli(_verify_intent_argv(), intent, capsys)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert "Traceback" not in combined
    assert "/home/" not in combined
    assert "KIS_" not in combined
    assert json.dumps(intent) not in combined
