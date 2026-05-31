"""Real Intake 3G3-4A — live-shaped KR discovery snapshot fetcher (fake transport only)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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

KST = timezone(timedelta(hours=9))
FETCHED_AT = datetime(2026, 5, 30, 0, 0, 0, tzinfo=KST)
AS_OF = FETCHED_AT
MARKET = "KR"
UNIVERSE_HINT = "synthetic-sector-discovery"
EXTERNAL_SERVICE = "fixture-transport"
SOURCE_NAME = "fixture-transport"

_FORBIDDEN_OUTPUT_FIELDS = frozenset(
    {
        "corp_code",
        "action",
        "side",
        "buy",
        "sell",
        "hold",
        "target_weight",
        "target_allocation",
        "quantity",
        "order",
        "order_type",
        "price_target",
        "stop_loss",
        "take_profit",
        "api_key",
        "crtfc_key",
        "DART_API_KEY",
        "FRED_API_KEY",
        "request",
    }
)

sys.path.insert(0, str(REPO_ROOT / "src"))

from data.fred_http_client import snapshot_filename_for_payload
from data.kr_candidate_pool import export_selected_candidates, parse_kr_candidate_pool_toml, select_candidates
from data.kr_candidate_ranker import rank_kr_candidates
from data.kr_discovery_live_client import (
    KrDiscoveryLiveFetchError,
    build_discovery_request_metadata,
    build_live_discovery_snapshot_payload,
    fetch_live_kr_discovery_snapshot,
)
from data.kr_discovery_source_adapter import load_kr_discovery_snapshot, replay_kr_discovery_snapshot
from data.kr_provider_mapping_generator import generate_kr_provider_mapping_files
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _fixture_records() -> list[dict[str, Any]]:
    payload = json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))
    records = payload["records"]
    assert isinstance(records, list)
    return records


def _fake_transport(
    records: list[dict[str, Any]] | None = None,
    *,
    extra_root: dict[str, Any] | None = None,
) -> object:
    resolved_records = _fixture_records() if records is None else records

    def transport(request_metadata: Mapping[str, str]) -> dict[str, Any]:
        transport.last_request_metadata = dict(request_metadata)  # type: ignore[attr-defined]
        payload: dict[str, Any] = {"records": resolved_records}
        if extra_root:
            payload.update(extra_root)
        return payload

    transport.last_request_metadata = None  # type: ignore[attr-defined]
    return transport


def _fetch_snapshot(tmp_path: Path, transport: object) -> Path:
    return fetch_live_kr_discovery_snapshot(
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
        as_of=AS_OF,
        market=MARKET,
        universe_hint=UNIVERSE_HINT,
        external_service=EXTERNAL_SERVICE,
        source_name=SOURCE_NAME,
        transport=transport,  # type: ignore[arg-type]
    )


def test_transport_none_fails_at_args_stage(tmp_path: Path) -> None:
    with pytest.raises(KrDiscoveryLiveFetchError) as exc_info:
        fetch_live_kr_discovery_snapshot(
            snapshot_dir=tmp_path,
            fetched_at=FETCHED_AT,
            as_of=AS_OF,
            market=MARKET,
            universe_hint=UNIVERSE_HINT,
            external_service=EXTERNAL_SERVICE,
            transport=None,
        )
    assert exc_info.value.stage == "args"


def test_fake_transport_receives_sanitized_request_metadata_only(tmp_path: Path) -> None:
    transport = _fake_transport()
    _fetch_snapshot(tmp_path, transport)
    metadata = transport.last_request_metadata  # type: ignore[attr-defined]
    assert metadata == build_discovery_request_metadata(
        market=MARKET,
        universe_hint=UNIVERSE_HINT,
        as_of=AS_OF,
        source_name=SOURCE_NAME,
    )
    assert set(metadata.keys()) == {"market", "universe_hint", "as_of", "source_name"}
    assert "api_key" not in metadata
    assert "DART_API_KEY" not in metadata


def test_fake_transport_produces_five_record_payload(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert len(payload["records"]) == 5


def test_built_snapshot_root_is_compatible_with_3g3_parser(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    load_kr_discovery_snapshot(snapshot_path)


def test_snapshot_payload_has_source_key_and_version(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload["source_key"] == "kr_discovery"
    assert payload["snapshot_version"] == 1


def test_snapshot_payload_has_timezone_aware_fetched_at_and_as_of(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    snapshot = load_kr_discovery_snapshot(snapshot_path)
    assert snapshot.fetched_at.tzinfo is not None
    assert snapshot.as_of.tzinfo is not None


def test_snapshot_payload_has_no_root_request_field(tmp_path: Path) -> None:
    transport = _fake_transport(extra_root={"request": {"api_key": "secret"}})
    snapshot_path = _fetch_snapshot(tmp_path, transport)
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert "request" not in payload


def test_snapshot_payload_rejects_corp_code_in_records(tmp_path: Path) -> None:
    records = _fixture_records()
    records[0] = {**records[0], "corp_code": "90000010"}
    with pytest.raises(KrDiscoveryLiveFetchError) as exc_info:
        _fetch_snapshot(tmp_path, _fake_transport(records))
    assert exc_info.value.stage == "snapshot"


def test_snapshot_payload_rejects_trading_fields(tmp_path: Path) -> None:
    records = _fixture_records()
    records[0] = {**records[0], "action": "buy"}
    with pytest.raises(KrDiscoveryLiveFetchError) as exc_info:
        _fetch_snapshot(tmp_path, _fake_transport(records))
    assert exc_info.value.stage == "snapshot"


def test_snapshot_payload_rejects_api_key_fields(tmp_path: Path) -> None:
    records = _fixture_records()
    records[0] = {**records[0], "api_key": "secret-key"}
    with pytest.raises(KrDiscoveryLiveFetchError) as exc_info:
        _fetch_snapshot(tmp_path, _fake_transport(records))
    assert exc_info.value.stage == "snapshot"


def test_transport_root_splatting_does_not_copy_unknown_root_fields(tmp_path: Path) -> None:
    transport = _fake_transport(
        extra_root={
            "request": {"ignored": True},
            "provider_credentials": "secret",
            "status": "ok",
        }
    )
    snapshot_path = _fetch_snapshot(tmp_path, transport)
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert set(payload.keys()) == {
        "source_key",
        "external_service",
        "snapshot_version",
        "fetched_at",
        "as_of",
        "market",
        "universe_hint",
        "records",
    }


def test_invalid_transport_output_fails_before_final_snapshot_write(tmp_path: Path) -> None:
    def bad_transport(_metadata: Mapping[str, str]) -> dict[str, Any]:
        return {"records": []}

    with pytest.raises(KrDiscoveryLiveFetchError) as exc_info:
        _fetch_snapshot(tmp_path, bad_transport)
    assert exc_info.value.stage == "snapshot"


def test_invalid_transport_output_leaves_no_final_snapshot_file(tmp_path: Path) -> None:
    def bad_transport(_metadata: Mapping[str, str]) -> dict[str, Any]:
        return {"records": [{"symbol": "bad"}]}

    with pytest.raises(KrDiscoveryLiveFetchError):
        _fetch_snapshot(tmp_path, bad_transport)
    assert list(tmp_path.glob("raw_*.json")) == []
    assert list(tmp_path.glob(".tmp_discovery_*.json")) == []


def test_snapshot_filename_is_deterministic_and_content_hash_based(tmp_path: Path) -> None:
    path_one = _fetch_snapshot(tmp_path, _fake_transport())
    payload = json.loads(path_one.read_text(encoding="utf-8"))
    expected_name = snapshot_filename_for_payload(payload, fetched_at=FETCHED_AT)
    assert path_one.name == expected_name
    assert path_one.name.startswith("raw_")
    assert path_one.name.endswith(".json")


def test_raw_snapshot_is_immutable_collision_raises_file_exists(tmp_path: Path) -> None:
    _fetch_snapshot(tmp_path, _fake_transport())
    with pytest.raises(FileExistsError):
        _fetch_snapshot(tmp_path, _fake_transport())


def test_raw_snapshot_replays_through_load_kr_discovery_snapshot(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    snapshot = load_kr_discovery_snapshot(snapshot_path)
    assert snapshot.market == "KR"
    assert len(snapshot.records) == 5


def test_raw_snapshot_replays_to_candidate_pool_toml(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    pool_out = tmp_path / "candidate_pool.toml"
    replay_kr_discovery_snapshot(
        snapshot_path=snapshot_path,
        candidate_pool_out=pool_out,
        pool_name="kr-discovery-live-shaped-v1",
        pool_description="From live-shaped fake transport snapshot.",
        force=True,
    )
    pool = parse_kr_candidate_pool_toml(pool_out)
    assert len(pool.candidates) == 5


def test_replayed_candidate_pool_default_selected_set(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    pool_out = tmp_path / "candidate_pool.toml"
    replay_kr_discovery_snapshot(
        snapshot_path=snapshot_path,
        candidate_pool_out=pool_out,
        pool_name="kr-discovery-live-shaped-v1",
        pool_description="From live-shaped fake transport snapshot.",
        force=True,
    )
    pool = parse_kr_candidate_pool_toml(pool_out)
    selected = select_candidates(pool)
    assert {entry.symbol for entry in selected} == {"900001", "900002", "900003"}


def test_replayed_pool_flows_through_3g1_selector_export(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    pool_out = tmp_path / "candidate_pool.toml"
    replay_kr_discovery_snapshot(
        snapshot_path=snapshot_path,
        candidate_pool_out=pool_out,
        pool_name="kr-discovery-live-shaped-v1",
        pool_description="From live-shaped fake transport snapshot.",
        force=True,
    )
    pool = parse_kr_candidate_pool_toml(pool_out)
    selected_out = tmp_path / "selected.toml"
    payload = export_selected_candidates(
        pool,
        out_candidates=selected_out,
        export_name="kr-discovery-selected-v1",
        export_description="Selected from live-shaped snapshot pool.",
        force=True,
    )
    assert payload["candidates_selected"] == 3


def test_replayed_pool_flows_through_3g3_1_ranker(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    pool_out = tmp_path / "candidate_pool.toml"
    replay_kr_discovery_snapshot(
        snapshot_path=snapshot_path,
        candidate_pool_out=pool_out,
        pool_name="kr-discovery-live-shaped-v1",
        pool_description="From live-shaped fake transport snapshot.",
        force=True,
    )
    ranked_selected_out = tmp_path / "ranked.selected.toml"
    payload = rank_kr_candidates(
        candidate_pool_path=pool_out,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=ranked_selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked from live-shaped snapshot pool.",
        top_n=3,
        force=True,
    )
    assert payload["selected_count"] == 3


def test_ranked_selected_candidate_toml_flows_through_3f1_generator(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    pool_out = tmp_path / "candidate_pool.toml"
    replay_kr_discovery_snapshot(
        snapshot_path=snapshot_path,
        candidate_pool_out=pool_out,
        pool_name="kr-discovery-live-shaped-v1",
        pool_description="From live-shaped fake transport snapshot.",
        force=True,
    )
    ranked_selected_out = tmp_path / "ranked.selected.toml"
    rank_kr_candidates(
        candidate_pool_path=pool_out,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=ranked_selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked from live-shaped snapshot pool.",
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
        universe_name="kr-discovery-live-shaped-v1",
        provider_mapping_name="kr-discovery-live-shaped-mappings-v1",
        force=True,
    )
    assert universe_out.is_file()
    assert mapping_out.is_file()


def test_generated_universe_and_mapping_validate(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    pool_out = tmp_path / "candidate_pool.toml"
    replay_kr_discovery_snapshot(
        snapshot_path=snapshot_path,
        candidate_pool_out=pool_out,
        pool_name="kr-discovery-live-shaped-v1",
        pool_description="From live-shaped fake transport snapshot.",
        force=True,
    )
    ranked_selected_out = tmp_path / "ranked.selected.toml"
    rank_kr_candidates(
        candidate_pool_path=pool_out,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=ranked_selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked from live-shaped snapshot pool.",
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
        universe_name="kr-discovery-live-shaped-v1",
        provider_mapping_name="kr-discovery-live-shaped-mappings-v1",
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


def test_build_live_discovery_snapshot_payload_rejects_forbidden_fields_in_records() -> None:
    records = _fixture_records()
    records[0] = {**records[0], "request": {"api_key": "secret"}}
    with pytest.raises(KrDiscoveryLiveFetchError) as exc_info:
        build_live_discovery_snapshot_payload(
            transport_payload={"records": records},
            fetched_at=FETCHED_AT,
            as_of=AS_OF,
            market=MARKET,
            universe_hint=UNIVERSE_HINT,
            external_service=EXTERNAL_SERVICE,
        )
    assert exc_info.value.stage == "snapshot"


def test_snapshot_serialized_output_has_no_forbidden_field_names(tmp_path: Path) -> None:
    snapshot_path = _fetch_snapshot(tmp_path, _fake_transport())
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                assert key not in _FORBIDDEN_OUTPUT_FIELDS
                _walk(nested)
        elif isinstance(value, list):
            for nested in value:
                _walk(nested)

    _walk(payload)


def test_source_module_has_no_forbidden_tokens() -> None:
    source = (REPO_ROOT / "src" / "data" / "kr_discovery_live_client.py").read_text(encoding="utf-8").lower()
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
        assert token not in source, f"kr_discovery_live_client.py must not reference {token!r}"


def test_no_runtime_files_tracked_in_repo() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runtime"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert tracked.returncode == 0
    assert tracked.stdout.strip() == ""


def test_static_kr_real_config_samples_unchanged() -> None:
    universe_before = KR_REAL_UNIVERSE.read_text(encoding="utf-8")
    mapping_before = KR_REAL_MAPPING.read_text(encoding="utf-8")
    assert "kr-real-sample-v0" in universe_before
    assert "kr-real-provider-mappings-v1" in mapping_before


def test_static_scan_includes_new_source_file() -> None:
    paths_text = (REPO_ROOT / "tests" / "test_fetch_research_sources.py").read_text(encoding="utf-8")
    assert "kr_discovery_live_client.py" in paths_text


def test_existing_3g3_3_tests_remain_importable() -> None:
    import test_kr_discovery_source_adapter  # noqa: F401
