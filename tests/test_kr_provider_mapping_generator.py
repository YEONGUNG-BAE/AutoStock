"""Real Intake 3F1 — fixture-first KR universe/provider mapping generator tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CANDIDATES_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "kr_candidates" / "kr_real_candidates.sample.toml"
)
CORP_CODE_SAMPLE_XML = REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "corp_code_sample.xml"
DISAMBIGUATION_XML = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "dart"
    / "corp_code_duplicate_stock_disambiguation.xml"
)
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
OPS_SCRIPT = REPO_ROOT / "ops" / "generate_kr_provider_mapping.py"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data.kr_provider_mapping_generator import (
    KrProviderMappingGeneratorError,
    generate_kr_provider_mapping_files,
    parse_kr_candidates_toml,
)
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _write_candidates(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "candidates.toml"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _write_zip_from_xml(tmp_path: Path, xml_path: Path) -> Path:
    zip_path = tmp_path / "corp_code.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("CORPCODE.xml", xml_path.read_bytes())
    return zip_path


def _generate(
    tmp_path: Path,
    *,
    candidates_path: Path | None = None,
    corp_code_xml: Path | None = CORP_CODE_SAMPLE_XML,
    corp_code_zip: Path | None = None,
    force: bool = False,
) -> dict[str, object]:
    universe_out = tmp_path / "universe.generated.toml"
    mapping_out = tmp_path / "provider_mappings.generated.toml"
    return generate_kr_provider_mapping_files(
        candidates_path=candidates_path or CANDIDATES_FIXTURE,
        corp_code_xml=corp_code_xml,
        corp_code_zip=corp_code_zip,
        universe_out=universe_out,
        provider_mapping_out=mapping_out,
        universe_name="kr-real-generated-v1",
        provider_mapping_name="kr-real-provider-mappings-generated-v1",
        force=force,
    )


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


def test_candidate_fixture_parses() -> None:
    document = parse_kr_candidates_toml(CANDIDATES_FIXTURE)
    assert document.name == "kr-real-candidates-fixture-v1"
    assert len(document.candidates) == 2


def test_generated_universe_loads(tmp_path: Path) -> None:
    payload = _generate(tmp_path)
    universe = load_universe_toml(Path(str(payload["universe_out"])))
    assert universe.name == "kr-real-generated-v1"
    assert universe.base_market == "KR"
    assert len(universe.enabled_symbols) == 2


def test_generated_provider_mapping_loads(tmp_path: Path) -> None:
    payload = _generate(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    assert registry.name == "kr-real-provider-mappings-generated-v1"
    assert len(registry.mappings) == 2


def test_generated_mapping_covers_universe(tmp_path: Path) -> None:
    payload = _generate(tmp_path)
    universe = load_universe_toml(Path(str(payload["universe_out"])))
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    validate_provider_mappings_cover_universe(
        registry,
        universe,
        require_yfinance=True,
        require_dart=True,
    )


def test_corp_code_resolved_from_fixture_not_candidate(tmp_path: Path) -> None:
    payload = _generate(tmp_path)
    resolved = payload["resolved"]
    assert isinstance(resolved, list)
    by_symbol = {entry["symbol"]: entry for entry in resolved}
    assert by_symbol["005930"]["corp_code"] == "00126380"
    assert by_symbol["000660"]["corp_code"] == "00164779"


def test_candidate_provided_corp_code_rejected(tmp_path: Path) -> None:
    candidates = _write_candidates(
        tmp_path,
        """
version = 1
name = "bad-candidate"
description = "corp_code forbidden"

[[candidates]]
symbol = "005930"
market = "KR"
enabled = true
display_name = "Samsung"
stock_code = "005930"
corp_name = "삼성전자"
corp_code = "00126380"
yfinance_provider_symbol = "005930.KS"
currency = "KRW"
""",
    )
    with pytest.raises(KrProviderMappingGeneratorError) as exc_info:
        parse_kr_candidates_toml(candidates)
    assert exc_info.value.stage == "parse"
    assert "corp_code" in exc_info.value.message


def test_yfinance_provider_symbol_suffix_required(tmp_path: Path) -> None:
    candidates = _write_candidates(
        tmp_path,
        """
version = 1
name = "bad-yfinance"
description = "missing suffix"

[[candidates]]
symbol = "005930"
market = "KR"
enabled = true
display_name = "Samsung"
stock_code = "005930"
corp_name = "삼성전자"
yfinance_provider_symbol = "005930"
currency = "KRW"
""",
    )
    with pytest.raises(KrProviderMappingGeneratorError) as exc_info:
        parse_kr_candidates_toml(candidates)
    assert exc_info.value.stage == "parse"
    assert ".KS" in exc_info.value.message or ".KQ" in exc_info.value.message


def test_non_kr_market_rejected(tmp_path: Path) -> None:
    candidates = _write_candidates(
        tmp_path,
        """
version = 1
name = "bad-market"
description = "US not supported"

