"""Real Intake 3G4-5 — operator-triggered KR factor source live smoke tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_factors"
    / "raw_kr_factor_source_synthetic_success.json"
)
POOL_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_candidates"
    / "kr_sector_candidate_pool.synthetic.toml"
)
SYNTHETIC_CORP_CODE_XML = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "corp_code_synthetic_multi.xml"
)
OPS_SCRIPT = REPO_ROOT / "ops" / "run_kr_factor_source_live_smoke.py"
HTTP_CLIENT_SOURCE = REPO_ROOT / "src" / "data" / "kr_factor_source_http_client.py"
SNAPSHOT_SOURCE = REPO_ROOT / "src" / "data" / "kr_factor_source_payload_snapshot.py"
STATIC_SCAN_PATHS = REPO_ROOT / "tests" / "test_fetch_research_sources.py"

KST = timezone(timedelta(hours=9))
FETCHED_AT = datetime(2026, 5, 30, 12, 0, 0, tzinfo=KST)
SECRET = "SECRET_VALUE_TEST"
ENDPOINT_URL = "https://example.test/factor-source.json"
ENDPOINT_URL_WITH_SECRET = f"https://example.test/factor/path?token={SECRET}"

_FORBIDDEN_OUTPUT_FIELDS = frozenset(
    {
        "action",
        "side",
        "buy",
        "sell",
        "hold",
        "target_weight",
        "target_allocation",
        "targetAllocation",
        "quantity",
        "order",
        "order_type",
        "price_target",
        "stop_loss",
        "take_profit",
        "corp_code",
        "corpCode",
        "yfinance_provider_symbol",
        "provider_symbol",
        "providerSymbol",
        "stockProviderSymbol",
        "source_key",
        "source_format",
        "external_service",
        "universe_hint",
        "displayName",
        "sectorCode",
        "lastUpdated",
    }
)

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from build_kr_factor_bundle_mapping import run_build_kr_factor_bundle_mapping
from build_kr_factor_ranked_mapping import run_build_kr_factor_ranked_mapping
from data.kr_factor_signal_generator import (
    generate_ranking_signals_from_factors,
    load_kr_factor_inputs_toml,
)
from data.kr_factor_source_adapter import KrFactorSourceAdapterError, load_kr_factor_source_payload
from data.kr_factor_source_http_client import (
    KrFactorSourceHttpError,
    fetch_kr_factor_source_http_payload,
    redact_factor_source_http_text,
    sanitize_factor_source_http_failure,
)
from data.kr_factor_source_payload_snapshot import (
    KrFactorSourceSnapshotError,
    snapshot_filename_for_factor_source_payload,
    write_immutable_factor_source_snapshot,
)
from run_kr_factor_source_live_smoke import KrFactorSourceLiveSmokeError, run_kr_factor_source_live_smoke


def _source_payload() -> dict[str, Any]:
    return json.loads(SOURCE_FIXTURE.read_text(encoding="utf-8"))


def _fake_urlopen(*, body: bytes | None = None, raises: Exception | None = None) -> object:
    resolved_body = json.dumps(_source_payload()).encode("utf-8") if body is None else body

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


def _runner_args(tmp_path: Path, *, factor_inputs_out: Path | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {
        "endpoint_url": ENDPOINT_URL,
        "snapshot_dir": tmp_path / "snapshots",
        "fetched_at": FETCHED_AT,
        "timeout_seconds": 15.0,
        "urlopen_fn": _fake_urlopen(),
    }
    if factor_inputs_out is not None:
        args["factor_inputs_out"] = factor_inputs_out
        args["output_name"] = "kr-factor-inputs-live-smoke-v1"
        args["output_description"] = "Operator-triggered KR factor source live smoke."
        args["factor_score_version"] = "kr-factor-live-smoke-v1"
    return args


def _run_smoke(tmp_path: Path, **overrides: object) -> dict[str, Any]:
    args = _runner_args(tmp_path, factor_inputs_out=overrides.pop("factor_inputs_out", None))
    args.update(overrides)
    return run_kr_factor_source_live_smoke(**args)  # type: ignore[arg-type]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
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
    ]


def _walk_forbidden_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            assert key not in _FORBIDDEN_OUTPUT_FIELDS
            _walk_forbidden_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk_forbidden_fields(nested)


# --- HTTP client (1–10) ---


def test_http_client_fetch_success_returns_json_object() -> None:
    payload = fetch_kr_factor_source_http_payload(ENDPOINT_URL, 15.0, urlopen_fn=_fake_urlopen())
    assert isinstance(payload, dict)
    assert payload["source_key"] == "kr_factor_source"


def test_http_client_rejects_non_http_https_scheme() -> None:
    with pytest.raises(KrFactorSourceHttpError) as exc_info:
        fetch_kr_factor_source_http_payload("ftp://example.test/x.json", 15.0)
    assert exc_info.value.stage == "fetch"


def test_http_client_network_failure_is_fetch_stage() -> None:
    with pytest.raises(KrFactorSourceHttpError) as exc_info:
        fetch_kr_factor_source_http_payload(
            ENDPOINT_URL,
            15.0,
            urlopen_fn=_fake_urlopen(raises=URLError("network down")),
        )
    assert exc_info.value.stage == "fetch"


def test_http_client_invalid_json_is_parse_stage() -> None:
    with pytest.raises(KrFactorSourceHttpError) as exc_info:
        fetch_kr_factor_source_http_payload(
            ENDPOINT_URL,
            15.0,
            urlopen_fn=_fake_urlopen(body=b"not-json"),
        )
    assert exc_info.value.stage == "parse"


def test_http_client_non_object_json_is_parse_stage() -> None:
    with pytest.raises(KrFactorSourceHttpError) as exc_info:
        fetch_kr_factor_source_http_payload(
            ENDPOINT_URL,
            15.0,
            urlopen_fn=_fake_urlopen(body=json.dumps([1, 2]).encode("utf-8")),
        )
    assert exc_info.value.stage == "parse"


def test_http_error_redacts_full_endpoint_url() -> None:
    url = ENDPOINT_URL_WITH_SECRET
    with pytest.raises(KrFactorSourceHttpError) as exc_info:
        fetch_kr_factor_source_http_payload(
            url,
            15.0,
            urlopen_fn=_fake_urlopen(raises=URLError(f"open failed for {url}")),
        )
    msg = exc_info.value.safe_message
    assert "example.test" not in msg
    assert "/factor/path" not in msg
    assert SECRET not in msg
    assert "token=" not in msg


def test_http_error_redacts_query_string_wholesale() -> None:
    redacted = redact_factor_source_http_text(ENDPOINT_URL_WITH_SECRET, extra_secret_values=(SECRET,))
    assert SECRET not in redacted
    assert "?<redacted>" in redacted


def test_http_error_redacts_credential_patterns() -> None:
    message = sanitize_factor_source_http_failure(
        RuntimeError(f"failed api_key={SECRET}"),
        endpoint_url=ENDPOINT_URL,
    )
    assert SECRET not in message
    assert "api_key=<redacted>" in message


def test_http_error_redacts_non_enumerated_query_by_removing_whole_query() -> None:
    redacted = redact_factor_source_http_text(
        "request failed for https://example.test/path?session=SECRET&x=1",
        extra_secret_values=("SECRET",),
    )
    assert "SECRET" not in redacted
    assert "https://example.test/path?<redacted>" in redacted


def test_http_error_raises_from_none() -> None:
    with pytest.raises(KrFactorSourceHttpError) as exc_info:
        fetch_kr_factor_source_http_payload(
            ENDPOINT_URL,
            15.0,
            urlopen_fn=_fake_urlopen(raises=URLError(f"open failed api_key={SECRET}")),
        )
    assert exc_info.value.__cause__ is None
    assert SECRET not in exc_info.value.safe_message


# --- live smoke JSON (11–12) ---


def test_success_json_never_includes_endpoint_url(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path, endpoint_url=ENDPOINT_URL_WITH_SECRET)
    serialized = json.dumps(payload)
    assert "endpoint_url" not in payload
    assert ENDPOINT_URL_WITH_SECRET not in serialized


def test_error_json_never_includes_endpoint_url_or_query(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from run_kr_factor_source_live_smoke import main

    def _patched_fetch(endpoint_url: str, timeout_seconds: float, urlopen_fn: object = None) -> dict[str, Any]:
        return fetch_kr_factor_source_http_payload(
            endpoint_url,
            timeout_seconds,
            urlopen_fn=_fake_urlopen(raises=URLError(f"open failed {ENDPOINT_URL_WITH_SECRET}")),
        )

    with patch(
        "run_kr_factor_source_live_smoke.fetch_kr_factor_source_http_payload",
        side_effect=_patched_fetch,
    ):
        assert main(_base_cli_args(tmp_path) + ["--endpoint-url", ENDPOINT_URL_WITH_SECRET, "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "error"
    assert ENDPOINT_URL_WITH_SECRET not in captured.out
    assert SECRET not in payload["error"]


# --- snapshot (13–20) ---


def test_snapshot_filename_has_raw_factor_source_prefix_and_sha8(tmp_path: Path) -> None:
    payload = _source_payload()
    filename = snapshot_filename_for_factor_source_payload(payload, fetched_at=FETCHED_AT)
    assert filename.startswith("raw_factor_source_")
    assert filename.endswith(".json")
    assert len(filename.split("_")[-1].replace(".json", "")) == 8


def test_snapshot_write_validates_through_3g4_4_parser(tmp_path: Path) -> None:
    path = write_immutable_factor_source_snapshot(
        _source_payload(),
        tmp_path / "snapshots",
        fetched_at=FETCHED_AT,
    )
    loaded = load_kr_factor_source_payload(path)
    assert loaded.source_key == "kr_factor_source"


def test_snapshot_content_is_raw_payload_not_wrapper(tmp_path: Path) -> None:
    source = _source_payload()
    path = write_immutable_factor_source_snapshot(source, tmp_path / "snapshots", fetched_at=FETCHED_AT)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == source
    assert set(on_disk.keys()) == set(source.keys())
    assert "payload" not in on_disk


def test_invalid_payload_does_not_create_final_snapshot(tmp_path: Path) -> None:
    bad = _source_payload()
    bad["source_format"] = "wrong_format"
    snapshot_dir = tmp_path / "snapshots"
    with pytest.raises(Exception):
        write_immutable_factor_source_snapshot(bad, snapshot_dir, fetched_at=FETCHED_AT)
    finals = list(snapshot_dir.glob("raw_factor_source_*.json"))
    assert finals == []


def test_invalid_payload_temp_file_cleaned_up(tmp_path: Path) -> None:
    bad = _source_payload()
    bad.pop("items")
    snapshot_dir = tmp_path / "snapshots"
    with pytest.raises(Exception):
        write_immutable_factor_source_snapshot(bad, snapshot_dir, fetched_at=FETCHED_AT)
    assert list(snapshot_dir.glob(".tmp_factor_source_*.json")) == []


def test_identical_payload_and_fetched_at_collision_fails_at_snapshot(tmp_path: Path) -> None:
    payload = _source_payload()
    snapshot_dir = tmp_path / "snapshots"
    write_immutable_factor_source_snapshot(payload, snapshot_dir, fetched_at=FETCHED_AT)
    with pytest.raises(FileExistsError):
        write_immutable_factor_source_snapshot(payload, snapshot_dir, fetched_at=FETCHED_AT)


def test_force_does_not_overwrite_raw_snapshot(tmp_path: Path) -> None:
    _run_smoke(tmp_path, force=True)
    with pytest.raises(KrFactorSourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, force=True)
    assert exc_info.value.stage == "snapshot"
    assert "already exists" in exc_info.value.message


def test_different_payload_produces_different_snapshot_hash(tmp_path: Path) -> None:
    first = write_immutable_factor_source_snapshot(
        _source_payload(),
        tmp_path / "a",
        fetched_at=FETCHED_AT,
    )
    other = _source_payload()
    other["universe_hint"] = "different-universe-hint-value"
    second = write_immutable_factor_source_snapshot(other, tmp_path / "b", fetched_at=FETCHED_AT)
    assert first.name != second.name


# --- ops args (21–23) ---


def test_naive_fetched_at_rejected_at_args(tmp_path: Path) -> None:
    with pytest.raises(KrFactorSourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, fetched_at=datetime(2026, 5, 30, 12, 0, 0))
    assert exc_info.value.stage == "args"


def test_timeout_zero_rejected_at_args(tmp_path: Path) -> None:
    with pytest.raises(KrFactorSourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, timeout_seconds=0)
    assert exc_info.value.stage == "args"


def test_invalid_endpoint_scheme_rejected_at_args_by_ops(tmp_path: Path) -> None:
    with pytest.raises(KrFactorSourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, endpoint_url="file:///tmp/x.json")
    assert exc_info.value.stage == "args"


# --- live smoke flow (24–32) ---


def test_live_smoke_success_without_replay_writes_only_raw_snapshot(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    assert payload["replayed"] is False
    assert Path(str(payload["snapshot_path"])).is_file()
    assert list(tmp_path.rglob("*.toml")) == []


def test_live_smoke_success_with_replay_writes_snapshot_and_factor_inputs(tmp_path: Path) -> None:
    factor_out = tmp_path / "factor_inputs.toml"
    payload = _run_smoke(tmp_path, factor_inputs_out=factor_out, force=True)
    assert payload["replayed"] is True
    assert Path(str(payload["snapshot_path"])).is_file()
    assert factor_out.is_file()


def test_replay_output_loads_through_load_kr_factor_inputs_toml(tmp_path: Path) -> None:
    factor_out = tmp_path / "factor_inputs.toml"
    _run_smoke(tmp_path, factor_inputs_out=factor_out, force=True)
    loaded = load_kr_factor_inputs_toml(factor_out)
    assert len(loaded.factors) == 5


def test_replay_output_feeds_3g4_1_signal_generator(tmp_path: Path) -> None:
    factor_out = tmp_path / "factor_inputs.toml"
    _run_smoke(tmp_path, factor_inputs_out=factor_out, force=True)
    factor_inputs = load_kr_factor_inputs_toml(factor_out)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="kr-factor-signals-live-v1",
        output_description="live smoke signals",
    )
    assert len(signal_set.signals) == 5


def test_replay_output_feeds_3g4_2_ranked_mapping(tmp_path: Path) -> None:
    factor_out = tmp_path / "factor_inputs.toml"
    _run_smoke(tmp_path, factor_inputs_out=factor_out, force=True)
    result = run_build_kr_factor_ranked_mapping(
        candidate_pool_path=POOL_FIXTURE,
        factor_inputs_path=factor_out,
        corp_code_xml=SYNTHETIC_CORP_CODE_XML,
        corp_code_zip=None,
        factor_signals_out=tmp_path / "signals.toml",
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=tmp_path / "selected.toml",
        universe_out=tmp_path / "universe.toml",
        provider_mapping_out=tmp_path / "mapping.toml",
        factor_output_name="kr-factor-signals-live-v1",
        factor_output_description="live smoke",
        selection_name="sel-v1",
        selection_description="sel",
        universe_name="uni-v1",
        provider_mapping_name="map-v1",
        top_n=3,
        force=True,
    )
    assert result["status"] == "ok"


def test_replay_output_feeds_3g4_3_bundle_when_copied_to_temp_bundle(tmp_path: Path) -> None:
    factor_out = tmp_path / "factor_inputs.toml"
    _run_smoke(tmp_path, factor_inputs_out=factor_out, force=True)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    factor_inputs_copy = bundle_dir / "factor_inputs.generated.toml"
    factor_inputs_copy.write_text(factor_out.read_text(encoding="utf-8"), encoding="utf-8")
    bundle_path = bundle_dir / "bundle.toml"
    bundle_path.write_text(
        f"""
