from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "research" / "dart"
SAMPLE_XML = FIXTURES / "corp_code_sample.xml"
MISSING_CORP_CODE_XML = FIXTURES / "corp_code_missing_corp_code.xml"
MISSING_CORP_NAME_XML = FIXTURES / "corp_code_missing_corp_name.xml"
DUPLICATE_STOCK_XML = FIXTURES / "corp_code_duplicate_stock.xml"
INVALID_XML = FIXTURES / "corp_code_invalid.xml"
OPS_SCRIPT = REPO_ROOT / "ops" / "resolve_dart_corp_code.py"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data.dart_corp_code_resolver import (
    DartCorpCodeEntry,
    DartCorpCodeResolverError,
    normalize_stock_code,
    parse_corp_code_xml_file,
    parse_corp_code_xml_text,
    parse_corp_code_zip_file,
    resolve_corp_code_by_stock_code,
)


def test_parse_valid_xml_returns_entries() -> None:
    entries = parse_corp_code_xml_file(SAMPLE_XML)
    assert len(entries) == 3
    listed = [entry for entry in entries if entry.stock_code is not None]
    assert len(listed) == 2


def test_resolve_005930_to_samsung() -> None:
    entries = parse_corp_code_xml_file(SAMPLE_XML)
    match = resolve_corp_code_by_stock_code(entries, "005930")
    assert match.corp_code == "00126380"
    assert match.corp_name == "삼성전자"
    assert match.stock_code == "005930"
    assert match.modify_date == "20240101"


def test_resolve_unpadded_5930_to_005930() -> None:
    entries = parse_corp_code_xml_file(SAMPLE_XML)
    match = resolve_corp_code_by_stock_code(entries, "5930")
    assert match.stock_code == "005930"
    assert match.corp_code == "00126380"


def test_resolve_kr_prefixed_stock_code() -> None:
    entries = parse_corp_code_xml_file(SAMPLE_XML)
    match = resolve_corp_code_by_stock_code(entries, "KR:005930")
    assert match.corp_code == "00126380"


def test_resolve_000660_to_sk_hynix() -> None:
    entries = parse_corp_code_xml_file(SAMPLE_XML)
    match = resolve_corp_code_by_stock_code(entries, "000660")
    assert match.corp_code == "00164779"
    assert match.corp_name == "SK하이닉스"


def test_blank_stock_code_entries_are_parsed_but_not_matched() -> None:
    entries = parse_corp_code_xml_file(SAMPLE_XML)
    unlisted = next(entry for entry in entries if entry.corp_name == "비상장샘플법인")
    assert unlisted.stock_code is None
    with pytest.raises(DartCorpCodeResolverError, match="no corp_code match"):
        resolve_corp_code_by_stock_code(entries, "123456")


def test_missing_corp_code_fails() -> None:
    with pytest.raises(DartCorpCodeResolverError, match="corp_code is required"):
        parse_corp_code_xml_file(MISSING_CORP_CODE_XML)


def test_missing_corp_name_fails() -> None:
    with pytest.raises(DartCorpCodeResolverError, match="corp_name is required"):
        parse_corp_code_xml_file(MISSING_CORP_NAME_XML)


def test_invalid_xml_fails() -> None:
    with pytest.raises(DartCorpCodeResolverError, match="invalid corp-code XML"):
        parse_corp_code_xml_file(INVALID_XML)


def test_duplicate_stock_code_fails_without_corp_name() -> None:
    entries = parse_corp_code_xml_file(DUPLICATE_STOCK_XML)
    with pytest.raises(DartCorpCodeResolverError, match="ambiguous"):
        resolve_corp_code_by_stock_code(entries, "005930")


def test_duplicate_stock_code_disambiguates_with_corp_name() -> None:
    entries = parse_corp_code_xml_file(DUPLICATE_STOCK_XML)
    match = resolve_corp_code_by_stock_code(entries, "005930", corp_name="삼성전자중복")
    assert match.corp_code == "00126381"


def test_no_match_fails_clearly() -> None:
    entries = parse_corp_code_xml_file(SAMPLE_XML)
    with pytest.raises(DartCorpCodeResolverError, match="no corp_code match"):
        resolve_corp_code_by_stock_code(entries, "123456")


def test_blank_stock_code_input_fails() -> None:
    entries = parse_corp_code_xml_file(SAMPLE_XML)
    with pytest.raises(DartCorpCodeResolverError, match="must not be blank"):
        resolve_corp_code_by_stock_code(entries, "  ")


def test_normalize_stock_code_rejects_non_numeric() -> None:
    with pytest.raises(DartCorpCodeResolverError, match="numeric"):
        normalize_stock_code("ABC")


def test_parse_corp_code_zip_file_reads_single_xml_member(tmp_path: Path) -> None:
    zip_path = tmp_path / "corp_code.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("CORPCODE.xml", SAMPLE_XML.read_text(encoding="utf-8"))

    entries = parse_corp_code_zip_file(zip_path)
    match = resolve_corp_code_by_stock_code(entries, "005930")
    assert match.corp_code == "00126380"


def test_parse_corp_code_zip_rejects_multiple_xml_members(tmp_path: Path) -> None:
    zip_path = tmp_path / "corp_code_multi.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("a.xml", "<result></result>")
        archive.writestr("b.xml", "<result></result>")

    with pytest.raises(DartCorpCodeResolverError, match="exactly one XML"):
        parse_corp_code_zip_file(zip_path)


def test_resolver_module_has_no_forbidden_tokens() -> None:
    paths = (
        REPO_ROOT / "src" / "data" / "dart_corp_code_resolver.py",
        REPO_ROOT / "ops" / "resolve_dart_corp_code.py",
    )
    forbidden_network = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
        "yfinance",
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


def test_fixtures_do_not_contain_api_key_strings() -> None:
    for path in FIXTURES.glob("corp_code*.xml"):
        text = path.read_text(encoding="utf-8")
        assert "DART_API_KEY" not in text
        assert "crtfc_key" not in text.lower()


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    import os

    full_env = os.environ.copy()
    full_env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(OPS_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=full_env,
    )


def test_cli_success_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from resolve_dart_corp_code import main

    exit_code = main(
        [
            "--corp-code-xml",
            str(SAMPLE_XML),
            "--stock-code",
            "005930",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "ok"
    assert payload["corp_code"] == "00126380"
    assert payload["corp_name"] == "삼성전자"


def test_cli_no_match_json_error() -> None:
    result = _run_cli(
        "--corp-code-xml",
        str(SAMPLE_XML),
        "--stock-code",
        "999999",
        "--json",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["stage"] == "resolve"


def test_cli_invalid_file_json_error() -> None:
    result = _run_cli(
        "--corp-code-xml",
        str(INVALID_XML),
        "--stock-code",
        "005930",
        "--json",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["stage"] == "parse"
