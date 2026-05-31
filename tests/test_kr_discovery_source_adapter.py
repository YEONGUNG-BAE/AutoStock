"""Real Intake 3G3-3 — fixture-first KR discovery snapshot replay adapter tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

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
OPS_SCRIPT = REPO_ROOT / "ops" / "replay_kr_discovery_snapshot.py"

_SYNTHETIC_CORP_CODES = {
    "900001": "90000010",
    "900002": "90000011",
    "900003": "90000012",
}
_SYNTHETIC_YFINANCE = {
    "900001": "900001.KS",
    "900002": "900002.KS",
    "900003": "900003.KS",
}

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data.kr_candidate_pool import (
    export_selected_candidates,
    parse_kr_candidate_pool_toml,
    select_candidates,
)
from data.kr_candidate_ranker import rank_kr_candidates
from data.kr_discovery_source_adapter import (
    KrDiscoverySnapshot,
    KrDiscoverySnapshotRecord,
    KrDiscoverySourceAdapterError,
    discovery_snapshot_to_candidate_pool,
    load_kr_discovery_snapshot,
    render_candidate_pool_toml,
    replay_kr_discovery_snapshot,
    write_candidate_pool_toml,
)
from data.kr_provider_mapping_generator import generate_kr_provider_mapping_files, parse_kr_candidates_toml
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _write_snapshot(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _load_fixture_payload() -> dict[str, object]:
    return json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))


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
        "--snapshot",
        str(SNAPSHOT_FIXTURE),
        "--candidate-pool-out",
        str(tmp_path / "candidate_pool.toml"),
        "--pool-name",
        "kr-discovery-synthetic-pool-v1",
        "--pool-description",
        "Synthetic replayed KR discovery candidate pool.",
    ]


def _replay_to_pool(tmp_path: Path) -> Path:
    pool_out = tmp_path / "candidate_pool.toml"
    replay_kr_discovery_snapshot(
        snapshot_path=SNAPSHOT_FIXTURE,
        candidate_pool_out=pool_out,
        pool_name="kr-discovery-synthetic-pool-v1",
        pool_description="Synthetic replayed KR discovery candidate pool.",
        force=True,
    )
    return pool_out


def test_discovery_snapshot_fixture_parses() -> None:
    snapshot = load_kr_discovery_snapshot(SNAPSHOT_FIXTURE)
    assert snapshot.source_key == "kr_discovery"
    assert snapshot.snapshot_version == 1
    assert snapshot.market == "KR"
    assert len(snapshot.records) == 5


def test_root_source_key_must_be_kr_discovery(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    payload["source_key"] = "other"
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"


def test_snapshot_version_must_be_one(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    payload["snapshot_version"] = 2
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"


def test_datetimes_must_be_timezone_aware(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    payload["fetched_at"] = "2026-05-30T00:00:00"
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"
    assert "fetched_at" in exc_info.value.message


def test_record_source_timestamp_must_be_timezone_aware(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    records = payload["records"]
    assert isinstance(records, list)
    records[0]["source_timestamp"] = "2026-05-30T00:00:00"
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"
    assert "source_timestamp" in exc_info.value.message


def test_unknown_root_fields_rejected(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    payload["extra_root"] = True
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"


def test_unknown_record_fields_rejected(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    records = payload["records"]
    assert isinstance(records, list)
    records[0]["extra_field"] = True
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"


def test_record_corp_code_rejected(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    records = payload["records"]
    assert isinstance(records, list)
    records[0]["corp_code"] = "90000010"
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"
    assert "corp_code" in exc_info.value.message


def test_duplicate_market_symbol_rejected(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    records = payload["records"]
    assert isinstance(records, list)
    records.append(dict(records[0]))
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"
    assert "duplicate" in exc_info.value.message


def test_non_kr_market_rejected(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    payload["market"] = "US"
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"


def test_invalid_yfinance_suffix_rejected(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    records = payload["records"]
    assert isinstance(records, list)
    records[0]["yfinance_provider_symbol"] = "900001.US"
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"


def test_invalid_sector_slug_rejected(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    records = payload["records"]
    assert isinstance(records, list)
    records[0]["sector"] = "Semi Conductors"
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"


def test_control_characters_rejected(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    payload["universe_hint"] = "bad\x01hint"
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"
    assert "control character" in exc_info.value.message


def test_symbol_must_match_normalized_stock_code(tmp_path: Path) -> None:
    payload = _load_fixture_payload()
    records = payload["records"]
    assert isinstance(records, list)
    records[0]["symbol"] = "900999"
    path = _write_snapshot(tmp_path, payload)
    with pytest.raises(KrDiscoverySourceAdapterError) as exc_info:
        load_kr_discovery_snapshot(path)
    assert exc_info.value.stage == "parse"
    assert "symbol must match normalized stock_code" in exc_info.value.message


def test_replay_writes_candidate_pool_toml(tmp_path: Path) -> None:
    pool_out = _replay_to_pool(tmp_path)
    assert pool_out.is_file()


def test_candidate_pool_toml_loads_via_3g1_parser(tmp_path: Path) -> None:
    pool_out = _replay_to_pool(tmp_path)
    pool = parse_kr_candidate_pool_toml(pool_out)
    assert pool.name == "kr-discovery-synthetic-pool-v1"
    assert pool.base_market == "KR"
    assert len(pool.candidates) == 5


def test_candidate_pool_output_has_full_pool_schema(tmp_path: Path) -> None:
    pool_out = _replay_to_pool(tmp_path)
    raw = tomllib.loads(pool_out.read_text(encoding="utf-8"))
    assert raw["base_market"] == "KR"
    for entry in raw["candidates"]:
        assert "sector" in entry
        assert "industry" in entry
        assert "enabled" in entry
        assert "eligible" in entry


def test_candidate_pool_output_has_no_discovery_provenance_fields(tmp_path: Path) -> None:
    pool_out = _replay_to_pool(tmp_path)
    text = pool_out.read_text(encoding="utf-8")
    forbidden = {"corp_code", "source_timestamp", "source_url"}
    assert forbidden.isdisjoint(set(text.split()))
    raw = tomllib.loads(text)
    for entry in raw["candidates"]:
        assert forbidden.isdisjoint(entry.keys())


def test_optional_priority_and_notes_omitted_when_absent() -> None:
    snapshot = load_kr_discovery_snapshot(SNAPSHOT_FIXTURE)
    record = snapshot.records[0]
    minimal = KrDiscoverySnapshotRecord(
        symbol=record.symbol,
        market=record.market,
        display_name=record.display_name,
        stock_code=record.stock_code,
        corp_name=record.corp_name,
        yfinance_provider_symbol=record.yfinance_provider_symbol,
        currency=record.currency,
        sector=record.sector,
        industry=record.industry,
        enabled=record.enabled,
        eligible=record.eligible,
        priority=None,
        notes=None,
        source_timestamp=record.source_timestamp,
        source_url=None,
    )
    minimal_snapshot = KrDiscoverySnapshot(
        source_key=snapshot.source_key,
        external_service=snapshot.external_service,
        snapshot_version=snapshot.snapshot_version,
        fetched_at=snapshot.fetched_at,
        as_of=snapshot.as_of,
        market=snapshot.market,
        universe_hint=snapshot.universe_hint,
        records=(minimal,),
    )
    pool = discovery_snapshot_to_candidate_pool(
        minimal_snapshot,
        pool_name="minimal-pool",
        pool_description="Minimal pool for optional field test.",
    )
    rendered = render_candidate_pool_toml(pool)
    assert "priority" not in rendered
    assert "notes" not in rendered


def test_fixture_default_selected_candidates_are_900001_900002_900003(tmp_path: Path) -> None:
    pool_out = _replay_to_pool(tmp_path)
    pool = parse_kr_candidate_pool_toml(pool_out)
    selected = select_candidates(pool)
    assert {entry.symbol for entry in selected} == {"900001", "900002", "900003"}
    assert [entry.symbol for entry in selected] == ["900002", "900001", "900003"]


def test_cli_refuses_overwrite_without_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from replay_kr_discovery_snapshot import main

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"


def test_cli_json_stdout_is_pure_json(tmp_path: Path) -> None:
    result = _run_cli(*(_base_cli_args(tmp_path) + ["--force", "--json"]))
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"


def test_generated_pool_flows_through_3g1_selector_export(tmp_path: Path) -> None:
    pool_out = _replay_to_pool(tmp_path)
    pool = parse_kr_candidate_pool_toml(pool_out)
    selected_out = tmp_path / "selected.toml"
    payload = export_selected_candidates(
        pool,
        out_candidates=selected_out,
        export_name="kr-discovery-selected-v1",
        export_description="Selected from replayed discovery pool.",
        force=True,
    )
    assert payload["candidates_selected"] == 3
    assert selected_out.is_file()


def test_selected_candidate_toml_flows_through_3g3_1_ranker(tmp_path: Path) -> None:
    pool_out = _replay_to_pool(tmp_path)
    pool = parse_kr_candidate_pool_toml(pool_out)
    selected_out = tmp_path / "selected.toml"
    export_selected_candidates(
        pool,
        out_candidates=selected_out,
        export_name="kr-discovery-selected-v1",
        export_description="Selected from replayed discovery pool.",
        force=True,
    )
    ranked_out = tmp_path / "ranked.json"
    ranked_selected_out = tmp_path / "ranked.selected.toml"
    payload = rank_kr_candidates(
        candidate_pool_path=pool_out,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=ranked_out,
        selected_candidates_out=ranked_selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked from replayed discovery pool.",
        top_n=3,
        force=True,
    )
    assert payload["ranked_count"] == 3
    assert payload["selected_count"] == 3
    assert parse_kr_candidates_toml(ranked_selected_out)


def test_ranked_selected_candidate_toml_flows_through_3f1_generator(tmp_path: Path) -> None:
    pool_out = _replay_to_pool(tmp_path)
    ranked_selected_out = tmp_path / "ranked.selected.toml"
    rank_kr_candidates(
        candidate_pool_path=pool_out,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=ranked_selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked from replayed discovery pool.",
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
        universe_name="kr-discovery-ranked-v1",
        provider_mapping_name="kr-discovery-ranked-mappings-v1",
        force=True,
    )
    assert universe_out.is_file()
    assert mapping_out.is_file()


def test_generated_universe_and_mapping_validate(tmp_path: Path) -> None:
    pool_out = _replay_to_pool(tmp_path)
    ranked_selected_out = tmp_path / "ranked.selected.toml"
    rank_kr_candidates(
        candidate_pool_path=pool_out,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=ranked_selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked from replayed discovery pool.",
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
        universe_name="kr-discovery-ranked-v1",
        provider_mapping_name="kr-discovery-ranked-mappings-v1",
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
    for symbol in ("900001", "900002", "900003"):
        mapping = registry.resolve(symbol=symbol, market="KR")
        assert mapping.yfinance is not None
        assert mapping.yfinance.provider_symbol == _SYNTHETIC_YFINANCE[symbol]
        assert mapping.dart is not None
        assert mapping.dart.corp_code == _SYNTHETIC_CORP_CODES[symbol]


def test_ops_script_has_no_forbidden_tokens() -> None:
    paths = [
        REPO_ROOT / "src" / "data" / "kr_discovery_source_adapter.py",
        OPS_SCRIPT,
    ]
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
    for path in paths:
        source = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in source, f"{path.name} must not reference {token!r}"


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


def test_static_scan_includes_new_source_and_ops_files() -> None:
    paths_text = (REPO_ROOT / "tests" / "test_fetch_research_sources.py").read_text(encoding="utf-8")
    assert "kr_discovery_source_adapter.py" in paths_text
    assert "replay_kr_discovery_snapshot.py" in paths_text


def test_existing_3g3_1_and_3g3_2_tests_remain_importable() -> None:
    import test_kr_candidate_ranker  # noqa: F401
    import test_kr_real_ranked_mapping_workflow  # noqa: F401


def test_write_candidate_pool_self_validates(tmp_path: Path) -> None:
    snapshot = load_kr_discovery_snapshot(SNAPSHOT_FIXTURE)
    pool = discovery_snapshot_to_candidate_pool(
        snapshot,
        pool_name="kr-discovery-synthetic-pool-v1",
        pool_description="Synthetic replayed KR discovery candidate pool.",
    )
    out_path = tmp_path / "pool.toml"
    write_candidate_pool_toml(pool, out_path, force=True)
    assert parse_kr_candidate_pool_toml(out_path)
