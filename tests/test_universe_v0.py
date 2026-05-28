from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain.universe import UniverseDefinition, UniverseSymbol, load_universe_toml

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_UNIVERSE = REPO_ROOT / "config" / "universe.paper.toml.example"


def _write_universe(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_valid_universe_toml_loads_successfully(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.toml"
    _write_universe(
        universe_path,
        """
version = 1
name = "test-universe"
description = "Synthetic test universe."
base_market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true
""",
    )

    universe = load_universe_toml(universe_path)

    assert universe.name == "test-universe"
    assert universe.base_market == "KR"
    assert len(universe.enabled_symbols) == 1


def test_universe_rejects_blank_name(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.toml"
    _write_universe(
        universe_path,
        """
version = 1
name = "   "
description = "desc"
base_market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
""",
    )

    with pytest.raises(ValueError, match="name must not be blank"):
        load_universe_toml(universe_path)


def test_universe_rejects_duplicate_market_symbol(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.toml"
    _write_universe(
        universe_path,
        """
version = 1
name = "dup"
description = "desc"
base_market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = false
""",
    )

    with pytest.raises(ValueError, match="duplicate universe symbol entry"):
        load_universe_toml(universe_path)


def test_universe_rejects_no_enabled_symbols(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.toml"
    _write_universe(
        universe_path,
        """
version = 1
name = "disabled-only"
description = "desc"
base_market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = false
""",
    )

    with pytest.raises(ValueError, match="at least one enabled symbol"):
        load_universe_toml(universe_path)


def test_universe_rejects_invalid_base_market(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.toml"
    _write_universe(
        universe_path,
        """
version = 1
name = "bad-base"
description = "desc"
base_market = "EU"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
""",
    )

    with pytest.raises(ValueError, match="base_market must be one of"):
        load_universe_toml(universe_path)


def test_universe_treats_base_market_as_metadata_only(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.toml"
    _write_universe(
        universe_path,
        """
version = 1
name = "mixed"
description = "desc"
base_market = "KR"

[[symbols]]
symbol = "SYNTH-US-0001"
market = "US"
enabled = true
""",
    )

    universe = load_universe_toml(universe_path)
    assert universe.base_market == "KR"
    assert universe.enabled_symbols[0].market == "US"


def test_config_universe_paper_example_loads_successfully() -> None:
    universe = load_universe_toml(EXAMPLE_UNIVERSE)
    assert universe.name == "paper-v0"
    assert any(entry.symbol == "SYNTH-KR-0001" and entry.enabled for entry in universe.symbols)
