"""Real Intake 3G3-5 — fixture-first KR discovery source schema mapper."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
OPS_SCRIPT = REPO_ROOT / "ops" / "map_kr_discovery_fixture.py"

KST = timezone(timedelta(hours=9))
FETCHED_AT = datetime(2026, 5, 30, 0, 0, 0, tzinfo=KST)
AS_OF = FETCHED_AT
UNIVERSE_HINT = "synthetic-provider-v1"
EXTERNAL_SERVICE = "synthetic-provider-fixture"

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
_FORBIDDEN_OUTPUT_FIELDS = frozenset(
    {
        "corp_code",
        "action",
        "request",
        "api_key",
        "crtfc_key",
        "DART_API_KEY",
        "FRED_API_KEY",
    }
)

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data.kr_candidate_pool import export_selected_candidates, parse_kr_candidate_pool_toml, select_candidates
from data.kr_candidate_ranker import rank_kr_candidates
from data.kr_discovery_live_client import KrDiscoveryLiveFetchError
from data.kr_discovery_schema_mapper import (
    KrDiscoverySchemaMappingError,
    load_synthetic_provider_payload,
    map_synthetic_provider_fixture_to_snapshot,
    map_synthetic_provider_payload_to_transport_payload,
)
from data.kr_discovery_source_adapter import load_kr_discovery_snapshot, replay_kr_discovery_snapshot
from data.kr_provider_mapping_generator import generate_kr_provider_mapping_files
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml
from map_kr_discovery_fixture import MapKrDiscoveryFixtureCliError, run_map_kr_discovery_fixture


def _load_fixture_dict() -> dict[str, Any]:
    return json.loads(SOURCE_FIXTURE.read_text(encoding="utf-8"))


def _write_payload(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "source_payload.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _runner_args(tmp_path: Path, *, candidate_pool_out: Path | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {
        "source_payload_path": SOURCE_FIXTURE,
        "snapshot_dir": tmp_path / "snapshots",
        "fetched_at": FETCHED_AT,
        "as_of": AS_OF,
        "universe_hint": UNIVERSE_HINT,
        "external_service": EXTERNAL_SERVICE,
    }
    if candidate_pool_out is not None:
        args["candidate_pool_out"] = candidate_pool_out
        args["pool_name"] = "kr-discovery-mapped-pool-v1"
        args["pool_description"] = "Synthetic provider mapped KR discovery candidate pool."
    return args


def _run_mapper(tmp_path: Path, **overrides: object) -> dict[str, Any]:
    args = _runner_args(tmp_path, candidate_pool_out=overrides.pop("candidate_pool_out", None))
    args.update(overrides)
    return run_map_kr_discovery_fixture(**args)  # type: ignore[arg-type]


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
        "--source-payload",
        str(SOURCE_FIXTURE),
        "--snapshot-dir",
        str(tmp_path / "snapshots"),
        "--fetched-at",
        FETCHED_AT.isoformat(),
        "--as-of",
        AS_OF.isoformat(),
        "--universe-hint",
        UNIVERSE_HINT,
        "--external-service",
        EXTERNAL_SERVICE,
    ]


def test_synthetic_provider_fixture_parses() -> None:
    payload = load_synthetic_provider_payload(SOURCE_FIXTURE)
    assert payload.source_format == "synthetic-provider-v1"
    assert len(payload.items) == 5


def test_root_source_format_must_be_synthetic_provider_v1(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["source_format"] = "other-format"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"


def test_root_market_must_be_kr(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["market"] = "US"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"


def test_root_as_of_must_be_timezone_aware(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["as_of"] = "2026-05-30T00:00:00"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"


def test_unknown_root_fields_rejected(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["extra_root"] = "value"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"
    assert "extra_root" in exc_info.value.message


def test_unknown_item_fields_rejected(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["items"][0]["market"] = "KR"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"
    assert "unknown fields" in exc_info.value.message


def test_item_corp_code_rejected(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["items"][0]["corp_code"] = "90000010"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"
    assert "corp_code" in exc_info.value.message


def test_duplicate_normalized_market_symbol_rejected(tmp_path: Path) -> None:
    payload = load_synthetic_provider_payload(SOURCE_FIXTURE)
    items = list(payload.items)
    duplicate_item = copy.deepcopy(items[0])
    transport_payload = map_synthetic_provider_payload_to_transport_payload(payload)
    records = transport_payload["records"]
    records.append(
        {
            **records[0],
            "display_name": "Duplicate Display",
        }
    )
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        map_synthetic_provider_payload_to_transport_payload(
            type(payload)(
                source_format=payload.source_format,
                as_of=payload.as_of,
                market=payload.market,
                items=tuple([items[0], duplicate_item]),
            )
        )
    assert exc_info.value.stage == "map"
    assert "duplicate" in exc_info.value.message


def test_root_market_non_kr_rejected(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["market"] = "JP"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"


def test_item_level_market_rejected_as_unknown_field(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["items"][0]["market"] = "KR"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"


def test_invalid_stock_code_rejected(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["items"][0]["stockCode"] = "ABC"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"


def test_symbol_stock_code_normalization_produces_six_digit_canonical_symbol(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["items"] = [raw["items"][0].copy()]
    raw["items"][0]["stockCode"] = "9001"
    raw["items"][0]["ticker"] = "009001.KS"
    raw["items"][0]["sourceUrl"] = "fixture://synthetic-provider/009001"
    path = _write_payload(tmp_path, raw)
    transport = map_synthetic_provider_payload_to_transport_payload(load_synthetic_provider_payload(path))
    record = transport["records"][0]
    assert record["symbol"] == "009001"
    assert record["stock_code"] == "009001"


def test_invalid_yfinance_suffix_rejected(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["items"][0]["ticker"] = "900001.US"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"


def test_unknown_sector_code_fails_at_map_stage(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["items"][0]["sectorCode"] = "UNKNOWN_SECTOR"
    path = _write_payload(tmp_path, raw)
    payload = load_synthetic_provider_payload(path)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        map_synthetic_provider_payload_to_transport_payload(payload)
    assert exc_info.value.stage == "map"


def test_sector_code_maps_deterministically_to_canonical_slug() -> None:
    payload = load_synthetic_provider_payload(SOURCE_FIXTURE)
    transport = map_synthetic_provider_payload_to_transport_payload(payload)
    sectors = {record["sector"] for record in transport["records"]}
    assert sectors == {"semiconductors", "internet"}


def test_industry_label_maps_deterministically() -> None:
    payload = load_synthetic_provider_payload(SOURCE_FIXTURE)
    transport = map_synthetic_provider_payload_to_transport_payload(payload)
    industries = {record["industry"] for record in transport["records"]}
    assert industries == {"memory", "platform", "fabless", "commerce", "equipment"}


def test_control_characters_rejected(tmp_path: Path) -> None:
    raw = _load_fixture_dict()
    raw["items"][0]["displayName"] = "bad\u0007name"
    path = _write_payload(tmp_path, raw)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        load_synthetic_provider_payload(path)
    assert exc_info.value.stage == "parse"
    assert "bad" not in exc_info.value.message


def test_mapped_transport_payload_contains_records_only() -> None:
    transport = map_synthetic_provider_payload_to_transport_payload(load_synthetic_provider_payload(SOURCE_FIXTURE))
    assert set(transport.keys()) == {"records"}


def test_mapped_transport_records_contain_exactly_canonical_fifteen_keys() -> None:
    transport = map_synthetic_provider_payload_to_transport_payload(load_synthetic_provider_payload(SOURCE_FIXTURE))
    for record in transport["records"]:
        assert set(record.keys()) == _CANONICAL_RECORD_KEYS


def test_mapped_transport_has_no_corp_code_request_or_env_fields() -> None:
    transport = map_synthetic_provider_payload_to_transport_payload(load_synthetic_provider_payload(SOURCE_FIXTURE))

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                assert key not in _FORBIDDEN_OUTPUT_FIELDS
                _walk(nested)
        elif isinstance(value, list):
            for nested in value:
                _walk(nested)

    _walk(transport)


def test_mapped_transport_preserves_enabled_eligible_alignment() -> None:
    transport = map_synthetic_provider_payload_to_transport_payload(load_synthetic_provider_payload(SOURCE_FIXTURE))
    by_symbol = {record["symbol"]: record for record in transport["records"]}

    selected = {
        symbol
        for symbol, record in by_symbol.items()
        if record["enabled"] and record["eligible"]
    }
    assert selected == {"900001", "900002", "900003"}
    assert by_symbol["900004"]["enabled"] is False
    assert by_symbol["900004"]["eligible"] is True
    assert by_symbol["900005"]["enabled"] is True
    assert by_symbol["900005"]["eligible"] is False


def test_map_to_snapshot_writes_immutable_raw_snapshot_via_4a(tmp_path: Path) -> None:
    snapshot_path = map_synthetic_provider_fixture_to_snapshot(
        source_payload_path=SOURCE_FIXTURE,
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
        as_of=AS_OF,
        universe_hint=UNIVERSE_HINT,
        external_service=EXTERNAL_SERVICE,
    )
    assert snapshot_path.is_file()
    assert snapshot_path.name.startswith("raw_")


def test_mapper_catches_4a_error_and_remaps_to_snapshot_stage(tmp_path: Path) -> None:
    with patch(
        "data.kr_discovery_schema_mapper.fetch_live_kr_discovery_snapshot",
        side_effect=KrDiscoveryLiveFetchError("snapshot", "transport payload records must be a non-empty list"),
    ):
        with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
            map_synthetic_provider_fixture_to_snapshot(
                source_payload_path=SOURCE_FIXTURE,
                snapshot_dir=tmp_path,
                fetched_at=FETCHED_AT,
                as_of=AS_OF,
                universe_hint=UNIVERSE_HINT,
                external_service=EXTERNAL_SERVICE,
            )
    assert exc_info.value.stage == "snapshot"


def test_mapper_does_not_leak_bare_value_error_for_invalid_datetime(tmp_path: Path) -> None:
    naive = datetime(2026, 5, 30, 0, 0, 0)
    with pytest.raises(KrDiscoverySchemaMappingError) as exc_info:
        map_synthetic_provider_fixture_to_snapshot(
            source_payload_path=SOURCE_FIXTURE,
            snapshot_dir=tmp_path,
            fetched_at=naive,
            as_of=AS_OF,
            universe_hint=UNIVERSE_HINT,
            external_service=EXTERNAL_SERVICE,
        )
    assert exc_info.value.stage == "snapshot"
    assert isinstance(exc_info.value, KrDiscoverySchemaMappingError)


def test_raw_snapshot_has_compatible_eight_key_root(tmp_path: Path) -> None:
    snapshot_path = map_synthetic_provider_fixture_to_snapshot(
        source_payload_path=SOURCE_FIXTURE,
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
        as_of=AS_OF,
        universe_hint=UNIVERSE_HINT,
        external_service=EXTERNAL_SERVICE,
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert set(payload.keys()) == _SNAPSHOT_ROOT_KEYS


def test_raw_snapshot_has_no_root_request(tmp_path: Path) -> None:
    snapshot_path = map_synthetic_provider_fixture_to_snapshot(
        source_payload_path=SOURCE_FIXTURE,
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
        as_of=AS_OF,
        universe_hint=UNIVERSE_HINT,
        external_service=EXTERNAL_SERVICE,
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert "request" not in payload


def test_raw_snapshot_replays_through_load_kr_discovery_snapshot(tmp_path: Path) -> None:
    snapshot_path = map_synthetic_provider_fixture_to_snapshot(
        source_payload_path=SOURCE_FIXTURE,
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
        as_of=AS_OF,
        universe_hint=UNIVERSE_HINT,
        external_service=EXTERNAL_SERVICE,
    )
    snapshot = load_kr_discovery_snapshot(snapshot_path)
    assert len(snapshot.records) == 5


def test_raw_snapshot_replays_to_candidate_pool(tmp_path: Path) -> None:
    snapshot_path = map_synthetic_provider_fixture_to_snapshot(
        source_payload_path=SOURCE_FIXTURE,
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
        as_of=AS_OF,
        universe_hint=UNIVERSE_HINT,
        external_service=EXTERNAL_SERVICE,
    )
    pool_out = tmp_path / "candidate_pool.toml"
    replay_kr_discovery_snapshot(
        snapshot_path=snapshot_path,
        candidate_pool_out=pool_out,
        pool_name="kr-discovery-mapped-pool-v1",
        pool_description="Synthetic provider mapped KR discovery candidate pool.",
        force=True,
    )
    assert pool_out.is_file()


def test_candidate_pool_loads_through_parse_kr_candidate_pool_toml(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    _run_mapper(tmp_path, candidate_pool_out=pool_out, force=True)
    pool = parse_kr_candidate_pool_toml(pool_out)
    assert len(pool.candidates) == 5


def test_candidate_pool_flows_through_3g1_selector_export(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    _run_mapper(tmp_path, candidate_pool_out=pool_out, force=True)
    pool = parse_kr_candidate_pool_toml(pool_out)
    selected = select_candidates(pool)
    assert {entry.symbol for entry in selected} == {"900001", "900002", "900003"}
    export_path = tmp_path / "selected.toml"
    payload = export_selected_candidates(
        pool,
        out_candidates=export_path,
        export_name="kr-discovery-mapped-selected-v1",
        export_description="Selected from mapped discovery pool.",
        force=True,
    )
    assert payload["candidates_selected"] == 3


def test_candidate_pool_flows_through_3g3_1_ranker_with_default_selection(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    _run_mapper(tmp_path, candidate_pool_out=pool_out, force=True)
    ranked_selected_out = tmp_path / "ranked.selected.toml"
    rank_kr_candidates(
        candidate_pool_path=pool_out,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=ranked_selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked from mapped discovery pool.",
        top_n=3,
        force=True,
    )
    assert ranked_selected_out.is_file()


def test_ranked_selected_toml_flows_through_3f1_generator(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    _run_mapper(tmp_path, candidate_pool_out=pool_out, force=True)
    ranked_selected_out = tmp_path / "ranked.selected.toml"
    rank_kr_candidates(
        candidate_pool_path=pool_out,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=ranked_selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked from mapped discovery pool.",
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
        universe_name="kr-discovery-mapped-v1",
        provider_mapping_name="kr-discovery-mapped-mappings-v1",
        force=True,
    )
    universe = load_universe_toml(universe_out)
    registry = load_provider_mapping_toml(mapping_out)
    validate_provider_mappings_cover_universe(
        registry,
        universe,
        require_yfinance=True,
        require_dart=True,
    )


def test_cli_without_candidate_pool_out_writes_only_snapshot(tmp_path: Path) -> None:
    payload = _run_mapper(tmp_path)
    assert Path(str(payload["snapshot_path"])).is_file()
    assert "candidate_pool_out" not in payload
    assert list(tmp_path.rglob("*.toml")) == []


def test_cli_with_candidate_pool_out_writes_snapshot_and_pool(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    payload = _run_mapper(tmp_path, candidate_pool_out=pool_out, force=True)
    assert Path(str(payload["snapshot_path"])).is_file()
    assert Path(str(payload["candidate_pool_out"])).is_file()


def test_candidate_pool_overwrite_requires_force(tmp_path: Path) -> None:
    pool_out = tmp_path / "candidate_pool.toml"
    pool_out.write_text("existing\n", encoding="utf-8")
    with pytest.raises(MapKrDiscoveryFixtureCliError) as exc_info:
        _run_mapper(tmp_path, candidate_pool_out=pool_out)
    assert exc_info.value.stage == "write"
    assert pool_out.read_text(encoding="utf-8") == "existing\n"


def test_raw_snapshot_collision_remains_immutable_even_with_force(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshots"
    first = _run_mapper(tmp_path, snapshot_dir=snapshot_dir)
    snapshot_path = Path(str(first["snapshot_path"]))
    original_bytes = snapshot_path.read_bytes()

    pool_out = tmp_path / "candidate_pool.toml"
    with pytest.raises(MapKrDiscoveryFixtureCliError) as exc_info:
        _run_mapper(
            tmp_path,
            snapshot_dir=snapshot_dir,
            candidate_pool_out=pool_out,
            force=True,
        )
    assert exc_info.value.stage == "snapshot"
    assert snapshot_path.read_bytes() == original_bytes
    assert not pool_out.exists()


def test_invalid_datetime_in_cli_fails_at_args_stage(tmp_path: Path) -> None:
    result = _run_cli(
        *_base_cli_args(tmp_path),
        "--fetched-at",
        "2026-05-30T00:00:00",
        "--json",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "args"


def test_no_env_or_api_key_read_in_source_module() -> None:
    source = (REPO_ROOT / "src" / "data" / "kr_discovery_schema_mapper.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "os.environ",
        "getenv",
        "dart_api_key",
        "fred_api_key",
        "urllib.request",
        "requests",
        "httpx",
        "aiohttp",
        "import yfinance",
        "from yfinance",
    )
    for token in forbidden:
        assert token not in source, f"kr_discovery_schema_mapper.py must not reference {token!r}"


def test_no_network_or_live_api_in_new_modules() -> None:
    forbidden_network = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
    )
    for relative in (
        "src/data/kr_discovery_schema_mapper.py",
        "ops/map_kr_discovery_fixture.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8").lower()
        for token in forbidden_network:
            assert token not in source, f"{relative} must not reference {token!r}"


def test_no_runtime_files_tracked_in_repo() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runtime"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert tracked.returncode == 0
    assert tracked.stdout.strip() == ""


def test_static_scan_includes_new_source_and_ops_files() -> None:
    paths_text = (REPO_ROOT / "tests" / "test_fetch_research_sources.py").read_text(encoding="utf-8")
    assert "kr_discovery_schema_mapper.py" in paths_text
    assert "map_kr_discovery_fixture.py" in paths_text


def test_existing_3g3_4a_tests_remain_importable() -> None:
    import test_kr_discovery_live_client  # noqa: F401


def test_existing_3g3_4b_tests_remain_importable() -> None:
    import test_kr_discovery_live_smoke_cli  # noqa: F401


def test_cli_json_success_shape(tmp_path: Path) -> None:
    result = _run_cli(*_base_cli_args(tmp_path), "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["mode"] == "fixture-discovery-mapper"
    assert payload["records_count"] == 5


def test_static_kr_real_config_samples_unchanged() -> None:
    universe_before = KR_REAL_UNIVERSE.read_text(encoding="utf-8")
    mapping_before = KR_REAL_MAPPING.read_text(encoding="utf-8")
    assert "kr-real-sample-v0" in universe_before
    assert "kr-real-provider-mappings-v1" in mapping_before
