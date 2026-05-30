from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_UNIVERSE = REPO_ROOT / "config" / "universe.paper.toml.example"
EXAMPLE_MAPPING = REPO_ROOT / "config" / "provider_mappings.paper.toml.example"
OPS_SCRIPT = REPO_ROOT / "ops" / "validate_provider_mapping.py"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data.provider_mapping_registry import (
    ProviderMappingError,
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _write_mapping(path: Path, content: str) -> None:
    path.write_text(content.strip() + "\n", encoding="utf-8")


def _write_universe(path: Path, content: str) -> None:
    path.write_text(content.strip() + "\n", encoding="utf-8")


def test_valid_registry_toml_loads() -> None:
    registry = load_provider_mapping_toml(EXAMPLE_MAPPING)
    assert registry.version == 1
    assert registry.name == "paper-provider-mappings-v1"
    assert len(registry.mappings) == 2
    assert len(registry.enabled_mappings) == 1


def test_resolve_kr_synth_symbol_returns_provider_ids() -> None:
    registry = load_provider_mapping_toml(EXAMPLE_MAPPING)
    entry = registry.resolve(symbol="SYNTH-KR-0001", market="KR")
    assert entry.yfinance is not None
    assert entry.yfinance.provider_symbol == "005930.KS"
    assert entry.dart is not None
    assert entry.dart.corp_code == "00126380"
    assert entry.stock_code == "005930"
    assert entry.dart.stock_code == "005930"


def test_duplicate_market_symbol_fails(tmp_path: Path) -> None:
    mapping_path = tmp_path / "dup.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "dup"
description = "dup"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true
""",
    )
    with pytest.raises(ProviderMappingError, match="duplicate provider mapping"):
        load_provider_mapping_toml(mapping_path)


def test_blank_symbol_fails(tmp_path: Path) -> None:
    mapping_path = tmp_path / "blank_symbol.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "blank"
description = "blank"

[[mappings]]
symbol = "   "
market = "KR"
enabled = true
""",
    )
    with pytest.raises(ProviderMappingError, match="symbol must not be blank"):
        load_provider_mapping_toml(mapping_path)


def test_blank_market_fails(tmp_path: Path) -> None:
    mapping_path = tmp_path / "blank_market.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "blank"
description = "blank"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "   "
enabled = true
""",
    )
    with pytest.raises(ProviderMappingError, match="market must not be blank"):
        load_provider_mapping_toml(mapping_path)


def test_version_not_one_fails(tmp_path: Path) -> None:
    mapping_path = tmp_path / "version.toml"
    _write_mapping(
        mapping_path,
        """
version = 2
name = "bad-version"
description = "bad"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true
""",
    )
    with pytest.raises(ProviderMappingError, match="version must be exactly 1"):
        load_provider_mapping_toml(mapping_path)


def test_kr_yfinance_without_suffix_fails(tmp_path: Path) -> None:
    mapping_path = tmp_path / "kr_yfinance.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "kr-yfinance"
description = "kr"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true

[mappings.yfinance]
provider_symbol = "005930"
""",
    )
    with pytest.raises(ProviderMappingError, match=r"\.KS or \.KQ"):
        load_provider_mapping_toml(mapping_path)


def test_kr_yfinance_kq_suffix_passes(tmp_path: Path) -> None:
    mapping_path = tmp_path / "kr_kq.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "kr-kq"
description = "kr"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true

[mappings.yfinance]
provider_symbol = "035720.KQ"
""",
    )
    registry = load_provider_mapping_toml(mapping_path)
    entry = registry.resolve(symbol="SYNTH-KR-0001", market="KR")
    assert entry.yfinance is not None
    assert entry.yfinance.provider_symbol == "035720.KQ"


def test_kr_dart_corp_code_not_eight_digits_fails(tmp_path: Path) -> None:
    mapping_path = tmp_path / "dart_corp_code.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "dart-corp"
description = "dart"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true

[mappings.dart]
corp_code = "1234567"
stock_code = "005930"
""",
    )
    with pytest.raises(ProviderMappingError, match="exactly 8 digits"):
        load_provider_mapping_toml(mapping_path)


def test_kr_dart_invalid_stock_code_fails(tmp_path: Path) -> None:
    mapping_path = tmp_path / "dart_stock.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "dart-stock"
description = "dart"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true

[mappings.dart]
corp_code = "00126380"
stock_code = "ABC"
""",
    )
    with pytest.raises(ProviderMappingError, match="numeric"):
        load_provider_mapping_toml(mapping_path)


def test_kr_unpadded_stock_code_normalizes_to_six_digits(tmp_path: Path) -> None:
    mapping_path = tmp_path / "unpadded.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "unpadded"
description = "unpadded"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true
stock_code = "5930"

[mappings.dart]
corp_code = "00126380"
stock_code = "5930"
""",
    )
    registry = load_provider_mapping_toml(mapping_path)
    entry = registry.resolve(symbol="SYNTH-KR-0001", market="KR")
    assert entry.stock_code == "005930"
    assert entry.dart is not None
    assert entry.dart.stock_code == "005930"


def test_kr_prefixed_stock_code_normalizes(tmp_path: Path) -> None:
    mapping_path = tmp_path / "prefixed.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "prefixed"
description = "prefixed"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true
stock_code = "KR:005930"
""",
    )
    registry = load_provider_mapping_toml(mapping_path)
    entry = registry.resolve(symbol="SYNTH-KR-0001", market="KR")
    assert entry.stock_code == "005930"


def test_us_mapping_with_dart_provider_fails(tmp_path: Path) -> None:
    mapping_path = tmp_path / "us_dart.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "us-dart"
description = "us"

[[mappings]]
symbol = "SYNTH-US-0001"
market = "US"
enabled = false

[mappings.dart]
corp_code = "00126380"
stock_code = "005930"
""",
    )
    with pytest.raises(ProviderMappingError, match="must not include DART"):
        load_provider_mapping_toml(mapping_path)


