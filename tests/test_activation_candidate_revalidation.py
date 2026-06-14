"""RTM-7c.4g — activation candidate revalidation API + CLI tests."""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config.settings as _settings_mod
from composition import activation_candidate_revalidation as reval_mod
from composition.activation_candidate_revalidation import (
    ActivationCandidateRevalidationOutcome,
    revalidate_activation_candidate,
)
from composition.paper_fast_loop import (
    InspectionOutcome,
    MachineCheckOutcome,
    PRECHECK_RECEIPT_ARTIFACT_NAMES,
    PaperFastLoopPaths,
    precheck_runtime,
)
from composition.sqlite_inspector import ArtifactFingerprint
from composition.verified_precheck_receipt import (
    VerifiedPrecheckReceipt,
    VerifiedReceiptSnapshotOutcome,
    verify_and_snapshot_precheck_receipt,
)
from config.settings import RuntimePaperFastLoopSettings

import test_paper_fast_loop_composition as pfl_helper
import test_precheck_receipt_verifier as vrf_helper


def _snapshot(payload: object) -> VerifiedPrecheckReceipt:
    result = verify_and_snapshot_precheck_receipt(payload)
    assert result.outcome is VerifiedReceiptSnapshotOutcome.VALID
    assert result.receipt is not None
    return result.receipt

_CLI_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_paper_fast_loop.py"
_spec = importlib.util.spec_from_file_location("run_paper_fast_loop", _CLI_PATH)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

_NOW = pfl_helper._NOW
_SYMBOL = pfl_helper._SYMBOL


def _settings(tmp_path: Path, **overrides: Any) -> RuntimePaperFastLoopSettings:
    return pfl_helper._settings(tmp_path, **overrides)


