"""RTM-7c.4a — operator paper fast-loop CLI tests.

No network, no credentials, no production runtime DB. ``--run`` is refused before any
side effect. ``--replay`` uses an OS temp dir, never the configured runtime/ paths.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config.settings as _settings_mod
from composition.paper_fast_loop import PaperFastLoopPaths
from config.settings import RuntimePaperFastLoopSettings
from decision.canonical_json import payload_sha256

# ops/ is not a package; load the CLI module by path.
_CLI_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_paper_fast_loop.py"
_spec = importlib.util.spec_from_file_location("run_paper_fast_loop", _CLI_PATH)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

_ALLOCATOR_JSON = None  # built lazily via composition test helper import


def _write_config(tmp_path: Path, *, enabled: bool = True, symbol: str = "005930") -> Path:
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


def _write_snapshot(tmp_path: Path, settings: RuntimePaperFastLoopSettings) -> None:
    # composition 테스트 헬퍼의 snapshot payload를 재사용하되, CLI는 실제 시계를
    # 사용하므로 created_at/expires_at를 현재 시각 기준으로 덮어써 유효 구간에 둔다.
    import json as _json
    from datetime import UTC, datetime, timedelta

    import test_paper_fast_loop_composition as helper

    now = datetime.now(tz=UTC)
    allocator_decision = helper._allocator().model_dump(mode="json")
    allocator_decision["created_at"] = (now - timedelta(hours=2)).isoformat()
    payload = helper._snapshot_payload(
        created_at=(now - timedelta(hours=1)).isoformat(),
        expires_at=(now + timedelta(days=1)).isoformat(),
        allocator_decision=allocator_decision,
    )
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    paths.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    paths.snapshot_path.write_text(_json.dumps(payload), encoding="utf-8")


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return code, payload


def test_run_is_refused_before_side_effects(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run(["--run", "--json"], capsys)
    assert code == 2
    assert payload["outcome"] == "NO_GO"
    assert payload["reason_code"] == "live_run_not_implemented"
    assert payload["credential_read"] is False
    assert payload["network_called"] is False
    assert payload["production_db_touched"] is False
    assert payload["filesystem_written"] is False


def test_mutually_exclusive_modes_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run(["--validate-only", "--inspect-existing", "--json"], capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert "mutually exclusive" in payload["reason_code"]


def test_default_config_example_validate_only(capsys: pytest.CaptureFixture[str]) -> None:
    # 기본 config.toml.example은 snapshot이 없으므로 NOT_READY/FAIL이지만 안전하게 동작한다.
    code, payload = _run(["--json"], capsys)
    assert payload["mode"] == "validate-only"
    assert payload["outcome"] == "FAIL"
    assert "snapshot_file_missing" in payload["reasons"]
    assert code == 1


def test_validate_only_pass_with_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    config_path = _write_config(tmp_path)
    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    _write_snapshot(tmp_path, settings)
    code, payload = _run(["--config", str(config_path), "--json"], capsys)
    assert code == 0
    assert payload["outcome"] == "PASS"
    assert payload["plan_outcome"] == "ready"
    assert payload["network_called"] is False


def test_inspect_existing_reports_missing_dbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)
    code, payload = _run(["--config", str(config_path), "--inspect-existing", "--json"], capsys)
    # B2: DB가 전부 없으면 fail-closed — outcome NO_GO + nonzero exit (이전 fail-open과 반대).
    assert code == 1
    assert payload["mode"] == "inspect-existing"
    assert payload["outcome"] == "NO_GO"
    assert payload["inspection_outcome"] == "no_go"
    assert "missing_database:ledger" in payload["reasons"]
    assert set(payload["missing_databases"]) == {"ledger", "trigger_journal", "active_decision_store"}


def test_replay_buy_fill_uses_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)
    code, payload = _run(["--config", str(config_path), "--replay", "buy_fill", "--json"], capsys)
    assert code == 0
    assert payload["mode"] == "replay"
    assert payload["committed_count"] == 1
    assert payload["final_position_quantity"] == "57"
    assert payload["runtime_paths_touched"] is False
    # CLI는 runtime/ 경로를 생성하지 않는다.
    assert not (tmp_path / "runtime").exists()


def test_replay_unknown_fixture_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)
    code, payload = _run(["--config", str(config_path), "--replay", "nope", "--json"], capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"


# --- RTM-7c.4b CLI exit-code + sanitization (Section 7.5) ---

from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo as _ZoneInfo  # noqa: E402


class _FixedClock:
    """Pins ``datetime.now(tz=...)`` to a point inside the seeded validity windows
    (2026-06-16 09:30 KST == 00:30 UTC == helper._NOW), so a seeded stack is OK."""

    @staticmethod
    def now(tz: object = None) -> _dt:
        return _dt(2026, 6, 16, 9, 30, tzinfo=_ZoneInfo("Asia/Seoul"))


def _seed_valid_via_helper(tmp_path: Path, settings: RuntimePaperFastLoopSettings) -> None:
    import test_paper_fast_loop_composition as helper

    helper._seed_valid_stack(tmp_path, settings)


def test_inspect_existing_ok_when_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    config_path = _write_config(tmp_path)
    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    _seed_valid_via_helper(tmp_path, settings)
    code, payload = _run(["--config", str(config_path), "--inspect-existing", "--json"], capsys)
    assert code == 0
    assert payload["outcome"] == "PASS"
    assert payload["inspection_outcome"] == "ok"
    assert payload["reasons"] == []
    assert payload["network_called"] is False


def test_inspect_existing_non_quiescent_is_fail_closed_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    config_path = _write_config(tmp_path)
    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    _seed_valid_via_helper(tmp_path, settings)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    wal = paths.active_decision_store_path.with_name(
        paths.active_decision_store_path.name + "-wal"
    )
    wal.write_bytes(b"")
    code = cli.main(["--config", str(config_path), "--inspect-existing", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert "database_not_quiescent:active_decision_store" in payload["reasons"]
    # 출력에는 traceback/raw sqlite 예외 텍스트가 없다(파일 경로의 .sqlite3 확장자는 허용).
    assert "Traceback" not in out
    assert "OperationalError" not in out
    assert "sqlite3.Error" not in out


def test_validate_only_opens_no_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    config_path = _write_config(tmp_path)
    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    _write_snapshot(tmp_path, settings)
    code, payload = _run(["--config", str(config_path), "--validate-only", "--json"], capsys)
    assert code == 0
    assert payload["outcome"] == "PASS"
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    # validate-only는 어떤 DB도 열거나 만들지 않는다.
    assert not paths.ledger_path.exists()
    assert not paths.trigger_journal_path.exists()
    assert not paths.active_decision_store_path.exists()


# --- RTM-7c.4c precheck-runtime CLI (machine PASS ≠ activation authorization) ---


def test_precheck_runtime_pass_when_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    config_path = _write_config(tmp_path)
    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    _seed_valid_via_helper(tmp_path, settings)
    code, payload = _run(["--config", str(config_path), "--precheck-runtime", "--json"], capsys)
    assert code == 0
    assert payload["mode"] == "precheck-runtime"
    assert payload["outcome"] == "PASS"
    assert payload["machine_check_outcome"] == "pass"
    # Machine PASS NEVER authorizes activation — the activation fields are hard constants.
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"
    assert payload["explicit_operator_approval_required"] is True
    assert payload["writers_stopped_manual_confirmation_required"] is True
    assert payload["reasons"] == []
    # Read-only / no-side-effect attestations.
    assert payload["network_called"] is False
    assert payload["credential_read"] is False
    assert payload["broker_called"] is False
    assert payload["production_db_written"] is False
    assert payload["runtime_file_created"] is False
    receipt = payload["precheck_receipt"]
    assert receipt["machine_outcome"] == "pass"
    assert receipt["activation_authorized"] is False
    assert receipt["runtime_activation_outcome"] == "no_go"
    assert receipt["receipt_sha256"]
    assert len(receipt["receipt_sha256"]) == 64
    assert payload["activation_authorized"] == receipt["activation_authorized"]


def test_precheck_runtime_no_go_when_missing_dbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)
    code, payload = _run(["--config", str(config_path), "--precheck-runtime", "--json"], capsys)
    assert code == 1
    assert payload["mode"] == "precheck-runtime"
    assert payload["outcome"] == "NO_GO"
    assert payload["machine_check_outcome"] == "no_go"
    assert payload["activation_authorized"] is False
    assert "missing_database:ledger" in payload["reasons"]
    assert set(payload["missing_databases"]) == {
        "ledger",
        "trigger_journal",
        "active_decision_store",
    }


def test_precheck_runtime_mutually_exclusive_with_inspect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload = _run(["--precheck-runtime", "--inspect-existing", "--json"], capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert "mutually exclusive" in payload["reason_code"]


def test_precheck_runtime_non_quiescent_is_fail_closed_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    config_path = _write_config(tmp_path)
    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    _seed_valid_via_helper(tmp_path, settings)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    wal = paths.active_decision_store_path.with_name(
        paths.active_decision_store_path.name + "-wal"
    )
    wal.write_bytes(b"")
    code = cli.main(["--config", str(config_path), "--precheck-runtime", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert code == 1
    assert payload["outcome"] == "NO_GO"
    assert payload["activation_authorized"] is False
    assert "database_not_quiescent:active_decision_store" in payload["reasons"]
    assert "Traceback" not in out
    assert "OperationalError" not in out
    assert "sqlite3.Error" not in out


# --- RTM-7c.4c safety closure: precheck reads NO os.environ (real access spy) ---


class _NoEnvironAccess:
    """A mapping stand-in for ``os.environ`` that fails on *any* access.

    Proves an empirical property, not a printed boolean: if `--precheck-runtime`
    touches `os.environ` through config loading at all (substitution OR the live
    confirmation/credential gates), one of these raises and the test fails."""

    _MSG = "precheck must not read os.environ"

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
    """Proxies every attribute to the real ``os`` except ``environ``, which is the
    fail-on-access spy. Patched onto ``config.settings.os`` so ONLY config loading
    sees the spy — pytest's own ``os.environ`` use is unaffected."""

    environ = _NoEnvironAccess()

    def __getattr__(self, name: str) -> object:
        return getattr(_real_os, name)