[[candidates]]
symbol = "005930"
market = "US"
enabled = true
display_name = "Samsung"
stock_code = "005930"
corp_name = "Samsung"
yfinance_provider_symbol = "005930.KS"
currency = "KRW"
""",
    )
    with pytest.raises(KrProviderMappingGeneratorError) as exc_info:
        parse_kr_candidates_toml(candidates)
    assert exc_info.value.stage == "parse"
    assert "KR" in exc_info.value.message


def test_symbol_must_match_normalized_stock_code(tmp_path: Path) -> None:
    candidates = _write_candidates(
        tmp_path,
        """
version = 1
name = "symbol-mismatch"
description = "symbol mismatch"

[[candidates]]
symbol = "005931"
market = "KR"
enabled = true
display_name = "Mismatch"
stock_code = "005930"
corp_name = "삼성전자"
yfinance_provider_symbol = "005930.KS"
currency = "KRW"
""",
    )
    with pytest.raises(KrProviderMappingGeneratorError) as exc_info:
        parse_kr_candidates_toml(candidates)
    assert exc_info.value.stage == "parse"
    assert "symbol must match" in exc_info.value.message


@pytest.mark.parametrize(
    ("raw_stock_code", "expected_symbol"),
    [
        ("5930", "005930"),
        ("KR:005930", "005930"),
    ],
)
def test_stock_code_normalization_in_generated_outputs(
    tmp_path: Path,
    raw_stock_code: str,
    expected_symbol: str,
) -> None:
    candidates = _write_candidates(
        tmp_path,
        f"""
version = 1
name = "normalize-stock-code"
description = "normalize stock_code"

[[candidates]]
symbol = "{expected_symbol}"
market = "KR"
enabled = true
display_name = "Samsung Electronics"
stock_code = "{raw_stock_code}"
corp_name = "삼성전자"
yfinance_provider_symbol = "005930.KS"
currency = "KRW"
""",
    )
    payload = _generate(tmp_path, candidates_path=candidates)
    mapping_raw = tomllib.loads(
        Path(str(payload["provider_mapping_out"])).read_text(encoding="utf-8")
    )
    entry = mapping_raw["mappings"][0]
    assert entry["stock_code"] == expected_symbol
    assert entry["dart"]["stock_code"] == expected_symbol


def test_duplicate_market_symbol_rejected(tmp_path: Path) -> None:
    candidates = _write_candidates(
        tmp_path,
        """
version = 1
name = "duplicate-symbol"
description = "duplicate symbol"

[[candidates]]
symbol = "005930"
market = "KR"
enabled = true
display_name = "Samsung"
stock_code = "005930"
corp_name = "삼성전자"
yfinance_provider_symbol = "005930.KS"
currency = "KRW"

[[candidates]]
symbol = "005930"
market = "KR"
enabled = true
display_name = "Samsung duplicate"
stock_code = "005930"
corp_name = "삼성전자"
yfinance_provider_symbol = "005930.KS"
currency = "KRW"
""",
    )
    with pytest.raises(KrProviderMappingGeneratorError) as exc_info:
        parse_kr_candidates_toml(candidates)
    assert exc_info.value.stage == "parse"
    assert "duplicate" in exc_info.value.message


def test_corp_name_disambiguation_uses_resolver_result(tmp_path: Path) -> None:
    candidates = _write_candidates(
        tmp_path,
        """
version = 1
name = "disambiguation"
description = "duplicate stock_code disambiguation"

[[candidates]]
symbol = "123456"
market = "KR"
enabled = true
display_name = "Test A Display"
stock_code = "123456"
corp_name = "테스트A"
yfinance_provider_symbol = "123456.KS"
currency = "KRW"
""",
    )
    payload = _generate(tmp_path, candidates_path=candidates, corp_code_xml=DISAMBIGUATION_XML)
    mapping_raw = tomllib.loads(
        Path(str(payload["provider_mapping_out"])).read_text(encoding="utf-8")
    )
    dart = mapping_raw["mappings"][0]["dart"]
    assert dart["corp_code"] == "90000001"
    assert dart["corp_name"] == "테스트A"


def test_unlisted_stock_code_fails_at_resolve(tmp_path: Path) -> None:
    candidates = _write_candidates(
        tmp_path,
        """
version = 1
name = "unlisted"
description = "missing corp_code"

