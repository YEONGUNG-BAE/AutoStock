"""Real Intake 3G3-4B — KR discovery HTTP live smoke CLI tests."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_discovery"
    / "raw_kr_discovery_synthetic_success.json"
)
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
OPS_SCRIPT = REPO_ROOT / "ops" / "run_kr_discovery_live_smoke.py"

KST = timezone(timedelta(hours=9))
FETCHED_AT = datetime(2026, 5, 30, 12, 0, 0, tzinfo=KST)
AS_OF = FETCHED_AT
SECRET = "SECRET_VALUE_TEST"
ENDPOINT_URL = "https://example.test/discovery.json"
ENDPOINT_URL_WITH_SECRET = f"https://example.test/discovery.json?session={SECRET}"

_FORBIDDEN_OUTPUT_FIELDS = frozenset(
    {
        "corp_code",
        "action",
        "request",
        "api_key",
        "crtfc_key",
    }
)

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data.kr_candidate_pool import parse_kr_candidate_pool_toml
from data.kr_discovery_source_adapter import load_kr_discovery_snapshot
from run_kr_discovery_live_smoke import KrDiscoveryLiveSmokeError, run_kr_discovery_live_smoke


def _transport_payload() -> dict[str, object]:
    payload = json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))
    return {"records": payload["records"]}


def _fake_urlopen(*, body: bytes | None = None, raises: Exception | None = None) -> object:
    resolved_body = json.dumps(_transport_payload()).encode("utf-8") if body is None else body

    def urlopen(_request: object, timeout: float) -> object:
        if raises is not None:
            raise raises

        class FakeResponse:
            def read(self) -> bytes:
                return resolved_body

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        return FakeResponse()

    return urlopen


def _runner_args(tmp_path: Path, *, candidate_pool_out: Path | None = None) -> dict[str, object]:
    args: dict[str, object] = {
        "endpoint_url": ENDPOINT_URL,
        "snapshot_dir": tmp_path / "snapshots",
        "fetched_at": FETCHED_AT,
        "as_of": AS_OF,
        "universe_hint": "operator-supplied-discovery",
        "external_service": "operator-http-discovery",
        "timeout_seconds": 15.0,
        "urlopen_fn": _fake_urlopen(),
    }
    if candidate_pool_out is not None:
        args["candidate_pool_out"] = candidate_pool_out
        args["pool_name"] = "kr-discovery-live-candidate-pool-v1"
        args["pool_description"] = "Operator-triggered KR discovery live smoke replay."
    return args


def _run_smoke(tmp_path: Path, **overrides: object) -> dict[str, object]:
    args = _runner_args(tmp_path, candidate_pool_out=overrides.pop("candidate_pool_out", None))
    args.update(overrides)
    return run_kr_discovery_live_smoke(**args)  # type: ignore[arg-type]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(OPS_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _base_cli_args(tmp_path: Path) -> list[str]:
    return [
        "--endpoint-url",
        ENDPOINT_URL,
        "--snapshot-dir",
        str(tmp_path / "snapshots"),
        "--fetched-at",
        FETCHED_AT.isoformat(),
        "--as-of",
        AS_OF.isoformat(),
        "--universe-hint",
        "operator-supplied-discovery",
        "--external-service",
        "operator-http-discovery",
    ]


def test_cli_writes_raw_snapshot(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    snapshot_path = Path(str(payload["snapshot_path"]))
    assert snapshot_path.is_file()
    assert payload["records_count"] == 5


def test_cli_replays_candidate_pool_when_requested(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    payload = _run_smoke(tmp_path, candidate_pool_out=pool_out)
    assert Path(str(payload["candidate_pool_out"])).is_file()
    pool = parse_kr_candidate_pool_toml(pool_out)
    assert len(pool.candidates) == 5


def test_without_candidate_pool_out_only_snapshot_written(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    assert "candidate_pool_out" not in payload
    assert list(tmp_path.rglob("*.toml")) == []


def test_raw_snapshot_has_no_root_request(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    raw = json.loads(Path(str(payload["snapshot_path"])).read_text(encoding="utf-8"))
    assert "request" not in raw


def test_raw_snapshot_has_no_secret_fields(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path, endpoint_url=ENDPOINT_URL_WITH_SECRET)
    raw = json.loads(Path(str(payload["snapshot_path"])).read_text(encoding="utf-8"))

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                assert key not in _FORBIDDEN_OUTPUT_FIELDS
                assert SECRET not in str(nested)
                _walk(nested)
        elif isinstance(value, list):
            for nested in value:
                _walk(nested)

    _walk(raw)
    assert SECRET not in Path(str(payload["snapshot_path"])).read_text(encoding="utf-8")


def test_raw_snapshot_replays_through_3g3_parser(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    snapshot = load_kr_discovery_snapshot(Path(str(payload["snapshot_path"])))
    assert len(snapshot.records) == 5


def test_replayed_candidate_pool_loads_via_3g1_parser(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    _run_smoke(tmp_path, candidate_pool_out=pool_out)
    pool = parse_kr_candidate_pool_toml(pool_out)
    assert pool.base_market == "KR"


def test_candidate_pool_overwrite_requires_force(tmp_path: Path) -> None:
    from data.kr_discovery_source_adapter import KrDiscoverySourceAdapterError, replay_kr_discovery_snapshot

    pool_out = tmp_path / "candidate_pool.toml"
    payload = _run_smoke(tmp_path)
    snapshot_path = Path(str(payload["snapshot_path"]))
    replay_kr_discovery_snapshot(
        snapshot_path=snapshot_path,
        candidate_pool_out=pool_out,
        pool_name="kr-discovery-live-candidate-pool-v1",
        pool_description="Operator-triggered KR discovery live smoke replay.",
        force=True,
    )
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        replay_kr_discovery_snapshot(
            snapshot_path=snapshot_path,
            candidate_pool_out=pool_out,
            pool_name="kr-discovery-live-candidate-pool-v1",
            pool_description="Operator-triggered KR discovery live smoke replay.",
            force=False,
        )
    assert exc_info.value.stage == "write"


def test_raw_snapshot_collision_remains_immutable_even_with_force(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    _run_smoke(tmp_path, candidate_pool_out=pool_out, force=True)
    with pytest.raises(KrDiscoveryLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, candidate_pool_out=pool_out, force=True)
    assert exc_info.value.stage == "snapshot"
    assert "already exists" in exc_info.value.message


def test_invalid_http_failure_emits_sanitized_json_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from data.kr_discovery_http_client import KrDiscoveryHttpError
    from run_kr_discovery_live_smoke import main

    def _raise_fetch(**kwargs: object) -> dict[str, object]:
        raise KrDiscoveryHttpError("fetch", "discovery HTTP request failed: URLError: api_key=<redacted>")

    monkeypatch.setattr("run_kr_discovery_live_smoke.fetch_kr_discovery_http_payload", _raise_fetch)
    argv = _base_cli_args(tmp_path) + ["--json"]
    assert main(argv) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "error"
    assert payload["stage"] == "fetch"
    assert SECRET not in payload["error"]


def test_secret_exception_not_in_stdout_stderr_or_error_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from data.kr_discovery_http_client import fetch_kr_discovery_http_payload
    from run_kr_discovery_live_smoke import main

    def _patched_fetch(**kwargs: object) -> dict[str, object]:
        return fetch_kr_discovery_http_payload(
            endpoint_url=str(kwargs["endpoint_url"]),
            timeout_seconds=float(kwargs.get("timeout_seconds", 15.0)),
            urlopen_fn=_fake_urlopen(raises=URLError(f"open failed api_key={SECRET}")),
            extra_secret_values=(SECRET,),
        )

    monkeypatch.setattr("run_kr_discovery_live_smoke.fetch_kr_discovery_http_payload", _patched_fetch)
    argv = _base_cli_args(tmp_path) + [
        "--endpoint-url",
        ENDPOINT_URL_WITH_SECRET,
        "--json",
    ]
    assert main(argv) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert SECRET not in payload["error"]


def test_http_invalid_json_surfaces_as_parse_stage(tmp_path: Path) -> None:
    with pytest.raises(KrDiscoveryLiveSmokeError) as exc_info:
        _run_smoke(
            tmp_path,
            urlopen_fn=_fake_urlopen(body=b"not-json"),
        )
    assert exc_info.value.stage == "parse"


def test_http_fetch_failure_surfaces_as_fetch_stage(tmp_path: Path) -> None:
    with pytest.raises(KrDiscoveryLiveSmokeError) as exc_info:
        _run_smoke(
            tmp_path,
            urlopen_fn=_fake_urlopen(raises=URLError("network down")),
        )
    assert exc_info.value.stage == "fetch"


def test_success_json_does_not_include_endpoint_url(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path, endpoint_url=ENDPOINT_URL_WITH_SECRET)
    serialized = json.dumps(payload)
    assert "endpoint_url" not in payload
    assert ENDPOINT_URL not in serialized
    assert ENDPOINT_URL_WITH_SECRET not in serialized


def test_success_path_with_secret_endpoint_does_not_expose_secret(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path, endpoint_url=ENDPOINT_URL_WITH_SECRET)
    serialized = json.dumps(payload)
    snapshot_text = Path(str(payload["snapshot_path"])).read_text(encoding="utf-8")
    assert SECRET not in serialized
    assert SECRET not in snapshot_text


def test_invalid_timeout_fails_at_args_stage(tmp_path: Path) -> None:
    with pytest.raises(KrDiscoveryLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, timeout_seconds=0)
    assert exc_info.value.stage == "args"


def test_invalid_datetime_fails_at_args_stage(tmp_path: Path) -> None:
    from run_kr_discovery_live_smoke import main

    argv = _base_cli_args(tmp_path) + ["--fetched-at", "2026-05-30T00:00:00", "--json"]
    assert main(argv) == 1


def test_non_kr_market_fails_at_args_stage(tmp_path: Path) -> None:
    with pytest.raises(KrDiscoveryLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, market="US")
    assert exc_info.value.stage == "args"


def test_ops_script_has_no_env_or_urllib_reads() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    forbidden = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
        "dart_api_key",
        "fred_api_key",
        "import yfinance",
        "from yfinance",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
        "os.environ",
        "getenv",
    )
    for token in forbidden:
        assert token not in source, f"run_kr_discovery_live_smoke.py must not reference {token!r}"


def test_no_runtime_files_tracked_in_repo() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runtime"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert tracked.returncode == 0
    assert tracked.stdout.strip() == ""


def test_static_scan_includes_ops_file_only() -> None:
    paths_text = (REPO_ROOT / "tests" / "test_fetch_research_sources.py").read_text(encoding="utf-8")
    assert "run_kr_discovery_live_smoke.py" in paths_text
    assert "kr_discovery_http_client.py" not in paths_text


def test_existing_3g3_4a_tests_remain_importable() -> None:
    import test_kr_discovery_live_client  # noqa: F401


def test_known_http_error_cli_has_no_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from data.kr_discovery_http_client import KrDiscoveryHttpError
    from run_kr_discovery_live_smoke import main

    def _raise_fetch(**kwargs: object) -> dict[str, object]:
        raise KrDiscoveryHttpError("fetch", "discovery HTTP request failed: URLError: network down")

    monkeypatch.setattr("run_kr_discovery_live_smoke.fetch_kr_discovery_http_payload", _raise_fetch)
    argv = _base_cli_args(tmp_path) + ["--json"]
    assert main(argv) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
