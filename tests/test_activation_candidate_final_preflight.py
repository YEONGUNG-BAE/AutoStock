"""RTM-7c.4h — time-aware final activation candidate preflight API + CLI tests.

Composition: 4g byte-state revalidation + fresh current-time machine precheck. The composed
``precheck_runtime`` opens the configured DBs READ-ONLY (``mode=ro`` + ``PRAGMA query_only``);
isolation tests therefore assert no *write* / no operational mutation, not zero connections.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config.settings as _settings_mod
from composition import activation_candidate_final_preflight as final_mod
from composition import sqlite_inspector
from composition.activation_candidate_final_preflight import (
    ActivationCandidateFinalPreflightOutcome,
    final_preflight_activation_candidate,
)
from composition.paper_fast_loop import (
    MachineCheckOutcome,
    PaperFastLoopPaths,
    precheck_runtime,
)
from composition.paper_fast_loop_artifacts import PAPER_FAST_LOOP_ARTIFACT_NAMES
from composition.sqlite_inspector import ArtifactFingerprint
from config.settings import RuntimePaperFastLoopSettings

import test_paper_fast_loop_composition as pfl_helper
import test_precheck_receipt_verifier as vrf_helper

_CLI_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_paper_fast_loop.py"
_spec = importlib.util.spec_from_file_location("run_paper_fast_loop", _CLI_PATH)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

_NOW = pfl_helper._NOW
_SYMBOL = pfl_helper._SYMBOL
_KST = cli._KST
# now choices: receipt is built at _NOW; these probe time-validity with identical bytes.
_NOW_KST = _NOW.astimezone(_KST)
_FUTURE = _NOW + timedelta(days=2)  # snapshot + active decision expired
_PAST = _NOW - timedelta(days=2)  # snapshot + active decision not-yet-valid


def _settings(tmp_path: Path, **overrides: Any) -> RuntimePaperFastLoopSettings:
    return pfl_helper._settings(tmp_path, **overrides)


def _receipt_dict_from_precheck(tmp_path: Path, settings: RuntimePaperFastLoopSettings) -> dict[str, Any]:
    result = precheck_runtime(settings=settings, now=_NOW, base_dir=tmp_path)
    return vrf_helper._receipt_to_dict(result.receipt)


def _pass_receipt_for_seeded_stack(tmp_path: Path, settings: RuntimePaperFastLoopSettings) -> dict[str, Any]:
    pfl_helper._seed_valid_stack(tmp_path, settings)
    return _receipt_dict_from_precheck(tmp_path, settings)


def _assert_posture(result: Any, *, fresh_precheck_executed: bool) -> None:
    """Assert the constant activation posture plus the per-call execution flag.

    ``fresh_precheck_executed`` must be ``True`` only when the composed precheck actually
    ran (PASS, or a fresh-precheck machine NO_GO, or post-revalidation drift) and ``False``
    for every short-circuit that returns before it (naive ``now``, any 4g NO_GO)."""

    assert result.activation_authorized is False
    assert result.runtime_activation_outcome == "no_go"
    assert result.explicit_operator_approval_required is True
    assert result.writers_stopped_manual_confirmation_required is True
    assert result.fresh_precheck_executed is fresh_precheck_executed
    assert result.receipt_age_evaluated is False
    assert result.freshness_policy_evaluated is False
    assert not hasattr(result, "current_validity_evaluated")


# --- 15.2 happy path ---


def test_final_preflight_pass_on_valid_now(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)

    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )

    assert result.outcome is ActivationCandidateFinalPreflightOutcome.PASS
    assert result.reasons == ()
    assert result.receipt_sha256 == receipt["receipt_sha256"]
    assert result.market == settings.market
    assert result.symbol == settings.symbol
    assert result.current_precheck_result is not None
    assert result.current_precheck_result.machine_outcome is MachineCheckOutcome.PASS
    assert result.revalidation_result is not None
    _assert_posture(result, fresh_precheck_executed=True)


def test_final_preflight_pass_with_kst_aware_now(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    # same instant, KST tz — must behave identically to the UTC-aware now.
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW_KST, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateFinalPreflightOutcome.PASS
    _assert_posture(result, fresh_precheck_executed=True)


# --- 15.3 current time validity (byte-identical, time-window NO_GO) ---


def test_final_preflight_no_go_when_snapshot_and_decision_expired(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)

    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_FUTURE, base_dir=tmp_path
    )

    assert result.outcome is ActivationCandidateFinalPreflightOutcome.NO_GO
    # revalidation (byte-state) PASSed; the fresh precheck at a later now found the expiry.
    assert result.revalidation_result is not None
    assert "candidate_current_precheck:execution_inputs_expired" in result.reasons
    assert "candidate_current_precheck:active_decision_expired" in result.reasons
    # every current-validity reason carries the stable prefix; no raw precheck reason leaks.
    assert all(r.startswith("candidate_current_precheck:") for r in result.reasons)
    _assert_posture(result, fresh_precheck_executed=True)


def test_final_preflight_no_go_when_snapshot_and_decision_not_yet_valid(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)

    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_PAST, base_dir=tmp_path
    )

    assert result.outcome is ActivationCandidateFinalPreflightOutcome.NO_GO
    assert "candidate_current_precheck:execution_inputs_not_yet_valid" in result.reasons
    assert "candidate_current_precheck:active_decision_not_yet_valid" in result.reasons
    assert all(r.startswith("candidate_current_precheck:") for r in result.reasons)
    _assert_posture(result, fresh_precheck_executed=True)


def test_final_preflight_current_precheck_result_carried_on_no_go(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_FUTURE, base_dir=tmp_path
    )
    assert result.current_precheck_result is not None
    assert result.current_precheck_result.machine_outcome is MachineCheckOutcome.NO_GO
    assert result.fresh_precheck_executed is True


# Byte-changing failures (corrupt / nonterminal / non-quiescent) cannot survive a PASS receipt
# with identical bytes: they are caught at Step 1 revalidation (artifact mismatch), never
# reaching the fresh precheck. They still yield a final NO_GO — via the revalidation reason.


def test_final_preflight_no_go_on_non_quiescent_caught_at_revalidation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    # live WAL sidecar appears after the receipt → fingerprint differs → revalidation mismatch.
    paths.ledger_path.with_name(paths.ledger_path.name + "-wal").write_bytes(b"wal")

    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateFinalPreflightOutcome.NO_GO
    assert result.reasons == ("candidate_receipt_artifact_mismatch:ledger",)
    assert result.current_precheck_result is None  # never reached the fresh precheck
    assert result.fresh_precheck_executed is False


def test_final_preflight_no_go_on_corrupt_active_bundle_caught_at_revalidation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    with paths.active_decision_store_path.open("ab") as fh:
        fh.write(b"\x00")

    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateFinalPreflightOutcome.NO_GO
    assert result.reasons == ("candidate_receipt_artifact_mismatch:active_decision_store",)
    assert result.current_precheck_result is None
    assert result.fresh_precheck_executed is False


# --- 15.4 post-revalidation drift ---


def _drift_after_revalidation(real: Any, *, target: str) -> Any:
    seen: dict[str, int] = {}

    def _fp(path: str | Path, *, name: str, is_sqlite: bool) -> ArtifactFingerprint:
        fp = real(path, name=name, is_sqlite=is_sqlite)
        if name != target:
            return fp
        seen[name] = seen.get(name, 0) + 1
        # calls 1,2 = revalidation before/after (real → matches receipt, 4g PASS);
        # calls 3+ = fresh precheck before/after (drifted, but identical across its window).
        if seen[name] <= 2:
            return fp
        return ArtifactFingerprint(
            name=fp.name,
            present=fp.present,
            is_regular_file=fp.is_regular_file,
            size=(fp.size or 0) + 7,
            sha256="cd" * 32,
            user_version=fp.user_version,
            sidecar_suffixes=fp.sidecar_suffixes,
        )

    return _fp


@pytest.mark.parametrize("artifact_name", list(PAPER_FAST_LOOP_ARTIFACT_NAMES))
def test_final_preflight_post_revalidation_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, artifact_name: str
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    real_fp = sqlite_inspector.fingerprint_artifact
    monkeypatch.setattr(
        sqlite_inspector, "fingerprint_artifact", _drift_after_revalidation(real_fp, target=artifact_name)
    )

    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateFinalPreflightOutcome.NO_GO
    assert result.reasons == (f"candidate_post_revalidation_artifact_drift:{artifact_name}",)
    # drift is NOT double-counted as the fresh precheck's own within-window change.
    assert f"precheck_artifact_changed:{artifact_name}" not in result.reasons
    assert not any(r.startswith("candidate_current_precheck:") for r in result.reasons)
    _assert_posture(result, fresh_precheck_executed=True)


# --- 15.5 receipt / config rejection (4g matrix preserved) ---


def test_final_preflight_rejects_invalid_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload={"schema_version": 1}, now=_NOW, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateFinalPreflightOutcome.NO_GO
    assert result.reasons == ("candidate_receipt_invalid",)
    assert result.current_precheck_result is None
    _assert_posture(result, fresh_precheck_executed=False)


def test_final_preflight_rejects_no_go_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = vrf_helper._valid_receipt(
        machine_outcome=MachineCheckOutcome.NO_GO,
        inspection_outcome=pfl_helper.InspectionOutcome.NO_GO,
        reasons=("missing_database:ledger",),
    )
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )
    assert result.reasons == ("candidate_receipt_not_pass",)
    assert result.current_precheck_result is None
    _assert_posture(result, fresh_precheck_executed=False)


def test_final_preflight_rejects_market_mismatch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = vrf_helper._valid_receipt()
    object.__setattr__(settings, "market", "US")
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )
    assert result.reasons == ("candidate_market_mismatch",)
    assert result.current_precheck_result is None
    _assert_posture(result, fresh_precheck_executed=False)


def test_final_preflight_rejects_symbol_mismatch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    receipt = vrf_helper._valid_receipt(symbol="000660")
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )
    assert result.reasons == ("candidate_symbol_mismatch",)
    assert result.current_precheck_result is None
    _assert_posture(result, fresh_precheck_executed=False)


def test_final_preflight_rejects_config_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)
    receipt = vrf_helper._valid_receipt(enabled=False)
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )
    assert result.reasons == ("candidate_config_disabled",)
    assert result.current_precheck_result is None
    _assert_posture(result, fresh_precheck_executed=False)


# --- 15.6 time contract ---


@pytest.mark.parametrize(
    "naive_now",
    [datetime(2026, 6, 16, 0, 30), datetime.now().replace(tzinfo=None)],  # noqa: DTZ005
    ids=["fixed_naive", "naive_now"],
)
def test_final_preflight_naive_now_is_fail_closed(tmp_path: Path, naive_now: datetime) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=naive_now, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateFinalPreflightOutcome.NO_GO
    assert result.reasons == ("candidate_invalid_now",)
    # never reached revalidation or precheck.
    assert result.revalidation_result is None
    assert result.current_precheck_result is None
    _assert_posture(result, fresh_precheck_executed=False)


def test_final_preflight_naive_now_does_not_call_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)

    def _fail_precheck(*_a: object, **_k: object) -> object:
        raise AssertionError("precheck_runtime must not run for a naive now")

    monkeypatch.setattr(final_mod, "precheck_runtime", _fail_precheck)
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=datetime(2026, 6, 16), base_dir=tmp_path
    )
    assert result.reasons == ("candidate_invalid_now",)
    assert result.fresh_precheck_executed is False


def test_final_preflight_api_has_no_clock_read_in_source() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "composition"
        / "activation_candidate_final_preflight.py"
    ).read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "time.time" not in source
    assert "time.monotonic" not in source


# --- fresh-precheck execution truthfulness (P1 closure) ---


def _count_precheck_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = final_mod.precheck_runtime

    def _spy(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(final_mod, "precheck_runtime", _spy)
    return calls


def _no_go_receipt() -> dict[str, Any]:
    return vrf_helper._valid_receipt(
        machine_outcome=MachineCheckOutcome.NO_GO,
        inspection_outcome=pfl_helper.InspectionOutcome.NO_GO,
        reasons=("missing_database:ledger",),
    )


def test_final_preflight_short_circuit_paths_do_not_run_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every pre-precheck short-circuit: precheck_runtime call count 0 and
    fresh_precheck_executed False."""
    settings = _settings(tmp_path)
    valid = vrf_helper._valid_receipt()

    cases: list[tuple[RuntimePaperFastLoopSettings, object, datetime]] = [
        (settings, valid, datetime(2026, 6, 16)),  # naive now
        (settings, {"schema_version": 1}, _NOW),  # invalid receipt
        (settings, _no_go_receipt(), _NOW),  # machine NO_GO receipt
        (_settings(tmp_path, enabled=False), vrf_helper._valid_receipt(enabled=False), _NOW),
        (_market(settings, "US"), vrf_helper._valid_receipt(), _NOW),  # market mismatch
        (settings, vrf_helper._valid_receipt(symbol="000660"), _NOW),  # symbol mismatch
    ]
    for case_settings, receipt, now in cases:
        calls = _count_precheck_calls(monkeypatch)
        result = final_preflight_activation_candidate(
            settings=case_settings, receipt_payload=receipt, now=now, base_dir=tmp_path
        )
        assert result.outcome is ActivationCandidateFinalPreflightOutcome.NO_GO
        assert result.fresh_precheck_executed is False
        assert result.current_precheck_result is None
        assert len(calls) == 0
        monkeypatch.undo()


