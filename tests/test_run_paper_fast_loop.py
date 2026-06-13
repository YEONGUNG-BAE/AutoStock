"""RTM-7c.4a — operator paper fast-loop CLI tests.

No network, no credentials, no production runtime DB. ``--run`` is refused before any
side effect. ``--replay`` uses an OS temp dir, never the configured runtime/ paths.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.paper_fast_loop import PaperFastLoopPaths
from config.settings import RuntimePaperFastLoopSettings

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
