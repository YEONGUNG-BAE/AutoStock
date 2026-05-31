"""Real Intake 3G1 — fixture-first sector-tagged KR candidate pool tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
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
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
OPS_SCRIPT = REPO_ROOT / "ops" / "select_kr_candidates.py"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data.kr_candidate_pool import (
    KrCandidatePoolError,
    export_selected_candidates,
    parse_kr_candidate_pool_toml,
    render_selected_candidates_toml,
    select_candidates,
    validate_exported_candidates_toml,
)
from data.kr_provider_mapping_generator import generate_kr_provider_mapping_files, parse_kr_candidates_toml
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _write_pool(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pool.toml"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


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


def test_candidate_pool_fixture_parses() -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    assert pool.name == "kr-sector-candidate-pool-synthetic-v1"
    assert pool.base_market == "KR"
    assert len(pool.candidates) == 5
    sectors = {entry.sector for entry in pool.candidates}
    assert sectors == {"semiconductors", "internet"}


def test_unknown_root_field_rejected(tmp_path: Path) -> None:
    path = _write_pool(
        tmp_path,
        """
version = 1
name = "bad-root"
description = "unknown root"
base_market = "KR"
extra_root = true

[[candidates]]
symbol = "900001"
market = "KR"
display_name = "SYNTH Alpha Display"
stock_code = "900001"
corp_name = "SYNTH-ALPHA"
yfinance_provider_symbol = "900001.KS"
currency = "KRW"
sector = "semiconductors"
industry = "memory"
enabled = true
eligible = true
""",
    )
    with pytest.raises(KrCandidatePoolError) as exc_info:
        parse_kr_candidate_pool_toml(path)
    assert exc_info.value.stage == "parse"
    assert "unknown candidate pool root fields" in exc_info.value.message


def test_unknown_entry_field_rejected(tmp_path: Path) -> None:
    path = _write_pool(
        tmp_path,
        """
version = 1
name = "bad-entry"
description = "unknown entry"
base_market = "KR"

[[candidates]]
symbol = "900001"
market = "KR"
display_name = "SYNTH Alpha Display"
stock_code = "900001"
corp_name = "SYNTH-ALPHA"
yfinance_provider_symbol = "900001.KS"
currency = "KRW"
sector = "semiconductors"
industry = "memory"
enabled = true
eligible = true
extra_field = true
""",
    )
    with pytest.raises(KrCandidatePoolError) as exc_info:
        parse_kr_candidate_pool_toml(path)
    assert exc_info.value.stage == "parse"
    assert "unknown fields" in exc_info.value.message


def test_candidate_corp_code_field_rejected(tmp_path: Path) -> None:
    path = _write_pool(
        tmp_path,
        """
version = 1
name = "bad-corp-code"
description = "corp_code forbidden"
base_market = "KR"

[[candidates]]
symbol = "900001"
market = "KR"
display_name = "SYNTH Alpha Display"
stock_code = "900001"
corp_name = "SYNTH-ALPHA"
corp_code = "90000010"
yfinance_provider_symbol = "900001.KS"
currency = "KRW"
sector = "semiconductors"
industry = "memory"
enabled = true
eligible = true
""",
    )
    with pytest.raises(KrCandidatePoolError) as exc_info:
        parse_kr_candidate_pool_toml(path)
    assert exc_info.value.stage == "parse"
    assert "corp_code" in exc_info.value.message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("description", "line1\\nline2"),
        ("display_name", "Bad\\nName"),
        ("corp_name", "SYNTH\\tALPHA"),
        ("notes", "note\\nline"),
    ],
)
def test_control_characters_rejected(tmp_path: Path, field: str, value: str) -> None:
    if field == "description":
        path = _write_pool(
            tmp_path,
            f"""
version = 1
name = "control-root"
description = "{value}"
base_market = "KR"

[[candidates]]
symbol = "900001"
market = "KR"
display_name = "SYNTH Alpha Display"
stock_code = "900001"
corp_name = "SYNTH-ALPHA"
yfinance_provider_symbol = "900001.KS"
currency = "KRW"
sector = "semiconductors"
industry = "memory"
enabled = true
eligible = true
""",
        )
    else:
        entry_lines = [
            'symbol = "900001"',
            'market = "KR"',
            'display_name = "SYNTH Alpha Display"' if field != "display_name" else f'display_name = "{value}"',
            'stock_code = "900001"',
            'corp_name = "SYNTH-ALPHA"' if field != "corp_name" else f'corp_name = "{value}"',
            'yfinance_provider_symbol = "900001.KS"',
            'currency = "KRW"',
            'sector = "semiconductors"',
            'industry = "memory"',
            'enabled = true',
            'eligible = true',
        ]
        if field == "notes":
            entry_lines.append(f'notes = "{value}"')
        path = _write_pool(
            tmp_path,
            f"""