def _market(settings: RuntimePaperFastLoopSettings, value: str) -> RuntimePaperFastLoopSettings:
    object.__setattr__(settings, "market", value)
    return settings


@pytest.mark.parametrize("now", [_NOW, _FUTURE, _PAST], ids=["pass", "expired", "not_yet_valid"])
def test_final_preflight_executed_paths_run_precheck_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, now: datetime
) -> None:
    """PASS and fresh-precheck NO_GO both run precheck_runtime exactly once and report
    fresh_precheck_executed True."""
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    calls = _count_precheck_calls(monkeypatch)

    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=now, base_dir=tmp_path
    )
    assert len(calls) == 1
    assert result.fresh_precheck_executed is True
    assert result.current_precheck_result is not None


# --- 15.7 isolation (read-only connections allowed; no write/network/broker/env/spawn) ---


def test_final_preflight_zero_store_constructors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)

    def _fail_store(*_a: object, **_k: object) -> object:
        raise AssertionError("store constructor must not run during final preflight")

    monkeypatch.setattr(pfl_helper.ActiveDecisionStore, "__init__", _fail_store)
    monkeypatch.setattr(pfl_helper.SQLiteLedger, "__init__", _fail_store)
    monkeypatch.setattr(pfl_helper.SqliteTriggerJournal, "__init__", _fail_store)

    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateFinalPreflightOutcome.PASS


