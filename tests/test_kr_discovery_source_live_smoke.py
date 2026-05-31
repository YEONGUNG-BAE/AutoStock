"""Real Intake 3G3-6 — operator-triggered source-specific KR discovery live endpoint adapter."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import URLError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_discovery"
    / "source_payload_synthetic_provider_v1.json"
)
SIGNALS_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_candidates"
    / "kr_ranking_signals.synthetic.toml"
)
SYNTHETIC_CORP_CODE_XML = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "corp_code_synthetic_multi.xml"
)
OPS_SCRIPT = REPO_ROOT / "ops" / "run_kr_discovery_source_live_smoke.py"

KST = timezone(timedelta(hours=9))
FETCHED_AT = datetime(2026, 5, 30, 12, 0, 0, tzinfo=KST)
AS_OF = FETCHED_AT
SECRET = "SECRET_VALUE_TEST"
ENDPOINT_URL = "https://example.test/synthetic-provider-v1.json"
ENDPOINT_URL_WITH_SECRET = f"https://example.test/synthetic-provider-v1.json?session={SECRET}"

_CANONICAL_RECORD_KEYS = frozenset(
    {
        "symbol",
        "market",
        "display_name",
        "stock_code",
        "corp_name",
        "yfinance_provider_symbol",
        "currency",
        "sector",
        "industry",
        "enabled",
        "eligible",
        "priority",
        "notes",
        "source_timestamp",
        "source_url",
    }
)
_SNAPSHOT_ROOT_KEYS = frozenset(
    {
        "source_key",
        "external_service",
        "snapshot_version",
        "fetched_at",
        "as_of",
        "market",
        "universe_hint",
        "records",
    }
)

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data.kr_candidate_pool import parse_kr_candidate_pool_toml, select_candidates
from data.kr_candidate_ranker import rank_kr_candidates
from data.kr_discovery_schema_mapper import parse_synthetic_provider_payload_mapping
from data.kr_discovery_source_adapter import load_kr_discovery_snapshot
from data.kr_discovery_source_payload_snapshot import source_payload_snapshot_filename
from data.kr_provider_mapping_generator import generate_kr_provider_mapping_files
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml
from run_kr_discovery_source_live_smoke import (
    KrDiscoverySourceLiveSmokeError,
    run_kr_discovery_source_live_smoke,
)


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


def _runner_args(tmp_path: Path, *, candidate_pool_out: Path | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {
        "endpoint_url": ENDPOINT_URL,
        "source_snapshot_dir": tmp_path / "source_snapshots",
        "canonical_snapshot_dir": tmp_path / "canonical_snapshots",
        "fetched_at": FETCHED_AT,
        "as_of": AS_OF,
        "universe_hint": "synthetic-provider-v1-live-smoke",
        "external_service": "synthetic-provider-live-endpoint",
        "timeout_seconds": 15.0,
        "urlopen_fn": _fake_urlopen(),
    }
    if candidate_pool_out is not None:
        args["candidate_pool_out"] = candidate_pool_out
        args["pool_name"] = "kr-discovery-source-live-pool-v1"
        args["pool_description"] = "Operator-triggered source-specific KR discovery live smoke replay."
    return args


def _run_smoke(tmp_path: Path, **overrides: object) -> dict[str, Any]:
    args = _runner_args(tmp_path, candidate_pool_out=overrides.pop("candidate_pool_out", None))
    args.update(overrides)
    return run_kr_discovery_source_live_smoke(**args)  # type: ignore[arg-type]


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
        "--source-snapshot-dir",
        str(tmp_path / "source_snapshots"),
        "--canonical-snapshot-dir",
        str(tmp_path / "canonical_snapshots"),
        "--fetched-at",
        FETCHED_AT.isoformat(),
        "--as-of",
        AS_OF.isoformat(),
        "--universe-hint",
        "synthetic-provider-v1-live-smoke",
        "--external-service",
        "synthetic-provider-live-endpoint",
    ]


def test_fake_urlopen_source_specific_payload_success(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    assert Path(str(payload["source_snapshot_path"])).is_file()
    assert Path(str(payload["canonical_snapshot_path"])).is_file()
    assert payload["records_count"] == 5


def test_http_fetch_failure_surfaces_as_fetch_stage(tmp_path: Path) -> None:
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, urlopen_fn=_fake_urlopen(raises=URLError("network down")))
    assert exc_info.value.stage == "fetch"


def test_http_invalid_json_surfaces_as_parse_stage(tmp_path: Path) -> None:
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, urlopen_fn=_fake_urlopen(body=b"not-json"))
    assert exc_info.value.stage == "parse"


def test_invalid_source_format_surfaces_as_map_stage(tmp_path: Path) -> None:
    raw = _source_payload()
    raw["source_format"] = "other-format"
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, urlopen_fn=_fake_urlopen(body=json.dumps(raw).encode("utf-8")))
    assert exc_info.value.stage == "map"


def test_mapper_local_parse_errors_remap_to_cli_map_stage(tmp_path: Path) -> None:
    raw = _source_payload()
    raw["market"] = "US"
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, urlopen_fn=_fake_urlopen(body=json.dumps(raw).encode("utf-8")))
    assert exc_info.value.stage == "map"
    assert "market must be 'KR'" in exc_info.value.message


def test_unknown_sector_code_surfaces_as_map_stage(tmp_path: Path) -> None:
    raw = _source_payload()
    raw["items"][0]["sectorCode"] = "UNKNOWN_SECTOR"
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, urlopen_fn=_fake_urlopen(body=json.dumps(raw).encode("utf-8")))
    assert exc_info.value.stage == "map"


def test_source_payload_snapshot_written_before_canonical_snapshot(tmp_path: Path) -> None:
    call_order: list[str] = []
    real_write = __import__(
        "data.kr_discovery_source_payload_snapshot",
        fromlist=["write_source_payload_snapshot"],
    ).write_source_payload_snapshot
    real_fetch = __import__(
        "data.kr_discovery_live_client",
        fromlist=["fetch_live_kr_discovery_snapshot"],
    ).fetch_live_kr_discovery_snapshot

    def _track_write(**kwargs: object) -> Path:
        call_order.append("source_snapshot")
        return real_write(**kwargs)  # type: ignore[arg-type]

    def _track_fetch(**kwargs: object) -> Path:
        call_order.append("canonical_snapshot")
        return real_fetch(**kwargs)  # type: ignore[arg-type]

    with patch(
        "run_kr_discovery_source_live_smoke.write_source_payload_snapshot",
        side_effect=_track_write,
    ), patch(
        "run_kr_discovery_source_live_smoke.fetch_live_kr_discovery_snapshot",
        side_effect=_track_fetch,
    ):
        _run_smoke(tmp_path)
    assert call_order == ["source_snapshot", "canonical_snapshot"]


def test_source_payload_snapshot_filename_uses_raw_source_prefix(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    source_path = Path(str(payload["source_snapshot_path"]))
    assert source_path.name.startswith("raw_source_")


def test_source_payload_snapshot_filename_is_content_hash_based(tmp_path: Path) -> None:
    raw = _source_payload()
    wrapper = {
        "source_key": "kr_discovery_source_payload",
        "snapshot_version": 1,
        "external_service": "synthetic-provider-live-endpoint",
        "source_format": "synthetic-provider-v1",
        "fetched_at": FETCHED_AT.isoformat(),
        "payload": raw,
    }
    expected_name = source_payload_snapshot_filename(wrapper, fetched_at=FETCHED_AT)
    payload = _run_smoke(tmp_path)
    assert Path(str(payload["source_snapshot_path"])).name == expected_name


def test_source_payload_snapshot_collision_raises_and_is_not_overwritten(tmp_path: Path) -> None:
    _run_smoke(tmp_path)
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path)
    assert exc_info.value.stage == "source_snapshot"
    assert "already exists" in exc_info.value.message


def test_canonical_snapshot_collision_raises_and_is_not_overwritten(tmp_path: Path) -> None:
    canonical_dir = tmp_path / "canonical_snapshots"
    _run_smoke(tmp_path, canonical_snapshot_dir=canonical_dir)
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(
            tmp_path,
            source_snapshot_dir=tmp_path / "source_snapshots_alt",
            canonical_snapshot_dir=canonical_dir,
        )
    assert exc_info.value.stage == "canonical_snapshot"
    assert "already exists" in exc_info.value.message


def test_force_does_not_overwrite_source_snapshot(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    source_path = Path(str(payload["source_snapshot_path"]))
    original_bytes = source_path.read_bytes()
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, force=True)
    assert exc_info.value.stage == "source_snapshot"
    assert source_path.read_bytes() == original_bytes


def test_force_does_not_overwrite_canonical_snapshot(tmp_path: Path) -> None:
    source_dir = tmp_path / "source_a"
    canonical_dir = tmp_path / "canonical"
    payload = _run_smoke(
        tmp_path,
        source_snapshot_dir=source_dir,
        canonical_snapshot_dir=canonical_dir,
    )
    canonical_path = Path(str(payload["canonical_snapshot_path"]))
    original_bytes = canonical_path.read_bytes()
    alt_source_dir = tmp_path / "source_b"
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(
            tmp_path,
            source_snapshot_dir=alt_source_dir,
            canonical_snapshot_dir=canonical_dir,
            force=True,
        )
    assert exc_info.value.stage == "canonical_snapshot"
    assert canonical_path.read_bytes() == original_bytes


def test_force_only_overwrites_candidate_pool_output(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    first = _run_smoke(tmp_path, candidate_pool_out=pool_out, force=True)
    source_path = Path(str(first["source_snapshot_path"]))
    canonical_path = Path(str(first["canonical_snapshot_path"]))
    source_bytes = source_path.read_bytes()
    canonical_bytes = canonical_path.read_bytes()

    pool_out.write_text("placeholder\n", encoding="utf-8")
    second = _run_smoke(
        tmp_path,
        source_snapshot_dir=tmp_path / "source_alt",
        canonical_snapshot_dir=tmp_path / "canonical_alt",
        candidate_pool_out=pool_out,
        external_service="synthetic-provider-live-endpoint-alt",
        force=True,
    )

    assert source_path.read_bytes() == source_bytes
    assert canonical_path.read_bytes() == canonical_bytes
    assert Path(str(second["candidate_pool_out"])).is_file()
    assert "placeholder" not in pool_out.read_text(encoding="utf-8")


def test_source_snapshot_contains_no_endpoint_url(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path, endpoint_url=ENDPOINT_URL_WITH_SECRET)
    source = json.loads(Path(str(payload["source_snapshot_path"])).read_text(encoding="utf-8"))
    assert "endpoint_url" not in source
    assert "endpoint_url" not in json.dumps(source)


def test_source_snapshot_contains_no_request_field(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    source = json.loads(Path(str(payload["source_snapshot_path"])).read_text(encoding="utf-8"))
    assert "request" not in source


def test_source_snapshot_contains_no_env_or_api_key_fields(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path, endpoint_url=ENDPOINT_URL_WITH_SECRET)
    text = Path(str(payload["source_snapshot_path"])).read_text(encoding="utf-8")
    assert SECRET not in text
    assert "api_key" not in text.lower()


def test_broad_forbidden_validation_is_key_based_not_value_substring(tmp_path: Path) -> None:
    raw = _source_payload()
    raw["items"][0]["note"] = "Market order hold position review"
    raw["items"][0]["displayName"] = "SYNTH Alpha Display order hold"
    payload = _run_smoke(tmp_path, urlopen_fn=_fake_urlopen(body=json.dumps(raw).encode("utf-8")))
    source = json.loads(Path(str(payload["source_snapshot_path"])).read_text(encoding="utf-8"))
    assert "order hold" in source["payload"]["items"][0]["note"]


def test_credential_like_forbidden_keys_are_rejected(tmp_path: Path) -> None:
    raw = _source_payload()
    raw["api_key"] = "secret-value"
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, urlopen_fn=_fake_urlopen(body=json.dumps(raw).encode("utf-8")))
    assert exc_info.value.stage == "source_snapshot"


def test_success_json_contains_no_endpoint_url(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path, endpoint_url=ENDPOINT_URL_WITH_SECRET)
    serialized = json.dumps(payload)
    assert "endpoint_url" not in payload
    assert ENDPOINT_URL_WITH_SECRET not in serialized


def test_secret_query_endpoint_success_path_does_not_expose_secret(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path, endpoint_url=ENDPOINT_URL_WITH_SECRET)
    source_text = Path(str(payload["source_snapshot_path"])).read_text(encoding="utf-8")
    canonical_text = Path(str(payload["canonical_snapshot_path"])).read_text(encoding="utf-8")
    serialized = json.dumps(payload)
    assert SECRET not in serialized
    assert SECRET not in source_text
    assert SECRET not in canonical_text


def test_secret_query_endpoint_error_path_does_not_expose_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from data.kr_discovery_http_client import fetch_kr_discovery_http_payload
    from run_kr_discovery_source_live_smoke import main

    def _patched_fetch(**kwargs: object) -> dict[str, Any]:
        return fetch_kr_discovery_http_payload(
            endpoint_url=str(kwargs["endpoint_url"]),
            timeout_seconds=float(kwargs.get("timeout_seconds", 15.0)),
            urlopen_fn=_fake_urlopen(raises=URLError(f"open failed api_key={SECRET}")),
            extra_secret_values=(SECRET,),
        )

    with patch(
        "run_kr_discovery_source_live_smoke.fetch_kr_discovery_http_payload",
        side_effect=_patched_fetch,
    ):
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


def test_known_error_output_has_no_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from data.kr_discovery_http_client import KrDiscoveryHttpError
    from run_kr_discovery_source_live_smoke import main

    def _raise_fetch(**kwargs: object) -> dict[str, Any]:
        raise KrDiscoveryHttpError("fetch", "discovery HTTP request failed: URLError: network down")

    with patch(
        "run_kr_discovery_source_live_smoke.fetch_kr_discovery_http_payload",
        side_effect=_raise_fetch,
    ):
        assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_canonical_snapshot_has_compatible_eight_key_root(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    raw = json.loads(Path(str(payload["canonical_snapshot_path"])).read_text(encoding="utf-8"))
    assert set(raw.keys()) == _SNAPSHOT_ROOT_KEYS


def test_canonical_snapshot_has_no_root_request(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    raw = json.loads(Path(str(payload["canonical_snapshot_path"])).read_text(encoding="utf-8"))
    assert "request" not in raw


def test_canonical_records_emit_exactly_fifteen_expected_keys(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    raw = json.loads(Path(str(payload["canonical_snapshot_path"])).read_text(encoding="utf-8"))
    for record in raw["records"]:
        assert set(record.keys()) == _CANONICAL_RECORD_KEYS


def test_candidate_pool_replay_works_when_candidate_pool_out_supplied(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    payload = _run_smoke(tmp_path, candidate_pool_out=pool_out, force=True)
    assert Path(str(payload["candidate_pool_out"])).is_file()
    assert len(parse_kr_candidate_pool_toml(pool_out).candidates) == 5


def test_without_candidate_pool_out_only_source_and_canonical_snapshots_written(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    assert "candidate_pool_out" not in payload
    assert list(tmp_path.rglob("*.toml")) == []


def test_candidate_pool_overwrite_requires_force(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    first = _run_smoke(tmp_path, candidate_pool_out=pool_out, force=True)
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(
            tmp_path,
            source_snapshot_dir=tmp_path / "source_retry",
            canonical_snapshot_dir=tmp_path / "canonical_retry",
            candidate_pool_out=pool_out,
            force=False,
        )
    assert exc_info.value.stage == "write"
    assert Path(str(first["candidate_pool_out"])).is_file()


def test_candidate_pool_loads_through_parse_kr_candidate_pool_toml(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    _run_smoke(tmp_path, candidate_pool_out=pool_out, force=True)
    pool = parse_kr_candidate_pool_toml(pool_out)
    assert pool.base_market == "KR"


def test_default_selected_set_is_900001_900002_900003(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    _run_smoke(tmp_path, candidate_pool_out=pool_out, force=True)
    pool = parse_kr_candidate_pool_toml(pool_out)
    selected = select_candidates(pool)
    assert {entry.symbol for entry in selected} == {"900001", "900002", "900003"}


def test_ranker_with_synthetic_signals_succeeds_using_default_selection(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    _run_smoke(tmp_path, candidate_pool_out=pool_out, force=True)
    ranked_selected_out = tmp_path / "ranked.selected.toml"
    rank_kr_candidates(
        candidate_pool_path=pool_out,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=ranked_selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked from source live smoke pool.",
        top_n=3,
        force=True,
    )
    assert ranked_selected_out.is_file()


def test_generated_selected_toml_flows_through_3f1_generator(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    _run_smoke(tmp_path, candidate_pool_out=pool_out, force=True)
    ranked_selected_out = tmp_path / "ranked.selected.toml"
    rank_kr_candidates(
        candidate_pool_path=pool_out,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=ranked_selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked from source live smoke pool.",
        top_n=3,
        force=True,
    )
    universe_out = tmp_path / "universe.generated.toml"
    mapping_out = tmp_path / "provider_mappings.generated.toml"
    generate_kr_provider_mapping_files(
        candidates_path=ranked_selected_out,
        corp_code_xml=SYNTHETIC_CORP_CODE_XML,
        corp_code_zip=None,
        universe_out=universe_out,
        provider_mapping_out=mapping_out,
        universe_name="kr-discovery-source-live-v1",
        provider_mapping_name="kr-discovery-source-live-mappings-v1",
        force=True,
    )
    validate_provider_mappings_cover_universe(
        load_provider_mapping_toml(mapping_out),
        load_universe_toml(universe_out),
        require_yfinance=True,
        require_dart=True,
    )


def test_invalid_datetime_fails_at_args_stage(tmp_path: Path) -> None:
    from run_kr_discovery_source_live_smoke import main

    argv = _base_cli_args(tmp_path) + ["--fetched-at", "2026-05-30T00:00:00", "--json"]
    assert main(argv) == 1


def test_invalid_timeout_fails_at_args_stage(tmp_path: Path) -> None:
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        _run_smoke(tmp_path, timeout_seconds=0)
    assert exc_info.value.stage == "args"


def test_missing_pool_name_with_candidate_pool_out_fails_at_args_stage(tmp_path: Path) -> None:
    with pytest.raises(KrDiscoverySourceLiveSmokeError) as exc_info:
        run_kr_discovery_source_live_smoke(
            endpoint_url=ENDPOINT_URL,
            source_snapshot_dir=tmp_path / "source_snapshots",
            canonical_snapshot_dir=tmp_path / "canonical_snapshots",
            fetched_at=FETCHED_AT,
            as_of=AS_OF,
            universe_hint="synthetic-provider-v1-live-smoke",
            external_service="synthetic-provider-live-endpoint",
            candidate_pool_out=tmp_path / "pool.toml",
            urlopen_fn=_fake_urlopen(),
        )
    assert exc_info.value.stage == "args"


def test_no_env_or_api_key_read_in_new_modules() -> None:
    forbidden = (
        "os.environ",
        "getenv",
        "urllib.request",
        "requests",
        "httpx",
        "aiohttp",
        "import yfinance",
        "from yfinance",
    )
    for relative in (
        "src/data/kr_discovery_source_payload_snapshot.py",
        "ops/run_kr_discovery_source_live_smoke.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in source, f"{relative} must not reference {token!r}"


def test_no_real_network_call_in_new_modules() -> None:
    forbidden_network = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
    )
    for relative in (
        "src/data/kr_discovery_source_payload_snapshot.py",
        "ops/run_kr_discovery_source_live_smoke.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8").lower()
        for token in forbidden_network:
            assert token not in source, f"{relative} must not reference {token!r}"


def test_static_scan_includes_new_files() -> None:
    paths_text = (REPO_ROOT / "tests" / "test_fetch_research_sources.py").read_text(encoding="utf-8")
    assert "kr_discovery_source_payload_snapshot.py" in paths_text
    assert "run_kr_discovery_source_live_smoke.py" in paths_text


def test_existing_3g3_4b_tests_remain_importable() -> None:
    import test_kr_discovery_live_smoke_cli  # noqa: F401


def test_existing_3g3_5_tests_remain_importable() -> None:
    import test_kr_discovery_schema_mapper  # noqa: F401


def test_no_universe_or_provider_mapping_direct_write_by_adapter(tmp_path: Path) -> None:
    _run_smoke(tmp_path)
    assert list(tmp_path.rglob("universe*.toml")) == []
    assert list(tmp_path.rglob("provider_mappings*.toml")) == []


def test_no_broker_write_paperloop_path_in_new_modules() -> None:
    forbidden = ("paperbroker", "paperlooprunner", "submit_order", "kis")
    for relative in (
        "src/data/kr_discovery_source_payload_snapshot.py",
        "ops/run_kr_discovery_source_live_smoke.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in source, f"{relative} must not reference {token!r}"


def test_in_memory_parser_matches_fixture_file() -> None:
    raw = _source_payload()
    parsed = parse_synthetic_provider_payload_mapping(raw)
    assert parsed.source_format == "synthetic-provider-v1"
    assert len(parsed.items) == 5


def test_cli_success_json_shape(tmp_path: Path) -> None:
    payload = _run_smoke(tmp_path)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["mode"] == "source-live-discovery-smoke"
    assert payload["records_count"] == 5
