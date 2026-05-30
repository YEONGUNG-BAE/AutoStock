from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
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
    resolver_path = REPO_ROOT / "src" / "data" / "dart_corp_code_resolver.py"
    ops_path = REPO_ROOT / "ops" / "resolve_dart_corp_code.py"
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
    resolver_source = resolver_path.read_text(encoding="utf-8")
    resolver_lower = resolver_source.lower()
    for token in forbidden_network:
        assert token not in resolver_lower, f"{resolver_path.name} must not reference {token!r}"
    for token in ("DART_API_KEY", "FRED_API_KEY"):
        assert token not in resolver_source, f"{resolver_path.name} must not reference {token!r}"

    ops_source = ops_path.read_text(encoding="utf-8")
    ops_lower = ops_source.lower()
    for token in forbidden_network:
        assert token not in ops_lower, f"{ops_path.name} must not reference {token!r}"
    module_level = ops_source.split("def ", 1)[0]
    assert "dart_corp_code_http_client" not in module_level


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


def _sample_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CORPCODE.xml", SAMPLE_XML.read_text(encoding="utf-8"))
    return buffer.getvalue()


SECRET = "SECRET_DART_KEY_TEST"
FETCHED_AT = datetime(2026, 5, 30, 0, 0, 0, tzinfo=UTC)


def test_cli_live_fetch_success_with_injected_fetcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from resolve_dart_corp_code import run_resolve_dart_corp_code

    monkeypatch.setenv("DART_API_KEY", SECRET)
    snapshot_dir = tmp_path / "snapshots"

    payload = run_resolve_dart_corp_code(
        live_fetch=True,
        snapshot_dir=snapshot_dir,
        stock_code="005930",
        corp_name=None,
        api_key_env="DART_API_KEY",
        fetch_zip_bytes=lambda _key: _sample_zip_bytes(),
        fetched_at=FETCHED_AT,
    )
    assert payload["status"] == "ok"
    assert payload["mode"] == "live-fetch"
    assert payload["corp_code"] == "00126380"
    assert payload["corp_name"] == "삼성전자"
    snapshot_path = Path(payload["snapshot_path"])
    assert snapshot_path.is_file()
    assert SECRET not in payload["snapshot_path"]
    assert SECRET not in snapshot_path.read_bytes().decode("latin-1")


def test_cli_live_fetch_json_stdout_no_secret_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from resolve_dart_corp_code import main, run_resolve_dart_corp_code as original_run

    monkeypatch.setenv("DART_API_KEY", SECRET)

    def patched_run(**kwargs: object) -> dict[str, object]:
        return original_run(
            fetch_zip_bytes=lambda _key: _sample_zip_bytes(),
            fetched_at=FETCHED_AT,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr("resolve_dart_corp_code.run_resolve_dart_corp_code", patched_run)

    exit_code = main(
        [
            "--live-fetch",
            "--snapshot-dir",
            str(tmp_path / "snapshots"),
            "--stock-code",
            "005930",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "ok"
    assert payload["corp_code"] == "00126380"


def test_cli_live_fetch_missing_env_var_stage_args_no_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from resolve_dart_corp_code import main

    monkeypatch.delenv("DART_API_KEY", raising=False)
    exit_code = main(
        [
            "--live-fetch",
            "--snapshot-dir",
            str(tmp_path / "snapshots"),
            "--stock-code",
            "005930",
            "--json",
        ]
    )
    assert exit_code == 1
    assert list((tmp_path / "snapshots").glob("*.zip")) == []


def test_cli_live_fetch_blank_env_var_stage_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from resolve_dart_corp_code import main

    monkeypatch.setenv("DART_API_KEY", "   ")
    exit_code = main(
        [
            "--live-fetch",
            "--snapshot-dir",
            str(tmp_path / "snapshots"),
            "--stock-code",
            "005930",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out.strip())
    assert payload["stage"] == "args"


def test_cli_live_fetch_blank_api_key_env_stage_args(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from resolve_dart_corp_code import main

    exit_code = main(
        [
            "--live-fetch",
            "--api-key-env",
            "   ",
            "--snapshot-dir",
            str(tmp_path / "snapshots"),
            "--stock-code",
            "005930",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out.strip())
    assert payload["stage"] == "args"


def test_cli_local_modes_do_not_require_env_var(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from resolve_dart_corp_code import main

    monkeypatch.delenv("DART_API_KEY", raising=False)
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


@pytest.mark.parametrize(
    ("extra_args",),
    [
        (("--corp-code-xml", str(SAMPLE_XML), "--live-fetch"),),
        (("--corp-code-zip", "tests/fixtures/research/dart/corp_code_sample.zip", "--live-fetch"),),
        (("--corp-code-xml", str(SAMPLE_XML), "--corp-code-zip", str(SAMPLE_XML)),),
        (("--stock-code", "005930"),),
    ],
)
def test_cli_exactly_one_source_mode_required(
    extra_args: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from resolve_dart_corp_code import main

    exit_code = main([*extra_args, "--stock-code", "005930", "--json"])
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out.strip())
    assert payload["stage"] == "args"


def test_cli_live_fetch_non_zip_response_stage_fetch_no_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from resolve_dart_corp_code import ResolveCorpCodeError, run_resolve_dart_corp_code

    monkeypatch.setenv("DART_API_KEY", SECRET)
    snapshot_dir = tmp_path / "snapshots"

    with pytest.raises(ResolveCorpCodeError) as exc_info:
        run_resolve_dart_corp_code(
            live_fetch=True,
            snapshot_dir=snapshot_dir,
            stock_code="005930",
            corp_name=None,
            api_key_env="DART_API_KEY",
            fetch_zip_bytes=lambda _key: b"<result><status>010</status></result>",
        )
    assert exc_info.value.stage == "fetch"
    assert list(snapshot_dir.glob("*.zip")) == []


def test_cli_live_fetch_injected_http_error_stage_fetch_no_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from data.dart_corp_code_http_client import DartCorpCodeHttpError
    from resolve_dart_corp_code import ResolveCorpCodeError, main, run_resolve_dart_corp_code

    monkeypatch.setenv("DART_API_KEY", SECRET)
    snapshot_dir = tmp_path / "snapshots"

    def raising_fetch(_key: str) -> bytes:
        raise DartCorpCodeHttpError(
            "OpenDART corp-code HTTP request failed: crtfc_key=[REDACTED]"
        )

    with pytest.raises(ResolveCorpCodeError) as exc_info:
        run_resolve_dart_corp_code(
            live_fetch=True,
            snapshot_dir=snapshot_dir,
            stock_code="005930",
            corp_name=None,
            api_key_env="DART_API_KEY",
            fetch_zip_bytes=raising_fetch,
        )
    assert exc_info.value.stage == "fetch"
    assert SECRET not in exc_info.value.message
    assert list(snapshot_dir.glob("*.zip")) == []

    def patched_run(**kwargs: object) -> dict[str, object]:
        return run_resolve_dart_corp_code(
            fetch_zip_bytes=raising_fetch,
            fetched_at=FETCHED_AT,
            **kwargs,  # type: ignore[arg-type]
        )

    monkeypatch.setattr("resolve_dart_corp_code.run_resolve_dart_corp_code", patched_run)
    exit_code = main(
        [
            "--live-fetch",
            "--snapshot-dir",
            str(tmp_path / "snapshots_cli"),
            "--stock-code",
            "005930",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out.strip())
    assert payload["stage"] == "fetch"
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert list((tmp_path / "snapshots_cli").glob("*.zip")) == []
