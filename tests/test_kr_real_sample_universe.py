"""Real Intake 3E1 — static KR real-company sample universe + provider mapping validation."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
PAPER_UNIVERSE = REPO_ROOT / "config" / "universe.paper.toml.example"
PAPER_MAPPING = REPO_ROOT / "config" / "provider_mappings.paper.toml.example"
CORP_CODE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "corp_code_sample.xml"
OPS_SCRIPT = REPO_ROOT / "ops" / "validate_provider_mapping.py"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data.dart_corp_code_resolver import (
    parse_corp_code_xml_file,
    resolve_corp_code_by_stock_code,
)
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _load_kr_real_universe():
    return load_universe_toml(KR_REAL_UNIVERSE)


def _load_kr_real_registry():
    return load_provider_mapping_toml(KR_REAL_MAPPING)


def test_kr_real_universe_toml_loads() -> None:
    universe = _load_kr_real_universe()
    assert universe.name == "kr-real-sample-v0"
    assert universe.version == 1


def test_kr_real_universe_base_market_is_kr() -> None:
    universe = _load_kr_real_universe()
    assert universe.base_market == "KR"


def test_kr_real_universe_has_exactly_two_enabled_symbols() -> None:
    universe = _load_kr_real_universe()
    assert len(universe.enabled_symbols) == 2


def test_kr_real_universe_has_no_synth_symbols() -> None:
    universe = _load_kr_real_universe()
    for entry in universe.symbols:
        assert not entry.symbol.startswith("SYNTH-"), entry.symbol


def test_kr_real_enabled_symbols_are_kr_market() -> None:
    universe = _load_kr_real_universe()
    for entry in universe.enabled_symbols:
        assert entry.market == "KR"


def test_kr_real_symbols_use_display_name_not_description() -> None:
    universe = _load_kr_real_universe()
    for entry in universe.enabled_symbols:
        assert entry.display_name is not None
        assert entry.display_name.strip()

    raw = tomllib.loads(KR_REAL_UNIVERSE.read_text(encoding="utf-8"))
    for index, symbol_entry in enumerate(raw.get("symbols", [])):
        assert "description" not in symbol_entry, f"symbols[{index}] must not use description"


def test_kr_real_enabled_symbols_have_enabled_provider_mappings() -> None:
    universe = _load_kr_real_universe()
    registry = _load_kr_real_registry()
    for entry in universe.enabled_symbols:
        mapping = registry.resolve(symbol=entry.symbol, market=entry.market)
        assert mapping.enabled is True


def test_kr_real_provider_mapping_toml_loads() -> None:
    registry = _load_kr_real_registry()
    assert registry.name == "kr-real-provider-mappings-v1"
    assert len(registry.enabled_mappings) == 2


def test_kr_real_provider_mapping_coverage_with_yfinance_and_dart() -> None:
    universe = _load_kr_real_universe()
    registry = _load_kr_real_registry()
    validate_provider_mappings_cover_universe(
        registry,
        universe,
        require_yfinance=True,
        require_dart=True,
    )


def test_kr_real_provider_mappings_have_required_kr_fields() -> None:
    registry = _load_kr_real_registry()
    suffix_pattern = re.compile(r"\.K[QS]$")
    corp_code_pattern = re.compile(r"^\d{8}$")

    for mapping in registry.enabled_mappings:
        assert mapping.stock_code is not None
        assert mapping.yfinance is not None
        assert suffix_pattern.search(mapping.yfinance.provider_symbol)
        assert mapping.dart is not None
        assert corp_code_pattern.fullmatch(mapping.dart.corp_code)
        assert mapping.stock_code == mapping.dart.stock_code


def test_kr_real_corp_code_provenance_matches_local_fixture() -> None:
    universe = _load_kr_real_universe()
    registry = _load_kr_real_registry()
    entries = parse_corp_code_xml_file(CORP_CODE_FIXTURE)

    for universe_symbol in universe.enabled_symbols:
        mapping = registry.resolve(
            symbol=universe_symbol.symbol,
            market=universe_symbol.market,
        )
        assert mapping.dart is not None
        assert mapping.stock_code is not None

        resolved = resolve_corp_code_by_stock_code(entries, mapping.stock_code)
        assert resolved.corp_code == mapping.dart.corp_code
        assert resolved.corp_name == mapping.dart.corp_name


def test_kr_real_validate_provider_mapping_cli_json_ok(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from validate_provider_mapping import main

    exit_code = main(
        [
            "--universe",
            str(KR_REAL_UNIVERSE),
            "--provider-mapping",
            str(KR_REAL_MAPPING),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["universe_name"] == "kr-real-sample-v0"
    assert payload["mapping_name"] == "kr-real-provider-mappings-v1"
    assert payload["enabled_universe_symbols"] == 2
    assert payload["enabled_mappings"] == 2


def test_kr_real_config_files_have_no_env_or_api_key_references() -> None:
    for path in (KR_REAL_UNIVERSE, KR_REAL_MAPPING):
        source = path.read_text(encoding="utf-8")
        assert "os.environ" not in source
        assert "getenv" not in source
        for prefix, suffix in (("DART_", "API_KEY"), ("FRED_", "API_KEY")):
            assert prefix + suffix not in source, f"{path.name} must not reference API key env names"


def test_kr_real_sample_does_not_modify_paper_example_files() -> None:
    assert PAPER_UNIVERSE.is_file()
    assert PAPER_MAPPING.is_file()
    paper_universe = PAPER_UNIVERSE.read_text(encoding="utf-8")
    paper_mapping = PAPER_MAPPING.read_text(encoding="utf-8")
    assert "SYNTH-KR-0001" in paper_universe
    assert "SYNTH-US-0001" in paper_universe
    assert "paper-v0" in paper_universe
    assert "paper-provider-mappings-v1" in paper_mapping
    assert "SYNTH-KR-0001" in paper_mapping
