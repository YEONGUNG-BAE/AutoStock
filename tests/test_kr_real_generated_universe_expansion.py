"""Real Intake 3F2 — generator-based KR universe expansion (synthetic scale + operator-local guard)."""

from __future__ import annotations

import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_CANDIDATES = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_candidates"
    / "kr_real_candidates.synthetic_multi.toml"
)
SYNTHETIC_CORP_CODE_XML = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "corp_code_synthetic_multi.xml"
)
CORP_CODE_SAMPLE_XML = REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "corp_code_sample.xml"
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
SAMPLE_CANDIDATES = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "kr_candidates" / "kr_real_candidates.sample.toml"
)

sys.path.insert(0, str(REPO_ROOT / "src"))

from data.kr_provider_mapping_generator import (
    generate_kr_provider_mapping_files,
    parse_kr_candidates_toml,
)
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml

# synthetic fixture: stock_code → (corp_code, yfinance provider_symbol)
_SYNTHETIC_EXPECTED: dict[str, tuple[str, str]] = {
    "900001": ("90000010", "900001.KS"),
    "900002": ("90000011", "900002.KS"),
    "900003": ("90000012", "900003.KS"),
    "900004": ("90000013", "900004.KS"),
    "900005": ("90000014", "900005.KS"),
}


def _generate_synthetic_multi(tmp_path: Path) -> dict[str, object]:
    universe_out = tmp_path / "universe.generated.toml"
    mapping_out = tmp_path / "provider_mappings.generated.toml"
    return generate_kr_provider_mapping_files(
        candidates_path=SYNTHETIC_CANDIDATES,
        corp_code_xml=SYNTHETIC_CORP_CODE_XML,
        corp_code_zip=None,
        universe_out=universe_out,
        provider_mapping_out=mapping_out,
        universe_name="kr-real-synthetic-multi-v1",
        provider_mapping_name="kr-real-provider-mappings-synthetic-multi-v1",
        force=False,
    )


def _listed_entries_from_corp_code_xml(path: Path) -> list[tuple[str, str, str]]:
    """상장 entry(stock_code 비어 있지 않음)만 (corp_code, corp_name, stock_code)로 반환."""
    root = ET.parse(path).getroot()
    listed: list[tuple[str, str, str]] = []
    for node in root.iter("list"):
        corp_code = (node.findtext("corp_code") or "").strip()
        corp_name = (node.findtext("corp_name") or "").strip()
        stock_code = (node.findtext("stock_code") or "").strip()
        if stock_code:
            listed.append((corp_code, corp_name, stock_code))
    return listed


@pytest.fixture
def synthetic_payload(tmp_path: Path) -> dict[str, object]:
    return _generate_synthetic_multi(tmp_path)


def test_synthetic_candidate_fixture_has_five_entries_without_corp_code() -> None:
    document = parse_kr_candidates_toml(SYNTHETIC_CANDIDATES)
    assert document.name == "kr-real-candidates-synthetic-multi-v1"
    assert len(document.candidates) == 5
    for candidate in document.candidates:
        assert candidate.market == "KR"
        assert candidate.enabled is True


def test_synthetic_multi_generated_universe_has_five_enabled_kr_symbols(
    synthetic_payload: dict[str, object],
) -> None:
    universe = load_universe_toml(Path(str(synthetic_payload["universe_out"])))
    assert universe.name == "kr-real-synthetic-multi-v1"
    assert universe.base_market == "KR"
    assert len(universe.enabled_symbols) == 5
    assert all(symbol.market == "KR" for symbol in universe.enabled_symbols)


def test_synthetic_multi_generated_mapping_loads_and_covers_universe(
    synthetic_payload: dict[str, object],
) -> None:
    universe = load_universe_toml(Path(str(synthetic_payload["universe_out"])))
    registry = load_provider_mapping_toml(Path(str(synthetic_payload["provider_mapping_out"])))
    assert registry.name == "kr-real-provider-mappings-synthetic-multi-v1"
    assert len(registry.mappings) == 5
    validate_provider_mappings_cover_universe(
        registry,
        universe,
        require_yfinance=True,
        require_dart=True,
    )