version = 1
name = "control-entry"
description = "control entry"
base_market = "KR"

[[candidates]]
{chr(10).join(entry_lines)}
""",
        )
    with pytest.raises(KrCandidatePoolError) as exc_info:
        parse_kr_candidate_pool_toml(path)
    assert exc_info.value.stage == "parse"
    assert "control character" in exc_info.value.message


@pytest.mark.parametrize("sector", ["Semiconductors", "semi conductors", "반도체", "semi!"])
def test_invalid_sector_slug_rejected(tmp_path: Path, sector: str) -> None:
    path = _write_pool(
        tmp_path,
        f"""
version = 1
name = "bad-sector"
description = "invalid sector"
base_market = "KR"

[[candidates]]
symbol = "900001"
market = "KR"
display_name = "SYNTH Alpha Display"
stock_code = "900001"
corp_name = "SYNTH-ALPHA"
yfinance_provider_symbol = "900001.KS"
currency = "KRW"
sector = "{sector}"
industry = "memory"
enabled = true
eligible = true
""",
    )
    with pytest.raises(KrCandidatePoolError) as exc_info:
        parse_kr_candidate_pool_toml(path)
    assert exc_info.value.stage == "parse"


def test_non_kr_market_rejected(tmp_path: Path) -> None:
    path = _write_pool(
        tmp_path,
        """
version = 1
name = "bad-market"
description = "US not supported"
base_market = "KR"

[[candidates]]
symbol = "900001"
market = "US"
display_name = "SYNTH Alpha Display"
stock_code = "900001"
corp_name = "SYNTH-ALPHA"
yfinance_provider_symbol = "900001.KS"
currency = "KRW"
sector = "semiconductors"
industry = "memory"
enabled = true
eligible = true
""",
    )
    with pytest.raises(KrCandidatePoolError) as exc_info:
        parse_kr_candidate_pool_toml(path)
    assert exc_info.value.stage == "parse"
    assert "KR" in exc_info.value.message


def test_yfinance_provider_symbol_suffix_required(tmp_path: Path) -> None:
    path = _write_pool(
        tmp_path,
        """
version = 1
name = "bad-yfinance"
description = "missing suffix"
base_market = "KR"

[[candidates]]
symbol = "900001"
market = "KR"
display_name = "SYNTH Alpha Display"
stock_code = "900001"
corp_name = "SYNTH-ALPHA"
yfinance_provider_symbol = "900001"
currency = "KRW"
sector = "semiconductors"
industry = "memory"
enabled = true
eligible = true
""",
    )
    with pytest.raises(KrCandidatePoolError) as exc_info:
        parse_kr_candidate_pool_toml(path)
    assert exc_info.value.stage == "parse"
    assert ".KS" in exc_info.value.message or ".KQ" in exc_info.value.message


def test_symbol_must_match_normalized_stock_code(tmp_path: Path) -> None:
    path = _write_pool(
        tmp_path,
        """
version = 1
name = "symbol-mismatch"
description = "symbol mismatch"
base_market = "KR"

[[candidates]]
symbol = "900002"
market = "KR"
display_name = "SYNTH Beta Display"
stock_code = "900001"
corp_name = "SYNTH-ALPHA"
yfinance_provider_symbol = "900001.KS"
currency = "KRW"
sector = "semiconductors"
industry = "memory"
enabled = true
eligible = true
""",
    )
    with pytest.raises(KrCandidatePoolError) as exc_info:
        parse_kr_candidate_pool_toml(path)
    assert exc_info.value.stage == "parse"
    assert "symbol must match" in exc_info.value.message


def test_duplicate_market_symbol_rejected(tmp_path: Path) -> None:
    path = _write_pool(
        tmp_path,
        """
version = 1
name = "duplicate"
description = "duplicate symbol"
base_market = "KR"

[[candidates]]
symbol = "900001"
market = "KR"
display_name = "SYNTH Alpha Display"
stock_code = "900001"
corp_name = "SYNTH-ALPHA"
yfinance_provider_symbol = "900001.KS"
currency = "KRW"
sector = "semiconductors"
industry = "memory"
enabled = true
eligible = true