def test_final_preflight_sqlite_connections_are_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    uris: list[str] = []
    real_connect = sqlite3.connect

    def _spy_connect(target: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        uris.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateFinalPreflightOutcome.PASS
    # the composed precheck DOES open DBs (read-only): at least one connection must occur,
    # and every connection is mode=ro with zero write-capable (mode=rw / mode=rwc) opens.
    assert len(uris) >= 1
    for uri in uris:
        assert "mode=ro" in uri
        assert "mode=rwc" not in uri
        assert "mode=rw&" not in uri and not uri.endswith("mode=rw")


def test_final_preflight_zero_filesystem_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    writes: list[str] = []
    real_open = open

    def _spy_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(ch in mode for ch in ("w", "a", "x")) or "+" in mode:
            writes.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _spy_open)
    result = final_preflight_activation_candidate(
        settings=settings, receipt_payload=receipt, now=_NOW, base_dir=tmp_path
    )
    assert result.outcome is ActivationCandidateFinalPreflightOutcome.PASS
    assert writes == []


# --- CLI ---


def _stdin_bytes(data: bytes) -> object:
    class _Stdin:
        buffer = io.BytesIO(data)

    return _Stdin()


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


class _FixedClock:
    """Pins ``cli.datetime.now(tz=...)`` to ``_NOW`` (== helper._NOW, inside the seeded
    validity windows) so a seeded stack the receipt was built against stays time-valid."""

    @staticmethod
    def now(tz: object = None) -> datetime:
        return _NOW.astimezone(_KST) if tz is not None else _NOW