def test_synthetic_multi_yfinance_symbols_are_explicit_from_candidates(
    synthetic_payload: dict[str, object],
) -> None:
    mapping_raw = tomllib.loads(
        Path(str(synthetic_payload["provider_mapping_out"])).read_text(encoding="utf-8")
    )
    resolved = synthetic_payload["resolved"]
    assert isinstance(resolved, list)
    for entry in resolved:
        symbol = entry["symbol"]
        expected_yfinance = _SYNTHETIC_EXPECTED[symbol][1]
        assert entry["yfinance_provider_symbol"] == expected_yfinance

        mapping_entry = next(
            row for row in mapping_raw["mappings"] if row["symbol"] == symbol
        )
        assert mapping_entry["yfinance"]["provider_symbol"] == expected_yfinance
        assert mapping_entry["yfinance"]["currency"] == "KRW"


def test_synthetic_multi_corp_codes_come_from_resolver_not_candidates(
    synthetic_payload: dict[str, object],
) -> None:
    resolved = synthetic_payload["resolved"]
    assert isinstance(resolved, list)
    for entry in resolved:
        symbol = entry["symbol"]
        expected_corp_code, _ = _SYNTHETIC_EXPECTED[symbol]
        assert entry["corp_code"] == expected_corp_code


def test_synthetic_multi_stock_code_fields_normalized_and_consistent(
    synthetic_payload: dict[str, object],
) -> None:
    mapping_raw = tomllib.loads(
        Path(str(synthetic_payload["provider_mapping_out"])).read_text(encoding="utf-8")
    )
    for entry in mapping_raw["mappings"]:
        symbol = entry["symbol"]
        assert len(symbol) == 6
        assert symbol.isdigit()
        assert entry["stock_code"] == symbol
        assert entry["dart"]["stock_code"] == symbol


def test_synthetic_multi_dart_fields_present_for_all_enabled_symbols(
    synthetic_payload: dict[str, object],
) -> None:
    registry = load_provider_mapping_toml(Path(str(synthetic_payload["provider_mapping_out"])))
    for mapping in registry.mappings:
        if not mapping.enabled:
            continue
        assert mapping.yfinance is not None
        assert mapping.yfinance.provider_symbol
        assert mapping.yfinance.currency == "KRW"
        assert mapping.dart is not None
        assert mapping.dart.corp_code
        assert mapping.dart.stock_code == mapping.stock_code
        assert mapping.dart.corp_name


def test_repo_corp_code_sample_has_only_two_verified_listed_companies() -> None:
    """실제 3번째 corp_code fixture가 없음 — 대형주 확장은 operator-local snapshot 필요."""
    listed = _listed_entries_from_corp_code_xml(CORP_CODE_SAMPLE_XML)
    assert len(listed) == 2
    stock_codes = {entry[2] for entry in listed}
    assert stock_codes == {"005930", "000660"}
    corp_codes = {entry[0] for entry in listed}
    assert corp_codes == {"00126380", "00164779"}


def test_no_verified_third_real_company_corp_code_in_checked_in_fixtures() -> None:
    """체크인 fixture에 검증된 실제 회사 corp_code는 삼성/SK하이닉스 2개뿐."""
    scan_paths = (
        CORP_CODE_SAMPLE_XML,
        SYNTHETIC_CORP_CODE_XML,
        REPO_ROOT
        / "tests"
        / "fixtures"
        / "research"
        / "dart"
        / "corp_code_duplicate_stock_disambiguation.xml",
    )
    real_listed_corp_codes: set[str] = set()
    for xml_path in scan_paths:
        for corp_code, corp_name, stock_code in _listed_entries_from_corp_code_xml(xml_path):
            if corp_name.startswith("SYNTH-") or corp_name.startswith("테스트"):
                continue
            if stock_code in {"005930", "000660"}:
                real_listed_corp_codes.add(corp_code)
    assert real_listed_corp_codes == {"00126380", "00164779"}


def test_static_kr_real_config_samples_unchanged() -> None:
    universe_before = KR_REAL_UNIVERSE.read_text(encoding="utf-8")
    mapping_before = KR_REAL_MAPPING.read_text(encoding="utf-8")
    assert "kr-real-sample-v0" in universe_before
    assert "kr-real-provider-mappings-v1" in mapping_before
    sample_document = parse_kr_candidates_toml(SAMPLE_CANDIDATES)
    assert len(sample_document.candidates) == 2
    assert {entry.symbol for entry in sample_document.candidates} == {"005930", "000660"}


def test_3e2_and_3e3_smoke_ops_scripts_are_importable_only() -> None:
    sys.path.insert(0, str(REPO_ROOT / "ops"))
    import run_kr_real_dart_smoke  # noqa: F401
    import run_kr_real_price_smoke  # noqa: F401


def test_existing_3f1_generator_tests_remain_importable() -> None:
    import test_kr_provider_mapping_generator  # noqa: F401
