from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = REPO_ROOT / "ops" / "fetch_research_sources.py"
INTAKE_SCRIPT = REPO_ROOT / "ops" / "research_source_intake.py"
SUCCESS_SNAPSHOT = REPO_ROOT / "tests" / "fixtures" / "research" / "fred" / "raw_dgs10_success.json"

AS_OF = "2026-05-29T09:00:00+09:00"
SECRET = "SECRET_FRED_KEY_TEST"
FIXED_FETCHED_AT = datetime(2026, 5, 29, 1, 2, 3, tzinfo=UTC)

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))


def _run_fetch_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(OPS_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _run_intake_validate(jsonl_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(INTAKE_SCRIPT),
            "--source-jsonl",
            str(jsonl_path),
            "--validate-only",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def test_cli_help_exits_zero() -> None:
    result = _run_fetch_cli("--help")
    assert result.returncode == 0


def test_cli_dry_run_json_writes_no_output_file(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        "--dry-run",
        "--source",
        "fred",
        "--series-id",
        "DGS10",
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["mode"] == "dry-run"
    assert payload["source"] == "fred"
    assert payload["series_id"] == "DGS10"
    assert payload["out_jsonl"] == str(out_jsonl)
    assert not out_jsonl.exists()


def test_cli_replay_json_writes_jsonl(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        "--replay",
        "--source",
        "fred",
        "--series-id",
        "DGS10",
        "--date-id",
        "260529-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "ok",
        "stage": "complete",
        "mode": "replay",
        "source": "fred",
        "series_id": "DGS10",
        "records_count": 1,
        "snapshot_path": str(SUCCESS_SNAPSHOT),
        "out_jsonl": str(out_jsonl),
    }
    lines = out_jsonl.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["fact_type"] == "macro"
    assert record["source_name"] == "fred"
    assert record["date_id"] == "260529-1"


def test_cli_replay_fails_if_output_exists_without_force(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    out_jsonl.write_text("existing\n", encoding="utf-8")

    result = _run_fetch_cli(
        "--replay",
        "--source",
        "fred",
        "--series-id",
        "DGS10",
        "--date-id",
        "260529-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["stage"] == "write"
    assert out_jsonl.read_text(encoding="utf-8") == "existing\n"


def test_cli_replay_force_overwrites_existing_file(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    out_jsonl.write_text("existing\n", encoding="utf-8")

    result = _run_fetch_cli(
        "--replay",
        "--source",
        "fred",
        "--series-id",
        "DGS10",
        "--date-id",
        "260529-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--force",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    lines = out_jsonl.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["source_name"] == "fred"


def test_cli_replay_jsonl_round_trips_through_8b_validate_only(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    replay = _run_fetch_cli(
        "--replay",
        "--source",
        "fred",
        "--series-id",
        "DGS10",
        "--date-id",
        "260529-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )
    assert replay.returncode == 0, replay.stderr

    intake = _run_intake_validate(out_jsonl)
    assert intake.returncode == 0, intake.stderr
    payload = json.loads(intake.stdout)
    assert payload["status"] == "ok"
    assert payload["records_valid"] == 1


def test_cli_json_and_verbose_keeps_stdout_pure_json(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        "--dry-run",
        "--source",
        "fred",
        "--series-id",
        "DGS10",
        "--out-jsonl",
        str(out_jsonl),
        "--json",
        "--verbose",
    )

    assert result.returncode == 0
    json.loads(result.stdout)
    assert "verbose:" in result.stderr


def test_new_files_do_not_use_forbidden_network_or_trading_tokens() -> None:
    paths = [
        REPO_ROOT / "ops" / "fetch_research_sources.py",
        REPO_ROOT / "src" / "data" / "research_source_fetcher.py",
        REPO_ROOT / "src" / "data" / "fred_source_fetcher.py",
    ]
    forbidden = (
        "requests",
        "httpx",
        "aiohttp",
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "yfinance",
        "kis",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in source, f"{path.name} must not reference {token!r}"


def _patch_urlopen_success(monkeypatch: pytest.MonkeyPatch) -> None:
    body_payload = {
        "observations": [
            {"date": "2026-05-28", "value": "4.25"},
        ]
    }

    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps(body_payload).encode("utf-8")

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setenv("FRED_API_KEY", SECRET)
    monkeypatch.setattr("data.fred_http_client.urlopen", fake_urlopen)


def _patch_urlopen_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_urlopen(request: object, timeout: float) -> object:
        raise HTTPError(
            url=f"https://api.stlouisfed.org/fred/series/observations?api_key={SECRET}&series_id=DGS10",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(b""),
        )

    monkeypatch.setenv("FRED_API_KEY", SECRET)
    monkeypatch.setattr("data.fred_http_client.urlopen", raising_urlopen)


def _patch_fixed_fetched_at(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDateTime:
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime:
            if tz is None:
                return FIXED_FETCHED_AT
            return FIXED_FETCHED_AT.astimezone(tz)

    monkeypatch.setattr("fetch_research_sources.datetime", FixedDateTime)


def _live_smoke_argv(
    *,
    snapshot_dir: Path,
    out_jsonl: Path,
    extra: list[str] | None = None,
) -> list[str]:
    argv = [
        "--live-smoke",
        "--source",
        "fred",
        "--series-id",
        "DGS10",
        "--date-id",
        "260529-1",
        "--as-of",
        AS_OF,
        "--snapshot-dir",
        str(snapshot_dir),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    ]
    if extra:
        argv.extend(extra)
    return argv


def test_live_smoke_success_snapshot_and_jsonl_exclude_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    _patch_urlopen_success(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "fred"
    out_jsonl = tmp_path / "research_sources.jsonl"

    exit_code = main(
        [
            "--live-smoke",
            "--source",
            "fred",
            "--series-id",
            "DGS10",
            "--date-id",
            "260529-1",
            "--as-of",
            AS_OF,
            "--snapshot-dir",
            str(snapshot_dir),
            "--out-jsonl",
            str(out_jsonl),
            "--json",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "ok"
    assert payload["mode"] == "live-smoke"
    assert out_jsonl.is_file()

    snapshot_files = list(snapshot_dir.glob("raw_*.json"))
    assert len(snapshot_files) == 1
    snapshot_text = snapshot_files[0].read_text(encoding="utf-8")
    assert SECRET not in snapshot_text
    assert "api_key=" not in snapshot_text.lower()
    snapshot = json.loads(snapshot_text)
    assert "api_key" not in snapshot["request"]
    assert "?" not in snapshot["request"]["base_url"]


def test_live_smoke_http_error_does_not_leak_api_key_or_write_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    _patch_urlopen_http_error(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "fred"
    out_jsonl = tmp_path / "research_sources.jsonl"

    exit_code = main(
        [
            "--live-smoke",
            "--source",
            "fred",
            "--series-id",
            "DGS10",
            "--date-id",
            "260529-1",
            "--as-of",
            AS_OF,
            "--snapshot-dir",
            str(snapshot_dir),
            "--out-jsonl",
            str(out_jsonl),
            "--json",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["stage"] == "fetch"
    assert SECRET not in payload["error"]
    assert not out_jsonl.exists()
    assert list(snapshot_dir.glob("raw_*.json")) == []


def test_live_smoke_jsonl_round_trips_through_8b_validate_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fetch_research_sources import main

    _patch_urlopen_success(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "fred"
    out_jsonl = tmp_path / "research_sources.jsonl"

    exit_code = main(_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl))
    assert exit_code == 0

    intake = _run_intake_validate(out_jsonl)
    assert intake.returncode == 0, intake.stderr
    payload = json.loads(intake.stdout)
    assert payload["status"] == "ok"
    assert payload["records_valid"] == 1


def test_live_smoke_existing_snapshot_fails_even_with_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    _patch_urlopen_success(monkeypatch)
    _patch_fixed_fetched_at(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "fred"
    out_jsonl = tmp_path / "research_sources.jsonl"

    assert main(_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl)) == 0
    capsys.readouterr()
    snapshot_files = list(snapshot_dir.glob("raw_*.json"))
    assert len(snapshot_files) == 1
    original_bytes = snapshot_files[0].read_bytes()
    out_jsonl.unlink()

    exit_code = main(
        _live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, extra=["--force"])
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["stage"] == "snapshot"
    assert snapshot_files[0].read_bytes() == original_bytes
    assert not out_jsonl.exists()


def test_live_smoke_missing_api_key_fails_before_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    monkeypatch.delenv("FRED_API_KEY", raising=False)
    snapshot_dir = tmp_path / "sources" / "fred"
    out_jsonl = tmp_path / "research_sources.jsonl"

    exit_code = main(_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl))

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["stage"] == "args"
    assert list(snapshot_dir.glob("raw_*.json")) == []
    assert not out_jsonl.exists()


def test_live_smoke_dot_observation_value_fails_without_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    body_payload = {
        "observations": [
            {"date": "2026-05-28", "value": "."},
        ]
    }

    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps(body_payload).encode("utf-8")

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setenv("FRED_API_KEY", SECRET)
    monkeypatch.setattr("data.fred_http_client.urlopen", fake_urlopen)

    snapshot_dir = tmp_path / "sources" / "fred"
    out_jsonl = tmp_path / "research_sources.jsonl"

    exit_code = main(_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl))

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["stage"] == "fetch"
    assert not out_jsonl.exists()
    assert list(snapshot_dir.glob("raw_*.json")) == []


@pytest.mark.parametrize(
    "extra_flags",
    [
        ["--dry-run", "--live-smoke"],
        ["--replay", "--live-smoke"],
    ],
)
def test_mode_mutex_rejects_live_smoke_combined_with_other_modes(
    tmp_path: Path,
    extra_flags: list[str],
) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    argv = [
        "--source",
        "fred",
        "--series-id",
        "DGS10",
        "--out-jsonl",
        str(out_jsonl),
        "--json",
        *extra_flags,
    ]
    if "--replay" in extra_flags:
        argv.extend(
            [
                "--date-id",
                "260529-1",
                "--as-of",
                AS_OF,
                "--snapshot",
                str(SUCCESS_SNAPSHOT),
            ]
        )

    result = _run_fetch_cli(*argv)
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["stage"] == "args"