class _FutureClock:
    """Pins the CLI clock two days past ``_NOW`` so the seeded windows have closed."""

    @staticmethod
    def now(tz: object = None) -> datetime:
        return _FUTURE.astimezone(_KST) if tz is not None else _FUTURE


def _run_final(
    argv: list[str], receipt: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    sys.stdin = _stdin_bytes(json.dumps(receipt).encode("utf-8"))  # type: ignore[assignment]
    code = cli.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return code, payload


def test_final_preflight_cli_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)

    code, payload = _run_final(
        ["--config", str(config_path), "--final-preflight-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 0
    assert payload["outcome"] == "PASS"
    assert payload["mode"] == "final-preflight-activation-candidate"
    assert payload["reasons"] == []
    assert payload["current_precheck_outcome"] == "pass"
    assert payload["current_precheck_reasons"] == []
    assert payload["fresh_precheck_executed"] is True
    assert payload["receipt_age_evaluated"] is False
    assert payload["freshness_policy_evaluated"] is False
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"
    assert payload["explicit_operator_approval_required"] is True
    assert payload["writers_stopped_manual_confirmation_required"] is True
    assert payload["credential_read"] is False
    assert payload["network_called"] is False
    assert payload["broker_called"] is False
    assert payload["operational_db_written"] is False
    assert payload["filesystem_written"] is False
    # misleading DB-open telemetry must not be emitted.
    assert "read_only_databases_opened" not in payload
    assert "database_opened" not in payload
    assert "current_validity_evaluated" not in payload


def test_final_preflight_cli_no_go_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", _FutureClock)
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    # Build a PASS receipt against the seeded stack at its valid time, then let the CLI
    # evaluate it with a clock two days later so the windows have closed.
    pfl_helper._seed_valid_stack(tmp_path, settings)
    receipt = _receipt_dict_from_precheck(tmp_path, settings)

    code, payload = _run_final(
        ["--config", str(config_path), "--final-preflight-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["current_precheck_outcome"] == "no_go"
    assert payload["fresh_precheck_executed"] is True
    assert any(r.startswith("candidate_current_precheck:") for r in payload["reasons"])
    assert "candidate_current_precheck:execution_inputs_expired" in payload["reasons"]


def test_final_preflight_cli_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_final(
        ["--final-preflight-activation-candidate", "--revalidate-activation-candidate", "--json"],
        vrf_helper._valid_receipt(),
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert "mutually exclusive" in payload["reason_code"]


def test_final_preflight_cli_stdin_empty(capsys: pytest.CaptureFixture[str]) -> None:
    sys.stdin = _stdin_bytes(b"")  # type: ignore[assignment]
    code = cli.main(["--final-preflight-activation-candidate", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["receipt_input_empty"]
    assert payload["fresh_precheck_executed"] is False
    assert "read_only_databases_opened" not in payload
    assert "database_opened" not in payload
    assert "current_validity_evaluated" not in payload


def test_final_preflight_cli_config_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path, enabled=False)
    settings = _settings(tmp_path, enabled=False)
    pfl_helper._seed_valid_stack(tmp_path, settings)
    receipt = vrf_helper._valid_receipt(enabled=False)
    code, payload = _run_final(
        ["--config", str(config_path), "--final-preflight-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["reasons"] == ["candidate_config_disabled"]
    # revalidation short-circuit: fresh precheck never ran, no current-precheck outcome.
    assert payload["fresh_precheck_executed"] is False
    assert payload["current_precheck_outcome"] is None
    assert "read_only_databases_opened" not in payload


class _NoEnvironAccess:
    _MSG = "final preflight must not read os.environ"

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


def test_final_preflight_cli_zero_environ_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    monkeypatch.setattr(_settings_mod, "os", _OsShim())
    config_path = _write_config(tmp_path)
    settings = _settings(tmp_path)
    receipt = _pass_receipt_for_seeded_stack(tmp_path, settings)
    code, payload = _run_final(
        ["--config", str(config_path), "--final-preflight-activation-candidate", "--json"],
        receipt,
        capsys,
    )
    assert code == 0
    assert payload["outcome"] == "PASS"
