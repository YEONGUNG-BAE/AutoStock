from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = REPO_ROOT / "ops" / "fetch_research_sources.py"
INTAKE_SCRIPT = REPO_ROOT / "ops" / "research_source_intake.py"
SMOKE_SCRIPT = REPO_ROOT / "ops" / "run_date_md_smoke.py"
SUCCESS_SNAPSHOT = REPO_ROOT / "tests" / "fixtures" / "research" / "fred" / "raw_dgs10_success.json"
PRICE_SUCCESS_SNAPSHOT = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "price" / "raw_synth_kr_success.json"
)
PRICE_MISMATCHED_SYMBOL_SNAPSHOT = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "price" / "raw_synth_kr_mismatched_symbol.json"
)
EXAMPLE_UNIVERSE = REPO_ROOT / "config" / "universe.paper.toml.example"
DART_SUCCESS_SNAPSHOT = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "raw_synth_dart_success.json"
)
DART_AS_OF = "2026-05-30T13:00:00+09:00"
DART_SECRET = "SECRET_DART_KEY_TEST"
DART_CORP_CODE = "00126380"
DART_BGN_DE = "20260530"

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


def _run_intake_normal(
    *,
    jsonl_path: Path,
    store_path: Path,
    date_md_out: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(INTAKE_SCRIPT),
            "--source-jsonl",
            str(jsonl_path),
            "--store",
            str(store_path),
            "--date-md-out",
            str(date_md_out),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _run_date_md_smoke_cli(
    *,
    universe_path: Path,
    date_md_path: Path,
    store_path: Path,
    require_symbol_coverage: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    argv = [
        sys.executable,
        str(SMOKE_SCRIPT),
        "--universe",
        str(universe_path),
        "--date-md",
        str(date_md_path),
        "--store",
        str(store_path),
        "--json",
    ]
    if require_symbol_coverage:
        argv.append("--require-symbol-coverage")
    return subprocess.run(
        argv,
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
        REPO_ROOT / "src" / "data" / "price_source_fetcher.py",
        REPO_ROOT / "src" / "data" / "price_live_client.py",
        REPO_ROOT / "src" / "data" / "dart_source_fetcher.py",
        REPO_ROOT / "src" / "data" / "dart_live_client.py",
        REPO_ROOT / "src" / "data" / "dart_corp_code_resolver.py",
        REPO_ROOT / "ops" / "resolve_dart_corp_code.py",
        REPO_ROOT / "src" / "data" / "provider_mapping_registry.py",
        REPO_ROOT / "ops" / "validate_provider_mapping.py",
        REPO_ROOT / "ops" / "run_kr_real_price_smoke.py",
        REPO_ROOT / "ops" / "run_kr_real_dart_smoke.py",
        REPO_ROOT / "ops" / "build_kr_real_combined_context_smoke.py",
        REPO_ROOT / "ops" / "generate_kr_provider_mapping.py",
        REPO_ROOT / "src" / "data" / "kr_candidate_pool.py",
        REPO_ROOT / "ops" / "select_kr_candidates.py",
        REPO_ROOT / "ops" / "build_kr_real_sector_pool_mapping.py",
        REPO_ROOT / "ops" / "build_kr_real_ranked_mapping.py",
        REPO_ROOT / "src" / "data" / "kr_discovery_source_adapter.py",
        REPO_ROOT / "src" / "data" / "kr_discovery_live_client.py",
        REPO_ROOT / "src" / "data" / "kr_discovery_schema_mapper.py",
        REPO_ROOT / "src" / "data" / "kr_discovery_source_payload_snapshot.py",
        REPO_ROOT / "ops" / "replay_kr_discovery_snapshot.py",
        REPO_ROOT / "ops" / "run_kr_discovery_live_smoke.py",
        REPO_ROOT / "ops" / "map_kr_discovery_fixture.py",
        REPO_ROOT / "ops" / "run_kr_discovery_source_live_smoke.py",
        REPO_ROOT / "src" / "data" / "kr_candidate_ranker.py",
        REPO_ROOT / "ops" / "rank_kr_candidates.py",
        REPO_ROOT / "src" / "data" / "kr_factor_signal_generator.py",
        REPO_ROOT / "ops" / "generate_kr_factor_signals.py",
        REPO_ROOT / "ops" / "build_kr_factor_ranked_mapping.py",
        REPO_ROOT / "ops" / "build_kr_factor_bundle_mapping.py",
        REPO_ROOT / "src" / "data" / "kr_factor_source_adapter.py",
        REPO_ROOT / "ops" / "map_kr_factor_fixture.py",
        REPO_ROOT / "src" / "data" / "kr_factor_source_payload_snapshot.py",
        REPO_ROOT / "ops" / "run_kr_factor_source_live_smoke.py",
    ]
    forbidden = (
        "requests",
        "httpx",
        "aiohttp",
        "urllib.request",
        "urllib.parse",
        "urllib.error",
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


def test_cli_replay_price_json_writes_jsonl(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        "--replay",
        "--source",
        "price",
        "--symbol",
        "SYNTH-KR-0001",
        "--market",
        "KR",
        "--date-id",
        "260530-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(PRICE_SUCCESS_SNAPSHOT),
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
        "source": "price",
        "symbol": "SYNTH-KR-0001",
        "market": "KR",
        "records_count": 1,
        "snapshot_path": str(PRICE_SUCCESS_SNAPSHOT),
        "out_jsonl": str(out_jsonl),
    }
    lines = out_jsonl.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["fact_type"] == "price"
    assert record["symbol"] == "SYNTH-KR-0001"
    assert record["market"] == "KR"


def test_cli_replay_price_jsonl_round_trips_through_8b_validate_only(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    replay = _run_fetch_cli(
        "--replay",
        "--source",
        "price",
        "--symbol",
        "SYNTH-KR-0001",
        "--market",
        "KR",
        "--date-id",
        "260530-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(PRICE_SUCCESS_SNAPSHOT),
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


def test_cli_replay_price_8b_normal_and_8c_smoke_satisfies_symbol_coverage(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    date_md_path = tmp_path / "Date.md"
    universe_path = tmp_path / "universe.toml"
    universe_path.write_text(EXAMPLE_UNIVERSE.read_text(encoding="utf-8"), encoding="utf-8")

    replay = _run_fetch_cli(
        "--replay",
        "--source",
        "price",
        "--symbol",
        "SYNTH-KR-0001",
        "--market",
        "KR",
        "--date-id",
        "260530-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(PRICE_SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )
    assert replay.returncode == 0, replay.stderr

    intake = _run_intake_normal(
        jsonl_path=out_jsonl,
        store_path=store_path,
        date_md_out=date_md_path,
    )
    assert intake.returncode == 0, intake.stderr

    smoke = _run_date_md_smoke_cli(
        universe_path=universe_path,
        date_md_path=date_md_path,
        store_path=store_path,
    )
    assert smoke.returncode == 0, smoke.stderr
    payload = json.loads(smoke.stdout)
    assert payload["status"] == "ok"
    assert payload["missing_symbols"] == []


def test_cli_replay_price_fails_if_symbol_missing(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        "--replay",
        "--source",
        "price",
        "--market",
        "KR",
        "--date-id",
        "260530-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(PRICE_SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["stage"] == "args"
    assert "--symbol" in payload["error"]


def test_cli_replay_price_fails_if_market_missing(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        "--replay",
        "--source",
        "price",
        "--symbol",
        "SYNTH-KR-0001",
        "--date-id",
        "260530-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(PRICE_SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["stage"] == "args"
    assert "--market" in payload["error"]


def test_cli_replay_price_fails_on_mismatched_snapshot_symbol(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        "--replay",
        "--source",
        "price",
        "--symbol",
        "SYNTH-KR-0001",
        "--market",
        "KR",
        "--date-id",
        "260530-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(PRICE_MISMATCHED_SYMBOL_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["stage"] == "normalize"
    assert "symbol mismatch" in payload["error"]


def test_cli_replay_price_json_and_verbose_keeps_stdout_pure_json(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        "--replay",
        "--source",
        "price",
        "--symbol",
        "SYNTH-KR-0001",
        "--market",
        "KR",
        "--date-id",
        "260530-1",
        "--as-of",
        AS_OF,
        "--snapshot",
        str(PRICE_SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
        "--verbose",
    )

    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)
    assert "verbose:" in result.stderr


YFINANCE_ALLOWED = REPO_ROOT / "src" / "data" / "price_live_client.py"


def test_pyproject_contains_yfinance_dependency() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "yfinance" in text


def test_price_live_client_references_yfinance() -> None:
    source = YFINANCE_ALLOWED.read_text(encoding="utf-8")
    assert "yfinance" in source


def test_yfinance_import_only_in_price_live_client() -> None:
    """R3: production src/·ops/ 모듈 중 yfinance import는 price_live_client.py만 허용."""
    for root in (REPO_ROOT / "src", REPO_ROOT / "ops"):
        for path in root.rglob("*.py"):
            if path.resolve() == YFINANCE_ALLOWED.resolve():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "import yfinance" in stripped or stripped.startswith("from yfinance"):
                    raise AssertionError(f"{path} must not import yfinance")


class _PriceFakeILoc:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def __getitem__(self, idx: int) -> float:
        return self._values[idx]


class _PriceFakeCloseSeries:
    def __init__(self, values: list[float], indices: list[object]) -> None:
        self._values = values
        self.index = indices
        self.iloc = _PriceFakeILoc(values)

    def dropna(self) -> _PriceFakeCloseSeries:
        return self

    def __len__(self) -> int:
        return len(self._values)


class _PriceFakeHistory:
    def __init__(
        self,
        closes: list[float],
        indices: list[object],
        *,
        columns: list[str] | None = None,
    ) -> None:
        self._closes = closes
        self._indices = indices
        self.columns = list(columns) if columns is not None else ["Close"]

    def __len__(self) -> int:
        return len(self._closes)

    @property
    def empty(self) -> bool:
        return len(self._closes) == 0

    def __getitem__(self, key: str) -> _PriceFakeCloseSeries:
        if key == "Close":
            return _PriceFakeCloseSeries(self._closes, self._indices)
        raise KeyError(key)


class _PriceFakeTicker:
    def __init__(self, *, history: _PriceFakeHistory | None, currency: str | None = None) -> None:
        self._history = history
        self.info = {"currency": currency} if currency else {}

    def history(self, period: str, interval: str) -> _PriceFakeHistory | None:
        return self._history


def _default_price_fake_ticker_factory(_provider_symbol: str) -> _PriceFakeTicker:
    return _PriceFakeTicker(
        history=_PriceFakeHistory(
            closes=[71500.0],
            indices=[datetime(2026, 5, 30, tzinfo=UTC)],
        ),
        currency="KRW",
    )


def _patch_price_live_with_ticker_factory(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[str], _PriceFakeTicker],
) -> None:
    import data.price_live_client as price_live_client

    real_fetch = price_live_client.fetch_live_price_snapshot

    def bound_fetch(**kwargs: object) -> Path:
        if kwargs.get("ticker_factory") is None:
            kwargs["ticker_factory"] = factory
        return real_fetch(**kwargs)

    monkeypatch.setattr(price_live_client, "fetch_live_price_snapshot", bound_fetch)


def _patch_price_live_with_fake_ticker_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_price_live_with_ticker_factory(monkeypatch, _default_price_fake_ticker_factory)


def _price_live_smoke_argv(
    *,
    snapshot_dir: Path,
    out_jsonl: Path,
    extra: list[str] | None = None,
) -> list[str]:
    argv = [
        "--live-smoke",
        "--source",
        "price",
        "--symbol",
        "SYNTH-KR-0001",
        "--market",
        "KR",
        "--provider-symbol",
        "005930.KS",
        "--currency",
        "KRW",
        "--date-id",
        "260530-1",
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


def test_price_live_smoke_success_writes_snapshot_and_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    _patch_price_live_with_fake_ticker_factory(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"

    exit_code = main(_price_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl))

    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "status": "ok",
        "stage": "complete",
        "mode": "live-smoke",
        "source": "price",
        "symbol": "SYNTH-KR-0001",
        "market": "KR",
        "provider_symbol": "005930.KS",
        "records_count": 1,
        "snapshot_path": payload["snapshot_path"],
        "out_jsonl": str(out_jsonl),
    }
    assert out_jsonl.is_file()
    snapshot_files = list(snapshot_dir.glob("raw_*.json"))
    assert len(snapshot_files) == 1
    snapshot = json.loads(snapshot_files[0].read_text(encoding="utf-8"))
    assert snapshot["source_key"] == "price"
    assert snapshot["external_service"] == "yfinance"
    assert snapshot["provider_symbol"] == "005930.KS"
    assert "DataFrame" not in snapshot_files[0].read_text(encoding="utf-8")
    assert len(json.dumps(snapshot["payload"])) < 500


def test_price_live_smoke_jsonl_round_trips_through_8b_validate_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fetch_research_sources import main

    _patch_price_live_with_fake_ticker_factory(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"

    assert main(_price_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl)) == 0

    intake = _run_intake_validate(out_jsonl)
    assert intake.returncode == 0, intake.stderr
    payload = json.loads(intake.stdout)
    assert payload["status"] == "ok"
    assert payload["records_valid"] == 1


def test_price_live_smoke_8b_normal_and_8c_smoke_satisfies_symbol_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fetch_research_sources import main

    _patch_price_live_with_fake_ticker_factory(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    date_md_path = tmp_path / "Date.md"
    universe_path = tmp_path / "universe.toml"
    universe_path.write_text(EXAMPLE_UNIVERSE.read_text(encoding="utf-8"), encoding="utf-8")

    assert main(_price_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl)) == 0

    intake = _run_intake_normal(
        jsonl_path=out_jsonl,
        store_path=store_path,
        date_md_out=date_md_path,
    )
    assert intake.returncode == 0, intake.stderr

    smoke = _run_date_md_smoke_cli(
        universe_path=universe_path,
        date_md_path=date_md_path,
        store_path=store_path,
    )
    assert smoke.returncode == 0, smoke.stderr
    payload = json.loads(smoke.stdout)
    assert payload["status"] == "ok"
    assert payload["missing_symbols"] == []


def test_price_live_smoke_empty_history_fails_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    def factory(_provider_symbol: str) -> _PriceFakeTicker:
        return _PriceFakeTicker(history=_PriceFakeHistory(closes=[], indices=[]))

    _patch_price_live_with_ticker_factory(monkeypatch, factory)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"

    exit_code = main(_price_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl))

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["stage"] == "fetch"
    assert not out_jsonl.exists()
    assert list(snapshot_dir.glob("raw_*.json")) == []


def test_price_live_smoke_missing_close_column_fails_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    def factory(_provider_symbol: str) -> _PriceFakeTicker:
        return _PriceFakeTicker(
            history=_PriceFakeHistory(
                closes=[100.0],
                indices=[datetime(2026, 5, 30, tzinfo=UTC)],
                columns=["Open"],
            )
        )

    _patch_price_live_with_ticker_factory(monkeypatch, factory)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"

    exit_code = main(_price_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl))

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage"] == "fetch"
    assert not out_jsonl.exists()


def test_price_live_smoke_non_positive_close_fails_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    def factory(_provider_symbol: str) -> _PriceFakeTicker:
        return _PriceFakeTicker(
            history=_PriceFakeHistory(
                closes=[0.0],
                indices=[datetime(2026, 5, 30, tzinfo=UTC)],
            )
        )

    _patch_price_live_with_ticker_factory(monkeypatch, factory)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"

    exit_code = main(_price_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl))

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage"] == "fetch"
    assert not out_jsonl.exists()


def test_price_live_smoke_missing_provider_symbol_fails_args(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        "--live-smoke",
        "--source",
        "price",
        "--symbol",
        "SYNTH-KR-0001",
        "--market",
        "KR",
        "--date-id",
        "260530-1",
        "--as-of",
        AS_OF,
        "--snapshot-dir",
        str(tmp_path / "sources" / "price"),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "args"
    assert "--provider-symbol" in payload["error"]


def test_price_live_smoke_missing_symbol_or_market_fails_args(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    base = [
        "--live-smoke",
        "--source",
        "price",
        "--provider-symbol",
        "005930.KS",
        "--date-id",
        "260530-1",
        "--as-of",
        AS_OF,
        "--snapshot-dir",
        str(tmp_path / "sources" / "price"),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    ]
    missing_symbol = _run_fetch_cli(*base, "--market", "KR")
    assert missing_symbol.returncode == 1
    assert "--symbol" in json.loads(missing_symbol.stdout)["error"]

    missing_market = _run_fetch_cli(
        *base,
        "--symbol",
        "SYNTH-KR-0001",
    )
    assert missing_market.returncode == 1
    assert "--market" in json.loads(missing_market.stdout)["error"]


def test_price_live_smoke_existing_snapshot_fails_even_with_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    _patch_price_live_with_fake_ticker_factory(monkeypatch)
    _patch_fixed_fetched_at(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "price"
    out_jsonl = tmp_path / "research_sources.jsonl"

    assert main(_price_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl)) == 0
    capsys.readouterr()
    snapshot_files = list(snapshot_dir.glob("raw_*.json"))
    assert len(snapshot_files) == 1
    original_bytes = snapshot_files[0].read_bytes()
    out_jsonl.unlink()

    exit_code = main(
        _price_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, extra=["--force"])
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage"] == "snapshot"
    assert snapshot_files[0].read_bytes() == original_bytes
    assert not out_jsonl.exists()


def test_price_live_smoke_json_and_verbose_keeps_stdout_pure_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_price_live_with_fake_ticker_factory(monkeypatch)
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        *_price_live_smoke_argv(
            snapshot_dir=tmp_path / "sources" / "price",
            out_jsonl=out_jsonl,
            extra=["--verbose"],
        )
    )
    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)
    assert "verbose:" in result.stderr


def _success_opendart_list_body() -> dict[str, object]:
    return {
        "status": "000",
        "message": "정상",
        "page_no": 1,
        "page_count": 100,
        "total_count": 2,
        "total_page": 1,
        "list": [
            {
                "corp_code": DART_CORP_CODE,
                "corp_name": "Synthetic Corp",
                "stock_code": "000000",
                "corp_cls": "Y",
                "report_nm": "Synthetic DART report 1",
                "rcept_no": "202605300001",
                "flr_nm": "Synthetic Corp",
                "rcept_dt": "20260530",
                "rm": "",
            },
            {
                "corp_code": DART_CORP_CODE,
                "corp_name": "Synthetic Corp",
                "stock_code": "000000",
                "corp_cls": "Y",
                "report_nm": "Synthetic DART report 2",
                "rcept_no": "202605300002",
                "flr_nm": "Synthetic Corp",
                "rcept_dt": "20260530",
                "rm": "",
            },
        ],
    }


def _patch_opendart_urlopen_success(monkeypatch: pytest.MonkeyPatch) -> None:
    body_payload = _success_opendart_list_body()

    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps(body_payload).encode("utf-8")

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setenv("DART_API_KEY", DART_SECRET)
    monkeypatch.setattr("data.dart_http_client.urlopen", fake_urlopen)


def _patch_fixed_dart_fetched_at(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDateTime:
        @classmethod
        def now(cls, tz: datetime.tzinfo | None = None) -> datetime:
            if tz is None:
                return FIXED_FETCHED_AT
            return FIXED_FETCHED_AT.astimezone(tz)

    monkeypatch.setattr("fetch_research_sources.datetime", FixedDateTime)


def _dart_live_smoke_argv(
    *,
    snapshot_dir: Path,
    out_jsonl: Path,
    store_path: Path,
    extra: list[str] | None = None,
) -> list[str]:
    argv = [
        "--live-smoke",
        "--source",
        "dart",
        "--symbol",
        "SYNTH-KR-0001",
        "--corp-code",
        DART_CORP_CODE,
        "--bgn-de",
        DART_BGN_DE,
        "--store",
        str(store_path),
        "--as-of",
        DART_AS_OF,
        "--snapshot-dir",
        str(snapshot_dir),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    ]
    if extra:
        argv.extend(extra)
    return argv


def test_dart_live_smoke_success_snapshot_and_jsonl_exclude_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    _patch_opendart_urlopen_success(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    from data import SQLiteDateIdSourceStore

    SQLiteDateIdSourceStore(store_path).close()

    exit_code = main(_dart_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path))
    assert exit_code == 0
    captured = capsys.readouterr()
    assert DART_SECRET not in captured.out
    assert DART_SECRET not in captured.err
    payload = json.loads(captured.out)
    assert payload["status"] == "ok"
    assert payload["mode"] == "live-smoke"
    assert payload["source"] == "dart"
    assert payload["corp_code"] == DART_CORP_CODE
    assert payload["records_count"] == 2

    snapshot_files = list(snapshot_dir.glob("raw_*.json"))
    assert len(snapshot_files) == 1
    snapshot_text = snapshot_files[0].read_text(encoding="utf-8")
    assert DART_SECRET not in snapshot_text
    assert "crtfc_key=" not in snapshot_text.lower()
    assert out_jsonl.is_file()
    assert DART_SECRET not in out_jsonl.read_text(encoding="utf-8")


def test_dart_live_smoke_jsonl_round_trips_through_8b_validate_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fetch_research_sources import main

    _patch_opendart_urlopen_success(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    from data import SQLiteDateIdSourceStore

    SQLiteDateIdSourceStore(store_path).close()
    assert main(_dart_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0

    intake = _run_intake_validate(out_jsonl)
    assert intake.returncode == 0, intake.stderr
    payload = json.loads(intake.stdout)
    assert payload["status"] == "ok"
    assert payload["records_valid"] == 2


def test_dart_live_smoke_without_api_key_env_reads_dart_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main
    from urllib.parse import parse_qs, urlparse

    body_payload = _success_opendart_list_body()
    captured_crtfc_keys: list[str] = []

    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps(body_payload).encode("utf-8")

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        url_text = request.full_url  # type: ignore[attr-defined]
        query = parse_qs(urlparse(url_text).query)
        captured_crtfc_keys.append(query.get("crtfc_key", [""])[0])
        return FakeResponse()

    monkeypatch.setenv("DART_API_KEY", DART_SECRET)
    monkeypatch.setenv("FRED_API_KEY", "SECRET_FRED_KEY_SHOULD_NOT_BE_USED")
    monkeypatch.setattr("data.dart_http_client.urlopen", fake_urlopen)

    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    from data import SQLiteDateIdSourceStore

    SQLiteDateIdSourceStore(store_path).close()

    exit_code = main(_dart_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path))
    assert exit_code == 0
    captured = capsys.readouterr()
    assert DART_SECRET not in captured.out
    assert DART_SECRET not in captured.err
    assert captured_crtfc_keys == [DART_SECRET]
    assert out_jsonl.is_file()


def test_dart_live_smoke_without_api_key_env_does_not_use_fred_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.setenv("FRED_API_KEY", "SECRET_FRED_KEY_SHOULD_NOT_BE_USED")
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"

    exit_code = main(_dart_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path))
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "SECRET_FRED_KEY_SHOULD_NOT_BE_USED" not in captured.out
    assert "SECRET_FRED_KEY_SHOULD_NOT_BE_USED" not in captured.err
    payload = json.loads(captured.out)
    assert payload["stage"] == "args"
    assert "DART_API_KEY" in payload["error"]
    assert not out_jsonl.exists()
    assert list(snapshot_dir.glob("raw_*.json")) == []


def test_dart_live_smoke_explicit_custom_api_key_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    custom_secret = "SECRET_CUSTOM_DART_KEY_TEST"
    _patch_opendart_urlopen_success(monkeypatch)
    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.setenv("CUSTOM_DART_KEY", custom_secret)

    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    from data import SQLiteDateIdSourceStore

    SQLiteDateIdSourceStore(store_path).close()

    exit_code = main(
        _dart_live_smoke_argv(
            snapshot_dir=snapshot_dir,
            out_jsonl=out_jsonl,
            store_path=store_path,
            extra=["--api-key-env", "CUSTOM_DART_KEY"],
        )
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert custom_secret not in captured.out
    assert custom_secret not in captured.err
    assert out_jsonl.is_file()


def test_fred_live_smoke_without_api_key_env_still_defaults_to_fred_api_key(
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
    assert out_jsonl.is_file()


def test_dart_live_smoke_missing_api_key_fails_before_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    monkeypatch.delenv("DART_API_KEY", raising=False)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"

    exit_code = main(_dart_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path))
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage"] == "args"
    assert not out_jsonl.exists()
    assert list(snapshot_dir.glob("raw_*.json")) == []


def test_dart_live_smoke_blank_api_key_fails_before_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fetch_research_sources import main

    monkeypatch.setenv("DART_API_KEY", "   ")
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"

    exit_code = main(_dart_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path))
    assert exit_code == 1
    assert not out_jsonl.exists()


def test_dart_live_smoke_rejects_date_id(tmp_path: Path) -> None:
    from fetch_research_sources import main

    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    exit_code = main(
        _dart_live_smoke_argv(
            snapshot_dir=snapshot_dir,
            out_jsonl=out_jsonl,
            store_path=store_path,
            extra=["--date-id", "260530-1"],
        )
    )
    assert exit_code == 1


def test_dart_live_smoke_snapshot_collision_fails_without_jsonl_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fetch_research_sources import main

    _patch_opendart_urlopen_success(monkeypatch)
    _patch_fixed_dart_fetched_at(monkeypatch)
    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    from data import SQLiteDateIdSourceStore

    SQLiteDateIdSourceStore(store_path).close()

    assert main(_dart_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path)) == 0
    capsys.readouterr()
    snapshot_files = list(snapshot_dir.glob("raw_*.json"))
    assert len(snapshot_files) == 1
    original_bytes = snapshot_files[0].read_bytes()
    out_jsonl.unlink()

    exit_code = main(
        _dart_live_smoke_argv(
            snapshot_dir=snapshot_dir,
            out_jsonl=out_jsonl,
            store_path=store_path,
            extra=["--force"],
        )
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage"] == "snapshot"
    assert snapshot_files[0].read_bytes() == original_bytes
    assert not out_jsonl.exists()


def test_dart_live_smoke_does_not_write_store_during_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fetch_research_sources import main

    _patch_opendart_urlopen_success(monkeypatch)
    store_path = tmp_path / "date_id_sources.sqlite3"
    from data import SQLiteDateIdSourceStore

    SQLiteDateIdSourceStore(store_path).close()
    before_store = SQLiteDateIdSourceStore(store_path)
    before_count = len(before_store.list_records())
    before_store.close()

    snapshot_dir = tmp_path / "sources" / "dart"
    out_jsonl = tmp_path / "research_sources.dart.jsonl"
    assert (
        main(_dart_live_smoke_argv(snapshot_dir=snapshot_dir, out_jsonl=out_jsonl, store_path=store_path))
        == 0
    )

    after_store = SQLiteDateIdSourceStore(store_path)
    try:
        assert len(after_store.list_records()) == before_count
    finally:
        after_store.close()


def test_research_intake_modules_do_not_use_urllib_except_http_clients() -> None:
    """Real Intake 경로는 urllib 격리 — HTTP는 fred_http_client·dart_http_client만."""
    for relative in (
        "ops/fetch_research_sources.py",
        "src/data/research_source_fetcher.py",
        "src/data/fred_source_fetcher.py",
        "src/data/price_source_fetcher.py",
        "src/data/price_live_client.py",
        "src/data/dart_source_fetcher.py",
        "src/data/dart_live_client.py",
        "src/data/dart_corp_code_resolver.py",
        "ops/resolve_dart_corp_code.py",
        "src/data/provider_mapping_registry.py",
        "src/data/kr_provider_mapping_generator.py",
        "ops/validate_provider_mapping.py",
        "ops/run_kr_real_price_smoke.py",
        "ops/run_kr_real_dart_smoke.py",
        "ops/build_kr_real_combined_context_smoke.py",
        "ops/generate_kr_provider_mapping.py",
    ):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8").lower()
        assert "urllib.request" not in text, relative
        assert "urllib.parse" not in text, relative
        assert "urllib.error" not in text, relative


def _run_dart_replay_cli(
    tmp_path: Path,
    *,
    out_jsonl: Path | None = None,
    store_path: Path | None = None,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    resolved_out = out_jsonl or (tmp_path / "research_sources.jsonl")
    resolved_store = store_path or (tmp_path / "date_id_sources.sqlite3")
    argv = [
        "--replay",
        "--source",
        "dart",
        "--symbol",
        "SYNTH-KR-0001",
        "--store",
        str(resolved_store),
        "--as-of",
        DART_AS_OF,
        "--snapshot",
        str(DART_SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(resolved_out),
        "--json",
    ]
    if extra:
        argv.extend(extra)
    return _run_fetch_cli(*argv)


def test_cli_replay_dart_json_writes_jsonl(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    result = _run_dart_replay_cli(tmp_path, out_jsonl=out_jsonl, store_path=store_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["mode"] == "replay"
    assert payload["source"] == "dart"
    assert payload["symbol"] == "SYNTH-KR-0001"
    assert payload["records_count"] == 2
    lines = out_jsonl.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["fact_type"] == "disclosure"
    assert first["date_id"] == "260530-1"


def test_cli_replay_dart_jsonl_round_trips_through_8b_validate_only(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    replay = _run_dart_replay_cli(tmp_path, out_jsonl=out_jsonl)
    assert replay.returncode == 0, replay.stderr

    intake = _run_intake_validate(out_jsonl)
    assert intake.returncode == 0, intake.stderr
    payload = json.loads(intake.stdout)
    assert payload["status"] == "ok"
    assert payload["records_valid"] == 2


def test_cli_replay_dart_8b_normal_and_8c_smoke_without_symbol_coverage(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    date_md_path = tmp_path / "Date.md"
    universe_path = tmp_path / "universe.toml"
    universe_path.write_text(EXAMPLE_UNIVERSE.read_text(encoding="utf-8"), encoding="utf-8")

    replay = _run_dart_replay_cli(tmp_path, out_jsonl=out_jsonl, store_path=store_path)
    assert replay.returncode == 0, replay.stderr

    intake = _run_intake_normal(
        jsonl_path=out_jsonl,
        store_path=store_path,
        date_md_out=date_md_path,
    )
    assert intake.returncode == 0, intake.stderr

    smoke = _run_date_md_smoke_cli(
        universe_path=universe_path,
        date_md_path=date_md_path,
        store_path=store_path,
        require_symbol_coverage=False,
    )
    assert smoke.returncode == 0, smoke.stderr
    payload = json.loads(smoke.stdout)
    assert payload["status"] == "ok"
    assert payload["missing_symbols"] != []


def test_cli_replay_dart_8c_require_symbol_coverage_fails_for_dart_only(
    tmp_path: Path,
) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    date_md_path = tmp_path / "Date.md"
    universe_path = tmp_path / "universe.toml"
    universe_path.write_text(EXAMPLE_UNIVERSE.read_text(encoding="utf-8"), encoding="utf-8")

    assert _run_dart_replay_cli(tmp_path, out_jsonl=out_jsonl, store_path=store_path).returncode == 0
    assert (
        _run_intake_normal(
            jsonl_path=out_jsonl,
            store_path=store_path,
            date_md_out=date_md_path,
        ).returncode
        == 0
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    smoke = subprocess.run(
        [
            sys.executable,
            str(SMOKE_SCRIPT),
            "--universe",
            str(universe_path),
            "--date-md",
            str(date_md_path),
            "--store",
            str(store_path),
            "--require-symbol-coverage",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    assert smoke.returncode != 0
    payload = json.loads(smoke.stdout)
    assert payload["status"] == "error"


def test_cli_replay_dart_does_not_write_store_during_fetch(tmp_path: Path) -> None:
    store_path = tmp_path / "date_id_sources.sqlite3"
    from data import SQLiteDateIdSourceStore

    SQLiteDateIdSourceStore(store_path).close()
    before_store = SQLiteDateIdSourceStore(store_path)
    before_count = len(before_store.list_records())
    before_store.close()

    out_jsonl = tmp_path / "research_sources.jsonl"
    assert _run_dart_replay_cli(tmp_path, out_jsonl=out_jsonl, store_path=store_path).returncode == 0

    after_store = SQLiteDateIdSourceStore(store_path)
    try:
        assert len(after_store.list_records()) == before_count
    finally:
        after_store.close()
    assert out_jsonl.is_file()


def test_cli_replay_dart_requires_store(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_fetch_cli(
        "--replay",
        "--source",
        "dart",
        "--symbol",
        "SYNTH-KR-0001",
        "--as-of",
        DART_AS_OF,
        "--snapshot",
        str(DART_SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "args"
    assert "--store" in payload["error"]


def test_cli_replay_dart_fails_if_store_parent_missing(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    missing_parent = tmp_path / "missing" / "date_id_sources.sqlite3"
    result = _run_dart_replay_cli(tmp_path, out_jsonl=out_jsonl, store_path=missing_parent)
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "args"
    assert "store parent directory does not exist" in payload["error"]


def test_cli_replay_dart_accepts_empty_new_store_file(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = tmp_path / "new_store.sqlite3"
    result = _run_dart_replay_cli(tmp_path, out_jsonl=out_jsonl, store_path=store_path)
    assert result.returncode == 0, result.stderr
    assert store_path.is_file()


def test_cli_replay_dart_rejects_date_id(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    result = _run_dart_replay_cli(
        tmp_path,
        out_jsonl=out_jsonl,
        store_path=store_path,
        extra=["--date-id", "260530-1"],
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "args"
    assert "--date-id is not supported" in payload["error"]


def test_cli_replay_dart_requires_symbol(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    result = _run_fetch_cli(
        "--replay",
        "--source",
        "dart",
        "--store",
        str(store_path),
        "--as-of",
        DART_AS_OF,
        "--snapshot",
        str(DART_SUCCESS_SNAPSHOT),
        "--out-jsonl",
        str(out_jsonl),
        "--json",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "args"
    assert "--symbol" in payload["error"]


def test_cli_replay_dart_rejects_limit_zero(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    store_path = tmp_path / "date_id_sources.sqlite3"
    result = _run_dart_replay_cli(
        tmp_path,
        out_jsonl=out_jsonl,
        store_path=store_path,
        extra=["--limit", "0"],
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "args"
    assert "--limit" in payload["error"]


def test_cli_replay_dart_json_and_verbose_keeps_stdout_pure_json(tmp_path: Path) -> None:
    out_jsonl = tmp_path / "research_sources.jsonl"
    result = _run_dart_replay_cli(tmp_path, out_jsonl=out_jsonl, extra=["--verbose"])
    assert result.returncode == 0, result.stderr
    json.loads(result.stdout)
    assert "verbose:" in result.stderr
