"""RTM-7c.4f — attended one-shot activation contract (design freeze; NO-GO only).

실제 activation caller, KIS, daemon, 주문, approval 입력/저장 없음.
network/credential/운영 DB 미사용.
"""

from __future__ import annotations

import importlib.util
import io
import json
import socket
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.attended_activation import AttendedActivationStage
from composition.paper_fast_loop import precheck_runtime
from composition.precheck_receipt_verifier import verify_runtime_precheck_receipt_payload
from config.settings import RuntimePaperFastLoopSettings

_CLI_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_paper_fast_loop.py"
_spec = importlib.util.spec_from_file_location("run_paper_fast_loop", _CLI_PATH)
assert _spec is not None and _spec.loader is not None
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_cli_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    return code, payload


def _stdin_bytes(data: bytes) -> object:
    return type("_Stdin", (), {"buffer": io.BytesIO(data)})()


class _FixedClock:
    @staticmethod
    def now(tz: object = None) -> object:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime(2026, 6, 16, 9, 30, tzinfo=ZoneInfo("Asia/Seoul"))


def test_attended_activation_stage_enum_values() -> None:
    assert AttendedActivationStage.DISABLED.value == "disabled"
    assert AttendedActivationStage.ACTIVATION_NOT_IMPLEMENTED.value == "activation_not_implemented"
    assert len(AttendedActivationStage) == 6


def test_run_refused_before_load_settings_and_config_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import config.settings as settings_mod

    def _load_boom(*_a: object, **_k: object) -> object:
        raise AssertionError("load_settings must not run for --run")

    class _NoEnvironAccess:
        _MSG = "config env must not be read for --run"

        def __getitem__(self, key: object) -> str:
            raise AssertionError(f"{self._MSG} (__getitem__ {key!r})")

        def get(self, key: object, default: object = None) -> object:
            raise AssertionError(f"{self._MSG} (get {key!r})")

    class _SettingsOsShim:
        environ = _NoEnvironAccess()

        def __getattr__(self, name: str) -> object:
            import os as real_os

            return getattr(real_os, name)

    monkeypatch.setattr(cli, "load_settings", _load_boom)
    monkeypatch.setattr(settings_mod, "os", _SettingsOsShim())
    code, payload = _load_cli_json(["--run", "--json"], capsys)
    assert code == 2
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"


def test_run_makes_zero_sqlite_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _spy_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise AssertionError("sqlite3.connect must not run for --run")

    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    code, _ = _load_cli_json(["--run", "--json"], capsys)
    assert code == 2


def test_run_makes_zero_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _spy(*_a: object, **_k: object) -> object:
        raise AssertionError("network must not be used for --run")

    monkeypatch.setattr(socket, "create_connection", _spy)
    code, payload = _load_cli_json(["--run", "--json"], capsys)
    assert code == 2
    assert payload["network_called"] is False


def test_run_makes_zero_thread_or_subprocess_spawn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _thread_boom(*_a: object, **_k: object) -> threading.Thread:
        raise AssertionError("thread must not start for --run")

    def _popen_boom(*_a: object, **_k: object) -> subprocess.Popen[str]:
        raise AssertionError("subprocess must not start for --run")

    monkeypatch.setattr(threading, "Thread", _thread_boom)
    monkeypatch.setattr(subprocess, "Popen", _popen_boom)
    code, _ = _load_cli_json(["--run", "--json"], capsys)
    assert code == 2


def test_precheck_pass_never_authorizes_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import test_paper_fast_loop_composition as helper

    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    monkeypatch.setattr(cli, "datetime", _FixedClock)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[runtime.paper_fast_loop]
enabled = true
market = "KR"
symbol = "005930"
""",
        encoding="utf-8",
    )
    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    helper._seed_valid_stack(tmp_path, settings)
    code, payload = _load_cli_json(
        ["--config", str(config_path), "--precheck-runtime", "--json"], capsys
    )
    assert code == 0
    assert payload["outcome"] == "PASS"
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"


def test_verifier_valid_never_authorizes_activation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import test_precheck_receipt_verifier as vrf_helper

    receipt = vrf_helper._valid_receipt()
    assert verify_runtime_precheck_receipt_payload(receipt).outcome.value == "valid"
    monkeypatch.setattr(sys, "stdin", _stdin_bytes(json.dumps(receipt).encode("utf-8")))
    code = cli.main(["--verify-precheck-receipt", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert code == 0
    assert payload["outcome"] == "VALID"
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"


def test_valid_receipt_hash_does_not_consume_approval() -> None:
    import test_precheck_receipt_verifier as vrf_helper

    receipt = vrf_helper._valid_receipt()
    result = verify_runtime_precheck_receipt_payload(receipt)
    assert result.outcome.value == "valid"
    # approval 입력/소비 API가 없음 — receipt VALID 후에도 activation posture 불변
    assert receipt["activation_authorized"] is False
    assert receipt["runtime_activation_outcome"] == "no_go"
    assert receipt["explicit_operator_approval_required"] is True


@pytest.mark.parametrize(
    "argv,stdin_data",
    [
        (["--run", "--json"], None),
        (["--verify-precheck-receipt", "--json"], b""),
    ],
)
def test_activation_authorized_always_false_on_refusal_paths(
    argv: list[str],
    stdin_data: bytes | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if stdin_data is not None:
        monkeypatch.setattr(sys, "stdin", _stdin_bytes(stdin_data))
    code, payload = _load_cli_json(argv, capsys)
    assert payload["activation_authorized"] is False
    assert payload["runtime_activation_outcome"] == "no_go"
    assert code in (1, 2)


def test_precheck_library_activation_posture_constant(
    tmp_path: Path,
) -> None:
    import test_paper_fast_loop_composition as helper
    from datetime import datetime
    from zoneinfo import ZoneInfo

    settings = RuntimePaperFastLoopSettings(enabled=True, symbol="005930")
    helper._seed_valid_stack(tmp_path, settings)
    now = datetime(2026, 6, 16, 0, 30, tzinfo=ZoneInfo("UTC"))
    result = precheck_runtime(settings=settings, now=now, base_dir=tmp_path)
    assert result.machine_outcome.value == "pass"
    assert result.activation_authorized is False
    assert result.runtime_activation_outcome == "no_go"


def test_no_tracked_runtime_files() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runtime"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert tracked.stdout.strip() == ""


def test_attended_activation_contract_doc_exists() -> None:
    path = _REPO_ROOT / "docs" / "PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "ACTIVATION_NOT_IMPLEMENTED" in text
    assert "activation_authorized" in text
    assert AttendedActivationStage.PRECHECK_MACHINE_PASS.value in text
