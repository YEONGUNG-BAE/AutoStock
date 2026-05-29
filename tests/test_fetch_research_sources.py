from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = REPO_ROOT / "ops" / "fetch_research_sources.py"
INTAKE_SCRIPT = REPO_ROOT / "ops" / "research_source_intake.py"
SUCCESS_SNAPSHOT = REPO_ROOT / "tests" / "fixtures" / "research" / "fred" / "raw_dgs10_success.json"

AS_OF = "2026-05-29T09:00:00+09:00"


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
