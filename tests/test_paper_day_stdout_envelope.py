"""RTM-7c.12 — stdout-envelope capture hardening.

The persisted ``summary.json`` holds only the mechanical summary; the five
clean-exit clauses live only in the run's stdout ``--json`` payload. When the
envelope is not captured to disk, an otherwise-clean run cannot be verified to
PASS offline (the validator reports ``NEEDS_REVIEW`` /
``missing_from_persisted_summary``). These tests cover the tool-written envelope:
it is written on PASS and FAIL, excludes secret-like values, and round-trips
through the offline validator to PASS — while a missing envelope still yields the
explicit gap.

Fully offline: no network, no live KIS, no secrets printed, no raw frames.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "ops"))

from validate_paper_day_summary import (  # noqa: E402
    EXPECTED_SCHEMA_VERSION,
    NEEDS_REVIEW,
    PASS,
    ValidatorError,
    run_validate,
)

_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "paper_day_reports"

_CLI_PATH = REPO_ROOT / "ops" / "run_attended_paper_day.py"
_spec = importlib.util.spec_from_file_location("run_attended_paper_day", _CLI_PATH)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def _mechanical_summary(**overrides: object) -> dict[str, object]:
    """The persisted summary.json subset (no clean-exit clauses)."""
    base: dict[str, object] = {
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "run_id": "paper-day-005930-260626",
        "session_date": "2026-06-26",
        "market": "KR",
        "symbol": "005930",
        "paper_only": True,
        "activation_authorized": False,
        "real_order_adapter_constructed": False,
        "automatic_restart": False,
        "multi_symbol": False,
        "outcome": "PASS",
        "stop_reason": "completed",
        "nonterminal_journal": 0,
        "counters": {"counters": {}, "reason_counts": {}, "timestamps": {}},
        "source_kind": "kis_live",
    }
    base.update(overrides)
    return base


def _full_pass_payload(**overrides: object) -> dict[str, object]:
    """The full stdout --json payload: mechanical summary + clean-exit clauses."""
    mechanical = _mechanical_summary()
    payload: dict[str, object] = {
        **mechanical,
        "persisted_summary": dict(mechanical),
        "summary_publication_outcome": "WRITTEN",
        "summary_publication_reason_codes": [],
        "runtime_lock_fd_closed": True,
        "runtime_lock_unlinked": True,
        "runtime_lock_absent_confirmed": True,
        "runtime_lock_identity_matched": True,
        "runtime_lock_release_reason_code": None,
        "cleanup_outcome": "CLEAN",
    }
    payload.update(overrides)
    return payload


def _full_fail_payload() -> dict[str, object]:
    return _full_pass_payload(
        outcome="FAIL",
        stop_reason="resource_close_failure",
        cleanup_outcome="INCOMPLETE",
    )


def _benign_evidence(path: Path) -> None:
    rows = [
        {
            "recorded_at": "2026-06-26T12:30:00+09:00",
            "stage": "startup",
            "event": "subscriptions_ready",
            "reason_code": None,
            "sensitive_data_present": False,
        },
        {
            "recorded_at": "2026-06-26T18:00:00+09:00",
            "stage": "shutdown",
            "event": "finalized",
            "reason_code": "completed",
            "sensitive_data_present": False,
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _build_envelope(payload: dict[str, object], *, exit_code: int) -> dict[str, object]:
    return cli.build_stdout_envelope(
        summary=payload,
        exit_code=exit_code,
        summary_path=Path("runtime/paper-day/2026-06-26/pilot/summary.json"),
        evidence_path=Path("runtime/paper-day/2026-06-26/pilot/evidence.jsonl"),
        db_dir=Path("runtime/paper-day/2026-06-26/pilot/db"),
        command_args=["--symbol", "005930", "--duration-seconds", "100"],
        captured_at="2026-06-26T09:53:59+00:00",
        git_head="0123456789abcdef0123456789abcdef01234567",
    )


# --- envelope written on PASS / FAIL ----------------------------------------


def test_envelope_written_on_pass(tmp_path: Path) -> None:
    out = tmp_path / "run" / "stdout-envelope.json"
    envelope = _build_envelope(_full_pass_payload(), exit_code=0)
    cli.write_stdout_envelope(out, envelope)

    assert out.is_file()
    written = json.loads(out.read_text(encoding="utf-8"))

    cap = written["_envelope_capture"]
    assert cap["schema_version"] == cli.STDOUT_ENVELOPE_SCHEMA_VERSION
    assert cap["exit_code"] == 0
    assert cap["run_id"] == "paper-day-005930-260626"
    assert cap["summary_path"].endswith("summary.json")
    assert cap["evidence_path"].endswith("evidence.jsonl")
    assert cap["db_dir"].endswith("db")
    assert cap["captured_at"] == "2026-06-26T09:53:59+00:00"
    assert cap["git_head"] == "0123456789abcdef0123456789abcdef01234567"
    assert isinstance(cap["command_args"], list)

    # Clean-exit clauses stay at top level so validate/render --envelope read them.
    assert written["outcome"] == "PASS"
    assert written["summary_publication_outcome"] == "WRITTEN"
    assert written["cleanup_outcome"] == "CLEAN"
    assert written["runtime_lock_release_reason_code"] is None


def test_envelope_written_on_fail(tmp_path: Path) -> None:
    out = tmp_path / "run" / "stdout-envelope.json"
    envelope = _build_envelope(_full_fail_payload(), exit_code=1)
    cli.write_stdout_envelope(out, envelope)

    assert out.is_file()
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["_envelope_capture"]["exit_code"] == 1
    assert written["outcome"] == "FAIL"
    assert written["cleanup_outcome"] == "INCOMPLETE"


def test_write_is_atomic_no_leftover_temp(tmp_path: Path) -> None:
    out = tmp_path / "run" / "stdout-envelope.json"
    cli.write_stdout_envelope(out, _build_envelope(_full_pass_payload(), exit_code=0))
    assert out.is_file()
    assert not (out.parent / (out.name + ".tmp")).exists()


# --- secret / env exclusion -------------------------------------------------


def test_envelope_excludes_secret_like_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An env value must never reach the envelope: the builder reads no environment.
    monkeypatch.setenv("KIS_APP_SECRET", "envsecret_must_not_leak")

    command_args = [
        "--config",
        "config/config.toml",
        "--symbol",
        "005930",
        "--duration-seconds",
        "100",
        "KIS_APP_SECRET=topsecretvalue",
        "--app-key",
        "superappkeyvalue",
        "--approval-token=tok_abc123",
    ]
    envelope = cli.build_stdout_envelope(
        summary=_full_pass_payload(),
        exit_code=0,
        summary_path=Path("/x/summary.json"),
        evidence_path=Path("/x/evidence.jsonl"),
        db_dir=Path("/x/db"),
        command_args=command_args,
        captured_at="2026-06-26T09:53:59+00:00",
        git_head=None,
    )
    blob = json.dumps(envelope)

    # No secret value of any form survives.
    assert "topsecretvalue" not in blob
    assert "superappkeyvalue" not in blob
    assert "tok_abc123" not in blob
    assert "envsecret_must_not_leak" not in blob
    assert cli._REDACTED in blob

    # Non-secret args are preserved verbatim.
    sanitized = envelope["_envelope_capture"]["command_args"]
    assert "config/config.toml" in sanitized
    assert "005930" in sanitized
    assert "--duration-seconds" in sanitized
    assert "100" in sanitized
    assert "KIS_APP_SECRET=<redacted>" in sanitized
    assert "--approval-token=<redacted>" in sanitized
    # The `--app-key VALUE` pair: flag kept, value redacted.
    assert "--app-key" in sanitized
    assert sanitized[sanitized.index("--app-key") + 1] == cli._REDACTED


def test_git_head_returns_none_or_validated_sha() -> None:
    head = cli._git_head()
    assert head is None or (len(head) == 40 and all(c in "0123456789abcdef" for c in head))


# --- validator round-trip: missing envelope is the explicit gap --------------


def test_validator_needs_review_without_envelope_then_pass_with(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    evidence_path = tmp_path / "evidence.jsonl"
    envelope_path = tmp_path / "stdout-envelope.json"

    summary_path.write_text(json.dumps(_mechanical_summary()), encoding="utf-8")
    _benign_evidence(evidence_path)

    # Envelope missing → explicit gap, verdict NEEDS_REVIEW.
    result_missing = run_validate(
        summary_path=summary_path,
        evidence_path=evidence_path,
        envelope_path=None,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind="kis_live",
    )
    assert result_missing["verdict"] == NEEDS_REVIEW
    assert "missing_from_persisted_summary" in result_missing["pass_blockers"]

    # Tool-written envelope present → validator reaches PASS.
    cli.write_stdout_envelope(
        envelope_path, _build_envelope(_full_pass_payload(), exit_code=0)
    )
    result_present = run_validate(
        summary_path=summary_path,
        evidence_path=evidence_path,
        envelope_path=envelope_path,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind="kis_live",
    )
    assert result_present["verdict"] == PASS
    assert result_present["pass_blockers"] == []
    assert result_present["missing_from_persisted_summary"] == []


# --- CLI wiring: offline fixture writes the envelope -------------------------


def test_cli_offline_fixture_writes_envelope(tmp_path: Path, capsys) -> None:
    run_dir = tmp_path / "runtime" / "run"
    envelope_path = run_dir / "stdout-envelope.json"
    rc = cli.main(
        [
            "--config",
            "config/config.toml.example",
            "--session-date",
            "2026-06-17",
            "--symbol",
            "005930",
            "--duration-seconds",
            "2",
            "--evidence-out",
            str(run_dir / "evidence.jsonl"),
            "--summary-out",
            str(run_dir / "summary.json"),
            "--db-dir",
            str(run_dir / "db"),
            "--stdout-envelope-out",
            str(envelope_path),
            "--confirm-attended-paper",
            "--offline-fixture",
            "deterministic",
            "--json",
        ]
    )
    capsys.readouterr()

    assert envelope_path.is_file()
    written = json.loads(envelope_path.read_text(encoding="utf-8"))
    cap = written["_envelope_capture"]
    assert cap["exit_code"] == rc
    assert cap["schema_version"] == cli.STDOUT_ENVELOPE_SCHEMA_VERSION
    assert cap["run_id"]
    assert cap["summary_path"].endswith("summary.json")
    assert cap["evidence_path"].endswith("evidence.jsonl")
    assert cap["db_dir"].endswith("db")
    # The run actually produced the persisted artifacts.
    assert (run_dir / "summary.json").is_file()
    assert (run_dir / "evidence.jsonl").is_file()
    # The captured argv carries no secret marker (none were passed).
    assert "--stdout-envelope-out" in cap["command_args"]


# --- CLI FAIL paths also persist the envelope -------------------------------


def test_cli_input_error_path_writes_fail_envelope(tmp_path: Path, capsys) -> None:
    # A bad --session-date raises CliError inside _config_from_args, before any
    # config/source/credential work. The FAIL envelope must still be written.
    run_dir = tmp_path / "runtime" / "run"
    envelope_path = run_dir / "stdout-envelope.json"
    rc = cli.main(
        [
            "--config",
            "config/config.toml.example",
            "--session-date",
            "not-a-date",
            "--symbol",
            "005930",
            "--duration-seconds",
            "2",
            "--evidence-out",
            str(run_dir / "evidence.jsonl"),
            "--summary-out",
            str(run_dir / "summary.json"),
            "--db-dir",
            str(run_dir / "db"),
            "--stdout-envelope-out",
            str(envelope_path),
            "--confirm-attended-paper",
            "--offline-fixture",
            "deterministic",
            "--json",
        ]
    )
    capsys.readouterr()

    assert rc == 1
    assert envelope_path.is_file()
    written = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert written["outcome"] == "FAIL"
    cap = written["_envelope_capture"]
    assert cap["exit_code"] == 1
    assert cap["schema_version"] == cli.STDOUT_ENVELOPE_SCHEMA_VERSION
    assert cap["summary_path"].endswith("summary.json")
    assert cap["evidence_path"].endswith("evidence.jsonl")
    assert cap["db_dir"].endswith("db")


def test_cli_generic_exception_path_writes_fail_envelope_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # Force an unexpected error from the runtime entrypoint; the generic except must
    # persist a FAIL envelope without leaking the exception text/traceback.
    secret_marker = "boom_secret_/private/path/token"

    def _explode(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError(secret_marker)

    monkeypatch.setattr(cli, "run_attended_paper_day", _explode)

    run_dir = tmp_path / "runtime" / "run"
    envelope_path = run_dir / "stdout-envelope.json"
    rc = cli.main(
        [
            "--config",
            "config/config.toml.example",
            "--session-date",
            "2026-06-17",
            "--symbol",
            "005930",
            "--duration-seconds",
            "2",
            "--evidence-out",
            str(run_dir / "evidence.jsonl"),
            "--summary-out",
            str(run_dir / "summary.json"),
            "--db-dir",
            str(run_dir / "db"),
            "--stdout-envelope-out",
            str(envelope_path),
            "--confirm-attended-paper",
            "--offline-fixture",
            "deterministic",
            "--json",
        ]
    )
    capsys.readouterr()

    assert rc == 1
    assert envelope_path.is_file()
    raw = envelope_path.read_text(encoding="utf-8")
    written = json.loads(raw)
    assert written["outcome"] == "FAIL"
    assert written["reason"] == "internal_runtime_error"
    assert written["_envelope_capture"]["exit_code"] == 1
    # No traceback or raised-exception text leaks into the persisted envelope.
    assert secret_marker not in raw
    assert "Traceback" not in raw


# --- envelope edge cases must never be misclassified as PASS -----------------


def _validate_fixture(name: str, **overrides: object) -> dict[str, object]:
    fixture = _FIXTURE_ROOT / name
    envelope = fixture / "stdout-envelope.json"
    kwargs: dict[str, object] = {
        "summary_path": fixture / "summary.json",
        "evidence_path": fixture / "evidence.jsonl",
        "envelope_path": envelope if envelope.is_file() else None,
        "expect_schema_version": EXPECTED_SCHEMA_VERSION,
        "expect_source_kind": "kis_live",
    }
    kwargs.update(overrides)
    return run_validate(**kwargs)  # type: ignore[arg-type]


def test_validator_rejects_malformed_envelope_as_cli_error() -> None:
    # run_validate surfaces a malformed --envelope as a CLI-level ValidatorError
    # (NEEDS_REVIEW at the CLI boundary) — it never silently infers the clauses
    # and never reaches PASS.
    with pytest.raises(ValidatorError):
        _validate_fixture("needs_review_malformed_envelope")


def test_validator_wrong_run_envelope_is_needs_review() -> None:
    result = _validate_fixture("needs_review_wrong_run_envelope")
    assert result["verdict"] == NEEDS_REVIEW
    assert result["verdict"] != PASS
    assert "envelope_run_mismatch" in result["pass_blockers"]


def test_validator_contradictory_envelope_is_needs_review() -> None:
    result = _validate_fixture("needs_review_contradictory_envelope")
    assert result["verdict"] == NEEDS_REVIEW
    assert result["verdict"] != PASS
    # Same-run identity, so the mismatch guard must NOT fire here.
    assert "envelope_run_mismatch" not in result["pass_blockers"]
    # The FAIL-like clean-exit clauses are what block PASS.
    assert "summary_publication_outcome" in result["pass_blockers"]
    assert "cleanup_outcome" in result["pass_blockers"]


def test_validator_matching_identity_envelope_does_not_misfire(tmp_path: Path) -> None:
    # A correct, same-run envelope must still PASS — the identity guard adds an
    # "ok" check, never a spurious blocker.
    summary_path = tmp_path / "summary.json"
    evidence_path = tmp_path / "evidence.jsonl"
    envelope_path = tmp_path / "stdout-envelope.json"
    summary_path.write_text(json.dumps(_mechanical_summary()), encoding="utf-8")
    _benign_evidence(evidence_path)
    cli.write_stdout_envelope(envelope_path, _build_envelope(_full_pass_payload(), exit_code=0))

    result = run_validate(
        summary_path=summary_path,
        evidence_path=evidence_path,
        envelope_path=envelope_path,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind="kis_live",
    )
    assert result["verdict"] == PASS
    assert "envelope_run_mismatch" not in result["pass_blockers"]


def test_cli_fail_without_envelope_flag_writes_nothing(tmp_path: Path, capsys) -> None:
    # Without --stdout-envelope-out, a FAIL path must not create any envelope file.
    run_dir = tmp_path / "runtime" / "run"
    rc = cli.main(
        [
            "--config",
            "config/config.toml.example",
            "--session-date",
            "not-a-date",
            "--symbol",
            "005930",
            "--duration-seconds",
            "2",
            "--evidence-out",
            str(run_dir / "evidence.jsonl"),
            "--summary-out",
            str(run_dir / "summary.json"),
            "--db-dir",
            str(run_dir / "db"),
            "--confirm-attended-paper",
            "--offline-fixture",
            "deterministic",
            "--json",
        ]
    )
    capsys.readouterr()
    assert rc == 1
    assert not (run_dir / "stdout-envelope.json").exists()