version = 1
name = "kr-factor-bundle-live-smoke-v1"
description = "Bundle using live-smoke mapped factor inputs."
base_market = "KR"

[inputs]
candidate_pool = "{POOL_FIXTURE.as_posix()}"
factor_inputs = "{factor_inputs_copy.name}"
corp_code_xml = "{SYNTHETIC_CORP_CODE_XML.as_posix()}"

[outputs]
factor_signals_out = "outputs/factor_signals.generated.toml"
ranked_out = "outputs/ranked.json"
selected_candidates_out = "outputs/selected.toml"
universe_out = "outputs/universe.generated.toml"
provider_mapping_out = "outputs/provider_mappings.generated.toml"

[names]
factor_output_name = "kr-factor-signals-live-v1"
factor_output_description = "live smoke signals"
selection_name = "sel-v1"
selection_description = "sel"
universe_name = "uni-v1"
provider_mapping_name = "map-v1"

[selection]
top_n = 3
""".strip()
        + "\n",
        encoding="utf-8",
    )
    result = run_build_kr_factor_bundle_mapping(
        bundle_path=bundle_path,
        out_dir=tmp_path / "bundle_out",
        force=True,
    )
    assert result["status"] == "ok"


def test_replay_output_exists_without_force_fails_at_write(tmp_path: Path) -> None:
    factor_out = tmp_path / "factor_inputs.toml"
    _run_smoke(tmp_path, factor_inputs_out=factor_out, force=True)
    later_fetched_at = FETCHED_AT + timedelta(seconds=1)
    with pytest.raises(KrFactorSourceLiveSmokeError) as exc_info:
        _run_smoke(
            tmp_path,
            factor_inputs_out=factor_out,
            fetched_at=later_fetched_at,
            force=False,
        )
    assert exc_info.value.stage == "write"


def test_replay_parse_failure_maps_to_replay_stage() -> None:
    from run_kr_factor_source_live_smoke import _map_adapter_error

    with pytest.raises(KrFactorSourceLiveSmokeError) as exc_info:
        raise _map_adapter_error(KrFactorSourceAdapterError("parse", "unknown factor source root fields: extra"))
    assert exc_info.value.stage == "replay"


def test_replay_map_failure_maps_to_replay_stage(tmp_path: Path) -> None:
    from run_kr_factor_source_live_smoke import _map_adapter_error

    raw = _source_payload()
    raw["items"][1]["ticker"] = raw["items"][0]["ticker"]
    snapshot_path = write_immutable_factor_source_snapshot(raw, tmp_path / "snap", fetched_at=FETCHED_AT)
    with pytest.raises(KrFactorSourceLiveSmokeError) as exc_info:
        try:
            from data.kr_factor_source_adapter import replay_kr_factor_source_payload

            replay_kr_factor_source_payload(
                source_path=snapshot_path,
                factor_inputs_out=tmp_path / "out.toml",
                output_name="n",
                factor_score_version="v",
                force=True,
            )
        except KrFactorSourceAdapterError as exc:
            raise _map_adapter_error(exc)
    assert exc_info.value.stage == "replay"


def test_replay_self_validation_failure_maps_to_validate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    factor_out = tmp_path / "factor_inputs.toml"

    def _failing_replay(**_kwargs: object) -> dict[str, Any]:
        raise KrFactorSourceAdapterError("validate", "factor input self-validation failed")

    monkeypatch.setattr("run_kr_factor_source_live_smoke.replay_kr_factor_source_payload", _failing_replay)
    with pytest.raises(KrFactorSourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, factor_inputs_out=factor_out, force=True)
    assert exc_info.value.stage == "validate"


# --- snapshot safety fields (33–39) ---


def test_raw_snapshot_has_no_root_request(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    raw = json.loads(Path(str(payload["snapshot_path"])).read_text(encoding="utf-8"))
    assert "request" not in raw


def test_raw_snapshot_has_no_endpoint_url(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path, endpoint_url=ENDPOINT_URL_WITH_SECRET)
    text = Path(str(payload["snapshot_path"])).read_text(encoding="utf-8")
    assert "endpoint_url" not in text


def test_raw_snapshot_has_no_env_api_key_names(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    lowered = Path(str(payload["snapshot_path"])).read_text(encoding="utf-8").lower()
    for marker in ("fred_api_key", "dart_api_key", "kis_api", "os.environ", "getenv"):
        assert marker not in lowered


def test_raw_snapshot_has_no_corp_code(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    text = Path(str(payload["snapshot_path"])).read_text(encoding="utf-8").lower()
    assert "corp_code" not in text
    assert "corpcode" not in text


def test_raw_snapshot_has_no_trading_fields(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    text = Path(str(payload["snapshot_path"])).read_text(encoding="utf-8").lower()
    for token in ("submit_order", "paperloop", "target_weight", '"action"'):
        assert token not in text


def test_optional_replay_output_has_no_source_only_fields(tmp_path: Path) -> None:
    factor_out = tmp_path / "factor_inputs.toml"
    _run_smoke(tmp_path, factor_inputs_out=factor_out, force=True)
    doc = tomllib.loads(factor_out.read_text(encoding="utf-8"))
    _walk_forbidden_fields(doc)


def test_optional_replay_output_has_no_corp_code_provider_universe_fields(tmp_path: Path) -> None:
    factor_out = tmp_path / "factor_inputs.toml"
    _run_smoke(tmp_path, factor_inputs_out=factor_out, force=True)
    text = factor_out.read_text(encoding="utf-8").lower()
    for token in ("corp_code", "yfinance", "provider_mapping", "universe", "provider_symbol"):
        assert token not in text


# --- static isolation (40–45) ---


def test_ops_file_does_not_import_urllib() -> None:
    text = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    assert "urllib.request" not in text
    assert "urllib.error" not in text


def test_snapshot_module_does_not_import_urllib() -> None:
    text = SNAPSHOT_SOURCE.read_text(encoding="utf-8").lower()
    assert "urllib.request" not in text
    assert "urllib.parse" not in text
    assert "urllib.error" not in text


def test_http_client_is_only_new_file_with_urllib() -> None:
    new_files = [
        HTTP_CLIENT_SOURCE,
        SNAPSHOT_SOURCE,
        OPS_SCRIPT,
    ]
    urllib_files = [
        path
        for path in new_files
        if "urllib.request" in path.read_text(encoding="utf-8")
    ]
    assert urllib_files == [HTTP_CLIENT_SOURCE]


def test_new_files_have_no_env_api_key_read() -> None:
    for path in (HTTP_CLIENT_SOURCE, SNAPSHOT_SOURCE, OPS_SCRIPT):
        lowered = path.read_text(encoding="utf-8").lower()
        assert "os.environ" not in lowered
        assert "getenv" not in lowered


def test_static_scan_includes_ops_and_snapshot_not_http_client() -> None:
    text = STATIC_SCAN_PATHS.read_text(encoding="utf-8")
    assert "run_kr_factor_source_live_smoke.py" in text
    assert "kr_factor_source_payload_snapshot.py" in text
    paths_section = text.split("paths = [", 1)[1].split("]", 1)[0]
    assert "kr_factor_source_http_client.py" not in paths_section


def test_no_broker_paperloop_kis_in_new_files() -> None:
    for path in (HTTP_CLIENT_SOURCE, SNAPSHOT_SOURCE, OPS_SCRIPT):
        lowered = path.read_text(encoding="utf-8").lower()
        for token in ("kis", "paperbroker", "paperlooprunner", "submit_order"):
            assert token not in lowered


# --- runtime / regression (46–49) ---


def test_tests_use_tmp_path_not_runtime(tmp_path: Path) -> None:
    _run_smoke(tmp_path)
    assert "runtime" not in str(tmp_path)


def test_git_ls_files_runtime_remains_empty() -> None:
    result = subprocess.run(
        ["git", "ls-files", "runtime"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.stdout.strip() == ""


def test_existing_3g4_4_tests_remain_green() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_kr_factor_source_adapter.py", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_known_error_cli_has_no_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from run_kr_factor_source_live_smoke import main

    with patch(
        "run_kr_factor_source_live_smoke.fetch_kr_factor_source_http_payload",
        side_effect=KrFactorSourceHttpError("fetch", "factor source HTTP request failed: URLError: down"),
    ):
        assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


# --- 3G4-H1 hardening (50+) ---


def test_snapshot_naive_fetched_at_raises_snapshot_stage_not_bare_value_error(tmp_path: Path) -> None:
    with pytest.raises(KrFactorSourceSnapshotError) as exc_info:
        write_immutable_factor_source_snapshot(
            _source_payload(),
            tmp_path / "snapshots",
            fetched_at=datetime(2026, 5, 30, 12, 0, 0),
        )
    assert exc_info.value.stage == "snapshot"
    assert "fetched_at must be a timezone-aware datetime" in exc_info.value.message


def test_snapshot_unexpected_write_failure_sanitizes_message(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshots"
    secret_path = "/raw/path/with/SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret_path)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrFactorSourceSnapshotError) as exc_info:
            write_immutable_factor_source_snapshot(
                _source_payload(),
                snapshot_dir,
                fetched_at=FETCHED_AT,
            )
    assert exc_info.value.stage == "snapshot"
    assert exc_info.value.message == "factor source snapshot write failed: PermissionError"
    assert secret_path not in exc_info.value.message
    assert "SECRET_VALUE_TEST" not in exc_info.value.message
    assert exc_info.value.__cause__ is None


def test_snapshot_unexpected_rename_failure_sanitizes_message(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshots"
    secret = "SECRET_VALUE_TEST"

    original_rename = Path.rename

    def _raise_os_error(self: Path, target: Path) -> None:
        if self.name.startswith(".tmp_factor_source_"):
            raise OSError(f"rename blocked for {secret}")
        original_rename(self, target)

    with patch.object(Path, "rename", _raise_os_error):
        with pytest.raises(KrFactorSourceSnapshotError) as exc_info:
            write_immutable_factor_source_snapshot(
                _source_payload(),
                snapshot_dir,
                fetched_at=FETCHED_AT,
            )
    assert exc_info.value.stage == "snapshot"
    assert exc_info.value.message == "factor source snapshot write failed: OSError"
    assert secret not in exc_info.value.message
    assert exc_info.value.__cause__ is None


def test_snapshot_explicit_validation_failure_remains_useful(tmp_path: Path) -> None:
    bad = _source_payload()
    bad["source_format"] = "wrong_format"
    with pytest.raises(KrFactorSourceSnapshotError) as exc_info:
        write_immutable_factor_source_snapshot(bad, tmp_path / "snapshots", fetched_at=FETCHED_AT)
    assert exc_info.value.stage == "snapshot"
    assert "source_format must be" in exc_info.value.message


def test_snapshot_collision_behavior_unchanged(tmp_path: Path) -> None:
    payload = _source_payload()
    snapshot_dir = tmp_path / "snapshots"
    write_immutable_factor_source_snapshot(payload, snapshot_dir, fetched_at=FETCHED_AT)
    with pytest.raises(FileExistsError) as exc_info:
        write_immutable_factor_source_snapshot(payload, snapshot_dir, fetched_at=FETCHED_AT)
    assert "already exists" in str(exc_info.value)


def test_snapshot_raw_no_wrap_behavior_unchanged(tmp_path: Path) -> None:
    source = _source_payload()
    path = write_immutable_factor_source_snapshot(source, tmp_path / "snapshots", fetched_at=FETCHED_AT)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == source
    assert "payload" not in on_disk