[[candidates]]
symbol = "999999"
market = "KR"
enabled = true
display_name = "Missing"
stock_code = "999999"
corp_name = "없는회사"
yfinance_provider_symbol = "999999.KS"
currency = "KRW"
""",
    )
    with pytest.raises(KrProviderMappingGeneratorError) as exc_info:
        _generate(tmp_path, candidates_path=candidates)
    assert exc_info.value.stage == "resolve"


def test_korean_strings_round_trip_through_generated_toml(tmp_path: Path) -> None:
    payload = _generate(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    samsung = registry.resolve(symbol="005930", market="KR")
    hynix = registry.resolve(symbol="000660", market="KR")
    assert samsung.dart is not None
    assert hynix.dart is not None
    assert samsung.dart.corp_name == "삼성전자"
    assert hynix.dart.corp_name == "SK하이닉스"


def test_cli_requires_exactly_one_corp_code_source(tmp_path: Path) -> None:
    universe_out = tmp_path / "universe.toml"
    mapping_out = tmp_path / "mapping.toml"
    base = [
        "--candidates",
        str(CANDIDATES_FIXTURE),
        "--universe-out",
        str(universe_out),
        "--provider-mapping-out",
        str(mapping_out),
        "--universe-name",
        "kr-real-generated-v1",
        "--provider-mapping-name",
        "kr-real-provider-mappings-generated-v1",
        "--json",
    ]
    missing = _run_cli(*base)
    assert missing.returncode != 0

    both = _run_cli(
        *base,
        "--corp-code-xml",
        str(CORP_CODE_SAMPLE_XML),
        "--corp-code-zip",
        str(_write_zip_from_xml(tmp_path, CORP_CODE_SAMPLE_XML)),
    )
    assert both.returncode != 0


def test_cli_writes_outputs_with_xml_and_zip(tmp_path: Path) -> None:
    universe_out = tmp_path / "universe.toml"
    mapping_out = tmp_path / "mapping.toml"
    zip_path = _write_zip_from_xml(tmp_path, CORP_CODE_SAMPLE_XML)

    xml_result = _run_cli(
        "--candidates",
        str(CANDIDATES_FIXTURE),
        "--corp-code-xml",
        str(CORP_CODE_SAMPLE_XML),
        "--universe-out",
        str(universe_out),
        "--provider-mapping-out",
        str(mapping_out),
        "--universe-name",
        "kr-real-generated-v1",
        "--provider-mapping-name",
        "kr-real-provider-mappings-generated-v1",
        "--json",
    )
    assert xml_result.returncode == 0, xml_result.stderr
    assert universe_out.is_file()
    assert mapping_out.is_file()

    universe_out.unlink()
    mapping_out.unlink()
    zip_result = _run_cli(
        "--candidates",
        str(CANDIDATES_FIXTURE),
        "--corp-code-zip",
        str(zip_path),
        "--universe-out",
        str(universe_out),
        "--provider-mapping-out",
        str(mapping_out),
        "--universe-name",
        "kr-real-generated-v1",
        "--provider-mapping-name",
        "kr-real-provider-mappings-generated-v1",
        "--json",
    )
    assert zip_result.returncode == 0, zip_result.stderr
    assert universe_out.is_file()
    assert mapping_out.is_file()


def test_cli_refuses_overwrite_without_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from generate_kr_provider_mapping import main

    universe_out = tmp_path / "universe.toml"
    mapping_out = tmp_path / "mapping.toml"
    argv = [
        "--candidates",
        str(CANDIDATES_FIXTURE),
        "--corp-code-xml",
        str(CORP_CODE_SAMPLE_XML),
        "--universe-out",
        str(universe_out),
        "--provider-mapping-out",
        str(mapping_out),
        "--universe-name",
        "kr-real-generated-v1",
        "--provider-mapping-name",
        "kr-real-provider-mappings-generated-v1",
        "--json",
    ]
    assert main(argv) == 0
    capsys.readouterr()
    assert main(argv) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"


def test_cli_json_stdout_is_pure_json(tmp_path: Path) -> None:
    universe_out = tmp_path / "universe.toml"
    mapping_out = tmp_path / "mapping.toml"
    result = _run_cli(
        "--candidates",
        str(CANDIDATES_FIXTURE),
        "--corp-code-xml",
        str(CORP_CODE_SAMPLE_XML),
        "--universe-out",
        str(universe_out),
        "--provider-mapping-out",
        str(mapping_out),
        "--universe-name",
        "kr-real-generated-v1",
        "--provider-mapping-name",
        "kr-real-provider-mappings-generated-v1",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["candidates_read"] == 2


def test_config_sample_files_unchanged() -> None:
    universe_before = KR_REAL_UNIVERSE.read_text(encoding="utf-8")
    mapping_before = KR_REAL_MAPPING.read_text(encoding="utf-8")
    assert "kr-real-sample-v0" in universe_before
    assert "kr-real-provider-mappings-v1" in mapping_before


def test_ops_script_has_no_forbidden_tokens() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
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
        assert token not in source, f"generate_kr_provider_mapping.py must not reference {token!r}"


def test_no_runtime_files_tracked_in_repo() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runtime"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert tracked.returncode == 0
    assert tracked.stdout.strip() == ""


def test_existing_3e_smoke_tests_remain_importable() -> None:
    import test_combined_context_budget  # noqa: F401
    import test_kr_real_dart_smoke  # noqa: F401
    import test_kr_real_price_smoke  # noqa: F401
    import test_kr_real_sample_universe  # noqa: F401