[[candidates]]
symbol = "900001"
market = "KR"
display_name = "SYNTH Alpha Duplicate"
stock_code = "900001"
corp_name = "SYNTH-ALPHA"
yfinance_provider_symbol = "900001.KS"
currency = "KRW"
sector = "internet"
industry = "platform"
enabled = true
eligible = true
""",
    )
    with pytest.raises(KrCandidatePoolError) as exc_info:
        parse_kr_candidate_pool_toml(path)
    assert exc_info.value.stage == "parse"
    assert "duplicate" in exc_info.value.message


def test_selection_defaults_to_enabled_and_eligible_only() -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    selected = select_candidates(pool)
    symbols = [entry.symbol for entry in selected]
    assert symbols == ["900002", "900001", "900003"]
    assert "900004" not in symbols
    assert "900005" not in symbols


def test_sector_filter_works() -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    selected = select_candidates(pool, sectors={"semiconductors"})
    assert [entry.symbol for entry in selected] == ["900001", "900003"]


def test_max_per_sector_works() -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    selected = select_candidates(pool, max_per_sector=1)
    assert [entry.symbol for entry in selected] == ["900002", "900001"]


def test_max_total_works() -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    selected = select_candidates(pool, max_total=2)
    assert [entry.symbol for entry in selected] == ["900002", "900001"]


def test_selection_sort_is_deterministic_by_sector_priority_symbol() -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    selected = select_candidates(
        pool,
        sectors={"semiconductors", "internet"},
        include_disabled=True,
        include_ineligible=True,
    )
    assert [entry.symbol for entry in selected] == [
        "900002",
        "900004",
        "900001",
        "900003",
        "900005",
    ]


def test_exported_candidate_toml_loads_via_3f1_parser(tmp_path: Path) -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    out_path = tmp_path / "selected.toml"
    export_selected_candidates(
        pool,
        out_candidates=out_path,
        export_name="selected-test",
        export_description="selected for test",
        max_total=2,
        force=True,
    )
    document = parse_kr_candidates_toml(out_path)
    assert document.name == "selected-test"
    assert len(document.candidates) == 2


def test_exported_candidate_toml_root_has_no_base_market(tmp_path: Path) -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    out_path = tmp_path / "selected.toml"
    export_selected_candidates(
        pool,
        out_candidates=out_path,
        export_name="selected-test",
        export_description="selected for test",
        max_total=1,
        force=True,
    )
    raw = tomllib.loads(out_path.read_text(encoding="utf-8"))
    assert "base_market" not in raw


def test_exported_candidate_toml_entries_exclude_pool_only_fields(tmp_path: Path) -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    out_path = tmp_path / "selected.toml"
    export_selected_candidates(
        pool,
        out_candidates=out_path,
        export_name="selected-test",
        export_description="selected for test",
        max_total=3,
        force=True,
    )
    raw = tomllib.loads(out_path.read_text(encoding="utf-8"))
    forbidden = {"sector", "industry", "eligible", "priority", "notes", "corp_code"}
    for entry in raw["candidates"]:
        assert forbidden.isdisjoint(entry.keys())
        assert set(entry.keys()) == {
            "symbol",
            "market",
            "enabled",
            "display_name",
            "stock_code",
            "corp_name",
            "yfinance_provider_symbol",
            "currency",
        }


def test_exported_candidates_feed_3f1_generator_with_synthetic_corp_code(tmp_path: Path) -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    candidates_out = tmp_path / "selected.toml"
    export_selected_candidates(
        pool,
        out_candidates=candidates_out,
        export_name="selected-generator",
        export_description="selected for generator",
        sectors={"semiconductors", "internet"},
        max_total=3,
        max_per_sector=2,
        force=True,
    )
    universe_out = tmp_path / "universe.generated.toml"
    mapping_out = tmp_path / "provider_mappings.generated.toml"
    payload = generate_kr_provider_mapping_files(
        candidates_path=candidates_out,
        corp_code_xml=SYNTHETIC_CORP_CODE_XML,
        corp_code_zip=None,
        universe_out=universe_out,
        provider_mapping_out=mapping_out,
        universe_name="kr-selected-generated-v1",
        provider_mapping_name="kr-selected-provider-mappings-v1",
        force=True,
    )
    assert payload["candidates_read"] == 3


def test_generated_universe_and_mapping_validate(tmp_path: Path) -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    candidates_out = tmp_path / "selected.toml"
    export_selected_candidates(
        pool,
        out_candidates=candidates_out,
        export_name="selected-validate",
        export_description="selected for validate",
        max_total=2,
        force=True,
    )
    universe_out = tmp_path / "universe.generated.toml"
    mapping_out = tmp_path / "provider_mappings.generated.toml"
    generate_kr_provider_mapping_files(
        candidates_path=candidates_out,
        corp_code_xml=SYNTHETIC_CORP_CODE_XML,
        corp_code_zip=None,
        universe_out=universe_out,
        provider_mapping_out=mapping_out,
        universe_name="kr-selected-generated-v1",
        provider_mapping_name="kr-selected-provider-mappings-v1",
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


def test_selected_symbols_are_resolvable_by_synthetic_corp_code_fixture(tmp_path: Path) -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    candidates_out = tmp_path / "selected.toml"
    payload = export_selected_candidates(
        pool,
        out_candidates=candidates_out,
        export_name="selected-resolve",
        export_description="selected for resolve",
        max_total=3,
        force=True,
    )
    universe_out = tmp_path / "universe.generated.toml"
    mapping_out = tmp_path / "provider_mappings.generated.toml"
    generate_kr_provider_mapping_files(
        candidates_path=candidates_out,
        corp_code_xml=SYNTHETIC_CORP_CODE_XML,
        corp_code_zip=None,
        universe_out=universe_out,
        provider_mapping_out=mapping_out,
        universe_name="kr-selected-generated-v1",
        provider_mapping_name="kr-selected-provider-mappings-v1",
        force=True,
    )
    selected_symbols = {entry["symbol"] for entry in payload["selected"]}
    registry = load_provider_mapping_toml(mapping_out)
    for symbol in selected_symbols:
        mapping = registry.resolve(symbol=symbol, market="KR")
        assert mapping.dart is not None
        assert mapping.dart.corp_code.startswith("9000001")


def test_cli_writes_selected_candidates_to_tmp_path(tmp_path: Path) -> None:
    out_path = tmp_path / "selected.toml"
    result = _run_cli(
        "--candidate-pool",
        str(POOL_FIXTURE),
        "--sector",
        "semiconductors",
        "--sector",
        "internet",
        "--max-total",
        "3",
        "--max-per-sector",
        "2",
        "--out-candidates",
        str(out_path),
        "--force",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert out_path.is_file()
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["candidates_selected"] == 3
    validate_exported_candidates_toml(out_path)


def test_cli_refuses_overwrite_without_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from select_kr_candidates import main

    out_path = tmp_path / "selected.toml"
    argv_with_force = [
        "--candidate-pool",
        str(POOL_FIXTURE),
        "--out-candidates",
        str(out_path),
        "--max-total",
        "1",
        "--force",
        "--json",
    ]
    assert main(argv_with_force) == 0
    capsys.readouterr()
    argv_without_force = [
        "--candidate-pool",
        str(POOL_FIXTURE),
        "--out-candidates",
        str(out_path),
        "--max-total",
        "1",
        "--json",
    ]
    assert main(argv_without_force) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"


def test_cli_json_stdout_is_pure_json(tmp_path: Path) -> None:
    out_path = tmp_path / "selected.toml"
    result = _run_cli(
        "--candidate-pool",
        str(POOL_FIXTURE),
        "--out-candidates",
        str(out_path),
        "--max-total",
        "2",
        "--force",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert "selected" in payload


def test_empty_selection_fails_at_select_stage(tmp_path: Path) -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    with pytest.raises(KrCandidatePoolError) as exc_info:
        export_selected_candidates(
            pool,
            out_candidates=tmp_path / "empty.toml",
            export_name="empty",
            export_description="empty",
            sectors={"nonexistent-sector"},
            force=True,
        )
    assert exc_info.value.stage == "select"
    assert "zero candidates" in exc_info.value.message


def test_cli_empty_selection_fails_at_select_stage(tmp_path: Path) -> None:
    out_path = tmp_path / "selected.toml"
    result = _run_cli(
        "--candidate-pool",
        str(POOL_FIXTURE),
        "--sector",
        "nonexistent-sector",
        "--out-candidates",
        str(out_path),
        "--force",
        "--json",
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["stage"] == "select"
    assert not out_path.exists()


def test_new_pool_and_cli_files_have_no_forbidden_tokens() -> None:
    paths = [
        REPO_ROOT / "src" / "data" / "kr_candidate_pool.py",
        REPO_ROOT / "ops" / "select_kr_candidates.py",
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


def test_existing_3f1_and_3f2_tests_remain_importable() -> None:
    import test_kr_provider_mapping_generator  # noqa: F401
    import test_kr_real_generated_universe_expansion  # noqa: F401


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


def test_rendered_export_drops_pool_metadata_fields(tmp_path: Path) -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    selected = select_candidates(pool, max_total=1)
    rendered = render_selected_candidates_toml(
        name="drop-test",
        description="drop metadata",
        selected=selected,
    )
    out_path = tmp_path / "drop-test.toml"
    out_path.write_text(rendered, encoding="utf-8")
    raw = tomllib.loads(out_path.read_text(encoding="utf-8"))
    assert "base_market" not in raw
    forbidden = {"sector", "industry", "eligible", "priority", "notes", "corp_code"}
    for entry in raw["candidates"]:
        assert forbidden.isdisjoint(entry.keys())