def _patch_settings_environ_spy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings_mod, "os", _OsShim())


def _write_live_config(tmp_path: Path) -> Path:
    # Live-mode gate mismatch: RuntimeGateError without any ${ENV} or credential env read.
    config_path = tmp_path / "config_live.toml"
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
    return config_path


def test_precheck_runtime_makes_zero_environ_access_on_normal_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Normal seeded config (no ${...}, paper mode) → precheck runs to a real machine verdict
    # WITHOUT touching os.environ. The spy raises on any access, so reaching PASS proves
    # credential/env read is 0 through the whole config-loading + precheck path.
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    _patch_settings_environ_spy(monkeypatch)
    config_path = _write_config(tmp_path)
    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    _seed_valid_via_helper(tmp_path, settings)
    code, payload = _run(["--config", str(config_path), "--precheck-runtime", "--json"], capsys)
    assert code == 0
    assert payload["outcome"] == "PASS"
    assert payload["credential_read"] is False


def test_precheck_runtime_env_placeholder_fails_closed_without_environ_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A secret env reference in config must NOT be resolved: precheck fails closed with a
    # sanitized config error and never reads os.environ (the spy would raise otherwise).
    monkeypatch.chdir(tmp_path)
    _patch_settings_environ_spy(monkeypatch)
    config_path = tmp_path / "config_secret.toml"
    config_path.write_text(
        """
[trading]
mode = "paper"

[runtime.paper_fast_loop]
enabled = true
market = "KR"
symbol = "${KIS_LIVE_APP_KEY}"
""",
        encoding="utf-8",
    )
    code, payload = _run(["--config", str(config_path), "--precheck-runtime", "--json"], capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reason_code"] == "config error: ConfigEnvironmentError"
    # No env var name, no value, no raw config, no traceback in the sanitized output.
    assert "KIS_LIVE_APP_KEY" not in json.dumps(payload)


def test_precheck_runtime_live_config_fails_closed_without_environ_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Live-mode runtime gate mismatch must fail closed under precheck without reading
    # os.environ — no credential resolution, no confirmation phrase lookup.
    monkeypatch.chdir(tmp_path)
    _patch_settings_environ_spy(monkeypatch)
    config_path = _write_live_config(tmp_path)
    code = cli.main(["--config", str(config_path), "--precheck-runtime", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    combined = captured.out + captured.err
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert payload["reason_code"] == "config error: RuntimeGateError"
    assert "KIS" not in combined
    assert "APP_KEY" not in combined
    assert "APP_SECRET" not in combined
    assert "Traceback" not in combined
    assert "RuntimeGateError(" not in combined
    assert str(config_path) not in json.dumps(payload)
    assert "allow_live_trading" not in json.dumps(payload)


# --- RTM-7c.4e verify-precheck-receipt CLI ---


def _stdin_bytes(data: bytes) -> object:
    class _Stdin:
        buffer = io.BytesIO(data)

    return _Stdin()


def _run_verify(data: bytes, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    sys.stdin = _stdin_bytes(data)  # type: ignore[assignment]
    code = cli.main(["--verify-precheck-receipt", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return code, payload


def _assert_emitted_receipt_hash_matches_nested(receipt: dict[str, object]) -> None:
    stored = receipt["receipt_sha256"]
    assert isinstance(stored, str)
    body = dict(receipt)
    del body["receipt_sha256"]
    assert payload_sha256(body) == stored


def test_emitted_precheck_receipt_hash_recomputes_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    config_path = _write_config(tmp_path)
    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    _seed_valid_via_helper(tmp_path, settings)
    code, payload = _run(["--config", str(config_path), "--precheck-runtime", "--json"], capsys)
    assert code == 0
    receipt = payload["precheck_receipt"]
    _assert_emitted_receipt_hash_matches_nested(receipt)
    serialized = json.dumps(receipt)
    repo = str(Path(__file__).resolve().parents[1])
    assert repo not in serialized
    assert "KIS_" not in serialized
    assert "APP_KEY" not in serialized
    assert "APP_SECRET" not in serialized
    assert "Traceback" not in serialized


def test_emitted_precheck_receipt_hash_recomputes_no_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)
    code, payload = _run(["--config", str(config_path), "--precheck-runtime", "--json"], capsys)
    assert code == 1
    receipt = payload["precheck_receipt"]
    _assert_emitted_receipt_hash_matches_nested(receipt)


def test_verify_cli_accepts_emitted_pass_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    config_path = _write_config(tmp_path)
    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    _seed_valid_via_helper(tmp_path, settings)
    _, precheck_payload = _run(["--config", str(config_path), "--precheck-runtime", "--json"], capsys)
    receipt = precheck_payload["precheck_receipt"]
    code, verify_payload = _run_verify(json.dumps(receipt).encode("utf-8"), capsys)
    assert code == 0
    assert verify_payload["outcome"] == "VALID"
    assert verify_payload["mode"] == "verify-precheck-receipt"
    assert verify_payload["activation_authorized"] is False
    assert verify_payload["receipt_sha256"] == receipt["receipt_sha256"]


def test_verify_cli_accepts_emitted_no_go_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _, precheck_payload = _run(
        ["--config", str(_write_config(tmp_path)), "--precheck-runtime", "--json"], capsys
    )
    receipt = precheck_payload["precheck_receipt"]
    code, verify_payload = _run_verify(json.dumps(receipt).encode("utf-8"), capsys)
    assert code == 0
    assert verify_payload["outcome"] == "VALID"


def test_verify_cli_rejects_tampered_receipt(capsys: pytest.CaptureFixture[str]) -> None:
    import test_precheck_receipt_verifier as vrf_helper

    receipt = vrf_helper._valid_receipt()
    receipt["checked_at"] = "2026-06-16T01:00:00+00:00"
    code, payload = _run_verify(json.dumps(receipt).encode("utf-8"), capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["receipt_hash_mismatch"]


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "receipt_input_empty"),
        (b"\xff\xfe", "receipt_input_not_utf8"),
        (b"{not json", "receipt_input_not_json"),
    ],
)
def test_verify_cli_input_failures(data: bytes, reason: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run_verify(data, capsys)
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == [reason]
    assert payload["activation_authorized"] is False


def test_verify_cli_rejects_oversized_stdin(capsys: pytest.CaptureFixture[str]) -> None:
    limit = cli._VERIFY_RECEIPT_STDIN_LIMIT
    code, payload = _run_verify(b"x" * (limit + 1), capsys)
    assert code == 1
    assert payload["reason_codes"] == ["receipt_input_too_large"]


def _receipt_json_padded_to_exact_limit() -> bytes:
    import test_precheck_receipt_verifier as vrf_helper

    limit = cli._VERIFY_RECEIPT_STDIN_LIMIT
    text = json.dumps(vrf_helper._valid_receipt())
    if len(text.encode("utf-8")) > limit:
        raise AssertionError("valid receipt baseline exceeds stdin limit")
    pad = limit - len(text.encode("utf-8"))
    data = (text + (" " * pad)).encode("utf-8")
    assert len(data) == limit
    return data


def test_verify_cli_accepts_stdin_at_exact_limit(capsys: pytest.CaptureFixture[str]) -> None:
    data = _receipt_json_padded_to_exact_limit()
    code, payload = _run_verify(data, capsys)
    assert code == 0
    assert payload["outcome"] == "VALID"


def test_verify_stdin_reader_requests_limit_plus_one_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requested: list[int] = []

    def _read(size: int = -1) -> bytes:
        requested.append(size)
        return b"x" * (cli._VERIFY_RECEIPT_STDIN_LIMIT + 1)

    class _Stdin:
        buffer = type("_B", (), {"read": staticmethod(_read)})()

    monkeypatch.setattr(sys, "stdin", _Stdin())
    code = cli.main(["--verify-precheck-receipt", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 1
    assert payload["reason_codes"] == ["receipt_input_too_large"]
    assert requested == [cli._VERIFY_RECEIPT_STDIN_LIMIT + 1]


def test_verify_cli_mutually_exclusive_with_precheck(capsys: pytest.CaptureFixture[str]) -> None:
    code, payload = _run(["--verify-precheck-receipt", "--precheck-runtime", "--json"], capsys)
    assert code == 1
    assert payload["outcome"] == "FAIL"
    assert "mutually exclusive" in payload["reason_code"]


def test_verify_cli_makes_zero_load_settings_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import test_precheck_receipt_verifier as vrf_helper

    calls: list[str] = []

    def _spy(*_a: object, **_k: object) -> object:
        calls.append("load_settings")
        raise AssertionError("verify must not load settings")

    monkeypatch.setattr(cli, "load_settings", _spy)
    _run_verify(json.dumps(vrf_helper._valid_receipt()).encode("utf-8"), capsys)
    assert calls == []


def test_verify_cli_makes_zero_environ_access(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import test_precheck_receipt_verifier as vrf_helper

    _patch_settings_environ_spy(monkeypatch)
    _run_verify(json.dumps(vrf_helper._valid_receipt()).encode("utf-8"), capsys)


def test_verify_cli_sanitizes_poison_input(capsys: pytest.CaptureFixture[str]) -> None:
    poison = {
        "schema_version": 1,
        "checked_at": "2026-06-16T00:30:00+00:00",
        "market": "KR",
        "symbol": "005930",
        "enabled": True,
        "machine_outcome": "pass",
        "inspection_outcome": "ok",
        "reasons": [],
        "fingerprints_before": [],
        "fingerprints_after": [],
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
        "explicit_operator_approval_required": True,
        "writers_stopped_manual_confirmation_required": True,
        "receipt_sha256": "ab" * 32,
        "KIS_LIVE_APP_KEY": "/home/secret",
    }
    code, payload = _run_verify(json.dumps(poison).encode("utf-8"), capsys)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert "KIS_" not in combined
    assert "/home/" not in combined
    assert "APP_SECRET" not in combined
    assert "Traceback" not in combined


def _assert_verify_invalid_sanitized(
    data: bytes, capsys: pytest.CaptureFixture[str], *, reason: str
) -> None:
    code, payload = _run_verify(data, capsys)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert code == 1
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == [reason]
    assert payload["activation_authorized"] is False
    assert "Traceback" not in combined
    assert "ValueError" not in combined
    assert "RecursionError" not in combined


def test_verify_cli_rejects_large_integer_json(capsys: pytest.CaptureFixture[str]) -> None:
    data = (b'{"schema_version": ' + b"9" * 5000 + b"}")
    assert len(data) < cli._VERIFY_RECEIPT_STDIN_LIMIT
    _assert_verify_invalid_sanitized(data, capsys, reason="receipt_input_not_json")


def test_verify_cli_rejects_deeply_nested_json(capsys: pytest.CaptureFixture[str]) -> None:
    depth = 5000
    data = b"[" * depth + b"0" + b"]" * depth
    assert len(data) < cli._VERIFY_RECEIPT_STDIN_LIMIT
    _assert_verify_invalid_sanitized(data, capsys, reason="receipt_input_too_deep")


def test_verify_cli_rejects_duplicate_top_level_key(capsys: pytest.CaptureFixture[str]) -> None:
    data = b'{"schema_version": 1, "schema_version": 2}'
    _assert_verify_invalid_sanitized(data, capsys, reason="receipt_input_duplicate_key")


def test_verify_cli_rejects_duplicate_nested_fingerprint_key(capsys: pytest.CaptureFixture[str]) -> None:
    data = b'{"name": "ledger", "name": "ledger", "present": true}'
    _assert_verify_invalid_sanitized(data, capsys, reason="receipt_input_duplicate_key")


def test_verify_cli_rejects_nan_constant(capsys: pytest.CaptureFixture[str]) -> None:
    _assert_verify_invalid_sanitized(b"[NaN]", capsys, reason="receipt_input_not_json")


def test_verify_cli_rejects_infinity_constant(capsys: pytest.CaptureFixture[str]) -> None:
    _assert_verify_invalid_sanitized(b"[Infinity]", capsys, reason="receipt_input_not_json")


def test_verify_cli_subprocess_no_traceback_on_pathological_json() -> None:
    import os
    import subprocess

    data = b'{"schema_version": ' + b"9" * 5000 + b"}"
    result = subprocess.run(
        [sys.executable, str(_CLI_PATH), "--verify-precheck-receipt", "--json"],
        input=data,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert result.returncode == 1
    assert b"Traceback" not in result.stderr
    assert b"ValueError" not in result.stderr
    assert b"Traceback" not in result.stdout
    payload = json.loads(result.stdout.decode("utf-8").strip().splitlines()[-1])
    assert payload["outcome"] == "INVALID"
    assert payload["reason_codes"] == ["receipt_input_not_json"]