def _write_config(tmp_path: Path, *, enabled: bool = True, symbol: str = _SYMBOL) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[runtime.paper_fast_loop]
enabled = {str(enabled).lower()}
market = "KR"
symbol = "{symbol}"
""",
        encoding="utf-8",
    )
    return config_path


def _receipt_dict_from_precheck(tmp_path: Path, settings: RuntimePaperFastLoopSettings) -> dict[str, Any]:
    result = precheck_runtime(settings=settings, now=_NOW, base_dir=tmp_path)
    receipt = result.receipt
    return vrf_helper._receipt_to_dict(receipt)


def _assert_posture(result: Any) -> None:
    assert result.activation_authorized is False
    assert result.runtime_activation_outcome == "no_go"
    assert result.explicit_operator_approval_required is True
    assert result.writers_stopped_manual_confirmation_required is True
    assert result.freshness_evaluated is False


# --- 10.1 happy path ---


def test_revalidation_pass_on_matching_pass_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pfl_helper._seed_valid_stack(tmp_path, settings)
    receipt = _receipt_dict_from_precheck(tmp_path, settings)

    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )

    assert result.outcome is ActivationCandidateRevalidationOutcome.PASS
    _assert_posture(result)
    assert result.reasons == ()
    assert result.receipt_sha256 == receipt["receipt_sha256"]
    assert result.market == settings.market
    assert result.symbol == settings.symbol
    assert result.current_fingerprints_before == result.current_fingerprints_after
    assert len(result.current_fingerprints_before) == 4


# --- 10.2 receipt rejection ---


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("not-a-dict", "candidate_receipt_invalid"),
        ({"schema_version": 1}, "candidate_receipt_invalid"),
    ],
)
def test_revalidation_rejects_malformed_receipt(payload: object, reason: str, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=payload, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == (reason,)
    _assert_posture(result)


def test_revalidation_rejects_hash_mismatch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = vrf_helper._valid_receipt()
    receipt["checked_at"] = "2026-06-16T01:00:00+00:00"
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == ("candidate_receipt_invalid",)


def test_revalidation_rejects_valid_no_go_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = vrf_helper._valid_receipt(
        machine_outcome=MachineCheckOutcome.NO_GO,
        inspection_outcome=InspectionOutcome.NO_GO,
        reasons=("missing_database:ledger",),
    )
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == ("candidate_receipt_not_pass",)


def test_config_binding_rejects_market_mismatch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    object.__setattr__(settings, "market", "US")
    # a verified snapshot always carries the canonical market "KR"; a settings.market drift
    # is what the config binding catches.
    receipt = _snapshot(vrf_helper._valid_receipt())
    assert reval_mod._config_binding_reason(settings=settings, receipt=receipt) == (
        "candidate_market_mismatch"
    )


def test_revalidation_rejects_market_mismatch_against_valid_pass_receipt(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    receipt = vrf_helper._valid_receipt()
    object.__setattr__(settings, "market", "US")
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == ("candidate_market_mismatch",)


def test_revalidation_rejects_wrong_symbol(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = vrf_helper._valid_receipt(symbol="000660")
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == ("candidate_symbol_mismatch",)


def test_revalidation_rejects_enabled_mismatch(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=True)
    receipt = vrf_helper._valid_receipt(enabled=False)
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == ("candidate_enabled_mismatch",)


def test_revalidation_rejects_config_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)
    receipt = vrf_helper._valid_receipt(enabled=False)
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == ("candidate_config_disabled",)


# --- 10.3 artifact mismatch matrix ---


def _pass_receipt_for_seeded_stack(tmp_path: Path, settings: RuntimePaperFastLoopSettings) -> dict[str, Any]:
    pfl_helper._seed_valid_stack(tmp_path, settings)
    return _receipt_dict_from_precheck(tmp_path, settings)


def _mutate_artifact_size(path: Path) -> None:
    with path.open("ab") as fh:
        fh.write(b"\x00")


def _mutate_user_version_header(path: Path, version: int = 99) -> None:
    data = bytearray(path.read_bytes())
    data[60:64] = version.to_bytes(4, "big")
    path.write_bytes(data)


def _replace_with_directory(path: Path, *, is_sqlite: bool) -> None:
    if is_sqlite:
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
    path.unlink()
    path.mkdir()


@pytest.mark.parametrize(
    ("artifact_attr", "canonical_name", "mutator", "is_sqlite"),
    [
        ("snapshot_path", "execution_inputs_snapshot", _mutate_artifact_size, False),
        ("ledger_path", "ledger", _mutate_artifact_size, True),
        ("trigger_journal_path", "trigger_journal", _mutate_artifact_size, True),
        ("active_decision_store_path", "active_decision_store", _mutate_artifact_size, True),
    ],
    ids=["snapshot_size", "ledger_size", "journal_size", "active_store_size"],
)
def test_revalidation_receipt_artifact_mismatch_on_size_change(
    tmp_path: Path,
    artifact_attr: str,
    canonical_name: str,
    mutator: Any,
    is_sqlite: bool,
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    mutator(getattr(paths, artifact_attr))

    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == (f"candidate_receipt_artifact_mismatch:{canonical_name}",)


@pytest.mark.parametrize(
    ("artifact_attr", "canonical_name"),
    [
        ("ledger_path", "ledger"),
        ("trigger_journal_path", "trigger_journal"),
        ("active_decision_store_path", "active_decision_store"),
    ],
)
def test_revalidation_receipt_artifact_mismatch_on_user_version_change(
    tmp_path: Path, artifact_attr: str, canonical_name: str
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    _mutate_user_version_header(getattr(paths, artifact_attr))

    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == (f"candidate_receipt_artifact_mismatch:{canonical_name}",)


@pytest.mark.parametrize(
    ("artifact_attr", "canonical_name"),
    [
        ("ledger_path", "ledger"),
        ("trigger_journal_path", "trigger_journal"),
        ("active_decision_store_path", "active_decision_store"),
    ],
)
def test_revalidation_receipt_artifact_mismatch_on_sidecar(
    tmp_path: Path, artifact_attr: str, canonical_name: str
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    target = getattr(paths, artifact_attr)
    target.with_name(target.name + "-wal").write_bytes(b"wal")

    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == (f"candidate_receipt_artifact_mismatch:{canonical_name}",)


@pytest.mark.parametrize(
    ("artifact_attr", "canonical_name", "is_sqlite"),
    [
        ("snapshot_path", "execution_inputs_snapshot", False),
        ("ledger_path", "ledger", True),
        ("trigger_journal_path", "trigger_journal", True),
        ("active_decision_store_path", "active_decision_store", True),
    ],
)
def test_revalidation_receipt_artifact_mismatch_on_missing(
    tmp_path: Path, artifact_attr: str, canonical_name: str, is_sqlite: bool
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    getattr(paths, artifact_attr).unlink()

    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == (f"candidate_receipt_artifact_mismatch:{canonical_name}",)


@pytest.mark.parametrize(
    ("artifact_attr", "canonical_name", "is_sqlite"),
    [
        ("snapshot_path", "execution_inputs_snapshot", False),
        ("ledger_path", "ledger", True),
        ("trigger_journal_path", "trigger_journal", True),
        ("active_decision_store_path", "active_decision_store", True),
    ],
)
def test_revalidation_receipt_artifact_mismatch_on_irregular_path(
    tmp_path: Path, artifact_attr: str, canonical_name: str, is_sqlite: bool
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    _replace_with_directory(getattr(paths, artifact_attr), is_sqlite=is_sqlite)

    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == (f"candidate_receipt_artifact_mismatch:{canonical_name}",)


# --- H1 carry-over: fingerprint read fail-closed ---


def _raising_fp(real: Any, *, target: str, fail_on_call: int, exc: BaseException) -> Any:
    seen: dict[str, int] = {}

    def _fp(path: str | Path, *, name: str, is_sqlite: bool) -> ArtifactFingerprint:
        if name == target:
            seen[name] = seen.get(name, 0) + 1
            if seen[name] == fail_on_call:
                raise exc
        return real(path, name=name, is_sqlite=is_sqlite)

    return _fp


@pytest.mark.parametrize("artifact_name", list(PRECHECK_RECEIPT_ARTIFACT_NAMES))
@pytest.mark.parametrize("fail_on_call", [1, 2], ids=["first_pass", "second_pass"])
@pytest.mark.parametrize(
    "exc_cls",
    [FileNotFoundError, PermissionError, OSError],
    ids=["filenotfound", "permission", "oserror"],
)
def test_revalidation_artifact_unreadable_is_stable_no_go(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_name: str,
    fail_on_call: int,
    exc_cls: type[OSError],
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    real_fp = reval_mod.sqlite_inspector.fingerprint_artifact
    leak_token = "SeNtInEl_RaW_PaTh_42"
    exc = exc_cls(leak_token)
    monkeypatch.setattr(
        reval_mod.sqlite_inspector,
        "fingerprint_artifact",
        _raising_fp(real_fp, target=artifact_name, fail_on_call=fail_on_call, exc=exc),
    )

    # pure API must NOT propagate the OSError — it returns a stable NO_GO.
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == (f"candidate_artifact_unreadable:{artifact_name}",)
    # no raw exception type / message / path leaked into any reason.
    for reason in result.reasons:
        assert leak_token not in reason
        assert exc_cls.__name__ not in reason
    _assert_posture(result)


def test_revalidation_artifact_unreadable_canonical_order_multi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    real_fp = reval_mod.sqlite_inspector.fingerprint_artifact

    def _fp(path: str | Path, *, name: str, is_sqlite: bool) -> ArtifactFingerprint:
        if name in ("ledger", "active_decision_store"):
            raise PermissionError("denied")
        return real_fp(path, name=name, is_sqlite=is_sqlite)

    monkeypatch.setattr(reval_mod.sqlite_inspector, "fingerprint_artifact", _fp)
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    # one reason per failing artifact, canonical order (ledger before active_decision_store).
    assert result.reasons == (
        "candidate_artifact_unreadable:ledger",
        "candidate_artifact_unreadable:active_decision_store",
    )


def test_revalidate_cli_artifact_unreadable_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    real_fp = reval_mod.sqlite_inspector.fingerprint_artifact

    def _fp(path: str | Path, *, name: str, is_sqlite: bool) -> ArtifactFingerprint:
        if name == "trigger_journal":
            raise OSError("boom")
        return real_fp(path, name=name, is_sqlite=is_sqlite)

    monkeypatch.setattr(reval_mod.sqlite_inspector, "fingerprint_artifact", _fp)
    code, payload = _run_revalidate(
        ["--config", str(config_path), "--revalidate-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    # exact stable reason, NOT a generic "revalidate error:<type>".
    assert payload["reasons"] == ["candidate_artifact_unreadable:trigger_journal"]
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


# --- 10.4 revalidation-window drift ---


def test_revalidation_window_drift_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    real_fp = reval_mod.sqlite_inspector.fingerprint_artifact
    calls = {"n": 0}

    def _drifting_fp(path: str | Path, *, name: str, is_sqlite: bool) -> ArtifactFingerprint:
        fp = real_fp(path, name=name, is_sqlite=is_sqlite)
        if name == "ledger":
            calls["n"] += 1
            if calls["n"] == 1:
                return fp
            return ArtifactFingerprint(
                name=fp.name,
                present=fp.present,
                is_regular_file=fp.is_regular_file,
                size=(fp.size or 0) + 1,
                sha256="ff" * 32,
                user_version=fp.user_version,
                sidecar_suffixes=fp.sidecar_suffixes,
            )
        return fp

    monkeypatch.setattr(reval_mod.sqlite_inspector, "fingerprint_artifact", _drifting_fp)

    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == ("candidate_current_artifact_drift:ledger",)
    assert "candidate_receipt_artifact_mismatch:ledger" not in result.reasons


@pytest.mark.parametrize("artifact_name", list(PRECHECK_RECEIPT_ARTIFACT_NAMES))
def test_revalidation_window_drift_per_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact_name: str
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    real_fp = reval_mod.sqlite_inspector.fingerprint_artifact
    seen: dict[str, int] = {}

    def _drifting_fp(path: str | Path, *, name: str, is_sqlite: bool) -> ArtifactFingerprint:
        fp = real_fp(path, name=name, is_sqlite=is_sqlite)
        if name != artifact_name:
            return fp
        count = seen.get(name, 0) + 1
        seen[name] = count
        if count == 1:
            return fp
        return ArtifactFingerprint(
            name=fp.name,
            present=fp.present,
            is_regular_file=fp.is_regular_file,
            size=(fp.size or 0) + 1,
            sha256="ee" * 32,
            user_version=fp.user_version,
            sidecar_suffixes=fp.sidecar_suffixes,
        )

    monkeypatch.setattr(reval_mod.sqlite_inspector, "fingerprint_artifact", _drifting_fp)

    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.NO_GO
    assert result.reasons == (f"candidate_current_artifact_drift:{artifact_name}",)


# --- 10.5 exact comparison target (fingerprints_after) ---


def test_artifact_reasons_compare_against_receipt_after_not_before() -> None:
    current = vrf_helper._four_fps()
    different_before = tuple(
        ArtifactFingerprint(
            name=fp.name,
            present=fp.present,
            is_regular_file=fp.is_regular_file,
            size=fp.size,
            sha256="11" * 32,
            user_version=fp.user_version,
            sidecar_suffixes=fp.sidecar_suffixes,
        )
        for fp in current
    )
    # current == receipt_after, but receipt_before differs — must PASS when target is after.
    reasons = reval_mod._artifact_revalidation_reasons(
        current_before=current,
        current_after=current,
        receipt_target=current,
    )
    assert reasons == ()
    mismatch_if_before = reval_mod._artifact_revalidation_reasons(
        current_before=current,
        current_after=current,
        receipt_target=different_before,
    )
    assert mismatch_if_before == (
        "candidate_receipt_artifact_mismatch:execution_inputs_snapshot",
        "candidate_receipt_artifact_mismatch:ledger",
        "candidate_receipt_artifact_mismatch:trigger_journal",
        "candidate_receipt_artifact_mismatch:active_decision_store",
    )


def test_revalidation_does_not_accept_invalid_pass_receipt_bypassing_verifier(
    tmp_path: Path,
) -> None:
    # verifier 우회 invalid PASS drift receipt — candidate_receipt_invalid.
    settings = _settings(tmp_path)
    poison = vrf_helper._valid_receipt()
    poison["machine_outcome"] = "pass"
    poison["inspection_outcome"] = "ok"
    poison["reasons"] = []
    poison["receipt_sha256"] = "ab" * 32
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=poison, base_dir=tmp_path
    )
    assert result.reasons == ("candidate_receipt_invalid",)


# --- 10.6 isolation (API) ---


def test_revalidation_api_zero_sqlite_connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    pfl_helper._seed_valid_stack(tmp_path, settings)
    receipt = _receipt_dict_from_precheck(tmp_path, settings)
    connect_calls: list[str] = []

    real_connect = sqlite3.connect

    def _spy_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connect_calls.append("connect")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.PASS
    assert connect_calls == []


def test_revalidation_api_zero_store_constructors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    pfl_helper._seed_valid_stack(tmp_path, settings)
    receipt = _receipt_dict_from_precheck(tmp_path, settings)

    def _fail_store(*_a: object, **_k: object) -> object:
        raise AssertionError("store constructor must not run during revalidation")

    monkeypatch.setattr(pfl_helper.ActiveDecisionStore, "__init__", _fail_store)
    monkeypatch.setattr(pfl_helper.SQLiteLedger, "__init__", _fail_store)
    monkeypatch.setattr(pfl_helper.SqliteTriggerJournal, "__init__", _fail_store)

    result = revalidate_activation_candidate(
        settings=settings, receipt_payload=receipt, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateRevalidationOutcome.PASS


def test_revalidation_api_has_no_clock_read_in_source() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "activation_candidate_revalidation.py"
    ).read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "time.time" not in source
    assert "time.monotonic" not in source


# --- CLI tests ---


def _stdin_bytes(data: bytes) -> object:
    class _Stdin:
        buffer = io.BytesIO(data)

    return _Stdin()


def _run_revalidate(
    argv: list[str], receipt: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    sys.stdin = _stdin_bytes(json.dumps(receipt).encode("utf-8"))  # type: ignore[assignment]
    code = cli.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return code, payload


def test_revalidate_cli_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    pfl_helper._seed_valid_stack(tmp_path, settings)
    receipt = _receipt_dict_from_precheck(tmp_path, settings)

    code, payload = _run_revalidate(
        ["--config", str(config_path), "--revalidate-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 0
    assert payload["outcome"] == "PASS"
    assert payload["mode"] == "revalidate-activation-candidate"
    assert payload["reasons"] == []
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"
    assert payload["freshness_evaluated"] is False
    assert payload["credential_read"] is False
    assert payload["network_called"] is False
    assert payload["database_opened"] is False
    assert payload["filesystem_written"] is False


def test_revalidate_cli_no_go_on_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    paths.ledger_path.unlink()

    code, payload = _run_revalidate(
        ["--config", str(config_path), "--revalidate-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["candidate_receipt_artifact_mismatch:ledger"]


def test_revalidate_cli_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_revalidate(
        ["--revalidate-activation-candidate", "--verify-precheck-receipt", "--json"],
        vrf_helper._valid_receipt(),
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert "mutually exclusive" in payload["reason_code"]


class _NoEnvironAccess:
    _MSG = "revalidate must not read os.environ"

    def __getitem__(self, key: object) -> str:
        raise AssertionError(f"{self._MSG} (__getitem__ {key!r})")

    def __contains__(self, key: object) -> bool:
        raise AssertionError(f"{self._MSG} (__contains__ {key!r})")

    def get(self, key: object, default: object = None) -> object:
        raise AssertionError(f"{self._MSG} (get {key!r})")

    def __iter__(self):
        raise AssertionError(f"{self._MSG} (__iter__)")

    def keys(self):
        raise AssertionError(f"{self._MSG} (keys)")

    def copy(self):
        raise AssertionError(f"{self._MSG} (copy)")


import os as _real_os  # noqa: E402


class _OsShim:
    environ = _NoEnvironAccess()

    def __getattr__(self, name: str) -> object:
        return getattr(_real_os, name)


def _patch_settings_environ_spy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings_mod, "os", _OsShim())


def test_revalidate_cli_zero_environ_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_settings_environ_spy(monkeypatch)
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    pfl_helper._seed_valid_stack(tmp_path, settings)
    receipt = _receipt_dict_from_precheck(tmp_path, settings)
    code, payload = _run_revalidate(
        ["--config", str(config_path), "--revalidate-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 0
    assert payload["outcome"] == "PASS"


def test_revalidate_cli_env_substitution_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _patch_settings_environ_spy(monkeypatch)
    config_path = tmp_path / "config_env.toml"
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
    receipt = vrf_helper._valid_receipt()
    code, payload = _run_revalidate(
        ["--config", str(config_path), "--revalidate-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["config error: ConfigEnvironmentError"]


def test_revalidate_cli_zero_sqlite_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    pfl_helper._seed_valid_stack(tmp_path, settings)
    receipt = _receipt_dict_from_precheck(tmp_path, settings)
    connect_calls: list[str] = []

    real_connect = sqlite3.connect

    def _spy_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connect_calls.append("connect")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    code, _payload = _run_revalidate(
        ["--config", str(config_path), "--revalidate-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 0
    assert connect_calls == []


def test_revalidate_cli_zero_filesystem_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    pfl_helper._seed_valid_stack(tmp_path, settings)
    receipt = _receipt_dict_from_precheck(tmp_path, settings)
    writes: list[str] = []
    real_open = open

    def _spy_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(ch in mode for ch in ("w", "a", "x", "+")) and "r" not in mode.replace("+", ""):
            writes.append(str(file))
        elif "+" in mode:
            writes.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _spy_open)
    code, payload = _run_revalidate(
        ["--config", str(config_path), "--revalidate-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 0
    assert payload["filesystem_written"] is False
    assert writes == []


def test_revalidate_cli_stdin_empty(capsys: pytest.CaptureFixture[str]) -> None:
    sys.stdin = _stdin_bytes(b"")  # type: ignore[assignment]
    code = cli.main(["--revalidate-activation-candidate", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["receipt_input_empty"]


def test_precheck_and_revalidate_regression_valid_receipts(tmp_path: Path) -> None:
    # 10.7 — precheck PASS/NO_GO receipts remain verifier VALID.
    pass_settings = _settings(tmp_path / "pass")
    pfl_helper._seed_valid_stack(tmp_path / "pass", pass_settings)
    pass_result = precheck_runtime(settings=pass_settings, now=_NOW, base_dir=tmp_path / "pass")
    pass_payload = vrf_helper._receipt_to_dict(pass_result.receipt)
    assert (
        vrf_helper.verify_runtime_precheck_receipt_payload(pass_payload).outcome
        is vrf_helper.ReceiptVerificationOutcome.VALID
    )

    no_go_settings = _settings(tmp_path / "nogo")
    no_go_result = precheck_runtime(settings=no_go_settings, now=_NOW, base_dir=tmp_path / "nogo")
    no_go_payload = vrf_helper._receipt_to_dict(no_go_result.receipt)
    assert (
        vrf_helper.verify_runtime_precheck_receipt_payload(no_go_payload).outcome
        is vrf_helper.ReceiptVerificationOutcome.VALID
    )
    reval = revalidate_activation_candidate(
        settings=no_go_settings, receipt_payload=no_go_payload, base_dir=tmp_path / "nogo"
    )
    assert reval.reasons == ("candidate_receipt_not_pass",)