def test_disabled_entries_parse_but_not_enabled(tmp_path: Path) -> None:
    registry = load_provider_mapping_toml(EXAMPLE_MAPPING)
    disabled = registry.resolve(symbol="SYNTH-US-0001", market="US")
    assert disabled.enabled is False
    assert len(registry.enabled_mappings) == 1


def test_disabled_entry_invalid_schema_still_fails(tmp_path: Path) -> None:
    mapping_path = tmp_path / "disabled_invalid.toml"
    _write_mapping(
        mapping_path,
        """
version = 1
name = "disabled-invalid"
description = "disabled"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = false

[mappings.yfinance]
provider_symbol = "005930"
""",
    )
    with pytest.raises(ProviderMappingError, match=r"\.KS or \.KQ"):
        load_provider_mapping_toml(mapping_path)


def test_enabled_universe_symbol_missing_mapping_fails(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.toml"
    mapping_path = tmp_path / "mapping.toml"
    _write_universe(
        universe_path,
        """
version = 1
name = "paper-v0"
description = "test"
base_market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true
""",
    )
    _write_mapping(
        mapping_path,
        """
version = 1
name = "empty-ish"
description = "empty-ish"

[[mappings]]
symbol = "SYNTH-KR-9999"
market = "KR"
enabled = true

[mappings.yfinance]
provider_symbol = "005930.KS"
""",
    )
    universe = load_universe_toml(universe_path)
    registry = load_provider_mapping_toml(mapping_path)
    with pytest.raises(ProviderMappingError, match="missing provider mapping"):
        validate_provider_mappings_cover_universe(registry, universe)


def test_enabled_universe_symbol_mapped_to_disabled_registry_entry_fails(
    tmp_path: Path,
) -> None:
    universe_path = tmp_path / "universe.toml"
    mapping_path = tmp_path / "mapping.toml"
    _write_universe(
        universe_path,
        """
version = 1
name = "paper-v0"
description = "test"
base_market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true
""",
    )
    _write_mapping(
        mapping_path,
        """
version = 1
name = "disabled-entry"
description = "disabled-entry"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = false

[mappings.yfinance]
provider_symbol = "005930.KS"

[mappings.dart]
corp_code = "00126380"
stock_code = "005930"
""",
    )
    universe = load_universe_toml(universe_path)
    registry = load_provider_mapping_toml(mapping_path)
    with pytest.raises(ProviderMappingError, match="disabled registry entry"):
        validate_provider_mappings_cover_universe(registry, universe)


def test_example_universe_and_mapping_coverage_passes() -> None:
    universe = load_universe_toml(EXAMPLE_UNIVERSE)
    registry = load_provider_mapping_toml(EXAMPLE_MAPPING)
    validate_provider_mappings_cover_universe(registry, universe)


def test_require_dart_false_permits_kr_mapping_without_dart(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.toml"
    mapping_path = tmp_path / "mapping.toml"
    _write_universe(
        universe_path,
        """
version = 1
name = "paper-v0"
description = "test"
base_market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true
""",
    )
    _write_mapping(
        mapping_path,
        """
version = 1
name = "no-dart"
description = "no-dart"

[[mappings]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true

[mappings.yfinance]
provider_symbol = "005930.KS"
""",
    )
    universe = load_universe_toml(universe_path)
    registry = load_provider_mapping_toml(mapping_path)
    validate_provider_mappings_cover_universe(
        registry,
        universe,
        require_dart=False,
    )


def test_cli_success_json(capsys: pytest.CaptureFixture[str]) -> None:
    from validate_provider_mapping import main

    exit_code = main(
        [
            "--universe",
            str(EXAMPLE_UNIVERSE),
            "--provider-mapping",
            str(EXAMPLE_MAPPING),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["universe_name"] == "paper-v0"
    assert payload["mapping_name"] == "paper-provider-mappings-v1"
    assert payload["enabled_universe_symbols"] == 1
    assert payload["enabled_mappings"] == 1


def test_cli_missing_mapping_json_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from validate_provider_mapping import main

    universe_path = tmp_path / "universe.toml"
    mapping_path = tmp_path / "mapping.toml"
    _write_universe(
        universe_path,
        """
version = 1
name = "paper-v0"
description = "test"
base_market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true
""",
    )
    _write_mapping(
        mapping_path,
        """
version = 1
name = "missing"
description = "missing"

[[mappings]]
symbol = "SYNTH-KR-9999"
market = "KR"
enabled = true

[mappings.yfinance]
provider_symbol = "005930.KS"
""",
    )
    exit_code = main(
        [
            "--universe",
            str(universe_path),
            "--provider-mapping",
            str(mapping_path),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "error"
    assert payload["stage"] == "validate"


def test_registry_module_has_no_forbidden_tokens() -> None:
    paths = (
        REPO_ROOT / "src" / "data" / "provider_mapping_registry.py",
        REPO_ROOT / "ops" / "validate_provider_mapping.py",
    )
    forbidden_network = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
    )
    forbidden_secrets = ("DART_API_KEY", "FRED_API_KEY")
    for path in paths:
        source = path.read_text(encoding="utf-8")
        lowered = source.lower()
        for token in forbidden_network:
            assert token not in lowered, f"{path.name} must not reference {token!r}"
        for token in forbidden_secrets:
            assert token not in source, f"{path.name} must not reference {token!r}"
        assert "from yfinance" not in lowered
        assert "import yfinance" not in lowered
