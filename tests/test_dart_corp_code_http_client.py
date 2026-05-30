from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "research" / "dart"
SAMPLE_XML = FIXTURES / "corp_code_sample.xml"

sys.path.insert(0, str(REPO_ROOT / "src"))

from data.dart_corp_code_http_client import (
    DartCorpCodeHttpError,
    OPENDART_CORP_CODE_URL,
    fetch_corp_code_zip_bytes,
    redact_secrets,
    sanitize_http_failure,
)
from data.dart_corp_code_live_client import (
    DartCorpCodeSnapshotError,
    snapshot_filename_for_zip,
    write_corp_code_zip_snapshot,
)
from data.dart_corp_code_resolver import parse_corp_code_zip_file, resolve_corp_code_by_stock_code

SECRET = "SECRET_DART_KEY_TEST"
FETCHED_AT = __import__("datetime").datetime(2026, 5, 30, 0, 0, 0, tzinfo=__import__("datetime").UTC)


def _sample_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CORPCODE.xml", SAMPLE_XML.read_text(encoding="utf-8"))
    return buffer.getvalue()


def test_fetch_corp_code_zip_bytes_success_returns_zip() -> None:
    zip_bytes = _sample_zip_bytes()

    class FakeResponse:
        def read(self) -> bytes:
            return zip_bytes

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    captured: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    result = fetch_corp_code_zip_bytes(
        api_key=SECRET,
        urlopen_fn=fake_urlopen,
    )
    assert result == zip_bytes
    request = captured["request"]
    assert request is not None
    assert SECRET in getattr(request, "full_url", "")
    assert OPENDART_CORP_CODE_URL.split("?")[0] in getattr(request, "full_url", "")


def test_fetch_corp_code_zip_bytes_non_zip_fails_before_return() -> None:
    class FakeResponse:
        def read(self) -> bytes:
            return b"<result><status>010</status></result>"

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with pytest.raises(DartCorpCodeHttpError, match="did not return a ZIP"):
        fetch_corp_code_zip_bytes(api_key=SECRET, urlopen_fn=lambda *_a, **_k: FakeResponse())


def test_fetch_corp_code_zip_bytes_http_error_sanitizes_secret() -> None:
    def raising_urlopen(request: object, timeout: float) -> object:
        raise HTTPError(
            url=f"{OPENDART_CORP_CODE_URL}?crtfc_key={SECRET}",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(b""),
        )

    with pytest.raises(DartCorpCodeHttpError) as exc_info:
        fetch_corp_code_zip_bytes(api_key=SECRET, urlopen_fn=raising_urlopen)
    assert SECRET not in exc_info.value.message


def test_fetch_corp_code_zip_bytes_urlerror_sanitizes_secret_like_text() -> None:
    def raising_urlopen(request: object, timeout: float) -> object:
        raise URLError(f"network failed crtfc_key={SECRET}")

    with pytest.raises(DartCorpCodeHttpError) as exc_info:
        fetch_corp_code_zip_bytes(api_key=SECRET, urlopen_fn=raising_urlopen)
    assert SECRET not in exc_info.value.message


def test_redact_secrets_removes_known_key_and_query() -> None:
    text = redact_secrets(f"crtfc_key={SECRET}&other=1", secret=SECRET)
    assert SECRET not in text
    assert "crtfc_key=[REDACTED]" in text


def test_sanitize_http_failure_does_not_echo_secret() -> None:
    err = sanitize_http_failure(reason=f"failed crtfc_key={SECRET}", secret=SECRET)
    assert SECRET not in err.message


def test_fetch_does_not_parse_zip_contents() -> None:
    zip_bytes = _sample_zip_bytes()

    class FakeResponse:
        def read(self) -> bytes:
            return zip_bytes

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    result = fetch_corp_code_zip_bytes(api_key=SECRET, urlopen_fn=lambda *_a, **_k: FakeResponse())
    assert isinstance(result, bytes)
    assert result.startswith(b"PK\x03\x04")


def test_write_corp_code_zip_snapshot_filename_and_parse(tmp_path: Path) -> None:
    zip_bytes = _sample_zip_bytes()
    snapshot_path = write_corp_code_zip_snapshot(
        zip_bytes=zip_bytes,
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
    )
    expected_name = snapshot_filename_for_zip(zip_bytes=zip_bytes, fetched_at=FETCHED_AT)
    assert snapshot_path.name == expected_name
    assert snapshot_path.name.startswith("raw_corp_code_20260530T000000Z_")
    assert snapshot_path.name.endswith(".zip")

    entries = parse_corp_code_zip_file(snapshot_path)
    match = resolve_corp_code_by_stock_code(entries, "005930")
    assert match.corp_code == "00126380"


def test_write_corp_code_zip_snapshot_collision_fails(tmp_path: Path) -> None:
    zip_bytes = _sample_zip_bytes()
    write_corp_code_zip_snapshot(
        zip_bytes=zip_bytes,
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
    )
    with pytest.raises(DartCorpCodeSnapshotError, match="already exists"):
        write_corp_code_zip_snapshot(
            zip_bytes=zip_bytes,
            snapshot_dir=tmp_path,
            fetched_at=FETCHED_AT,
        )


def test_snapshot_filename_and_bytes_do_not_contain_api_key(tmp_path: Path) -> None:
    zip_bytes = _sample_zip_bytes()
    snapshot_path = write_corp_code_zip_snapshot(
        zip_bytes=zip_bytes,
        snapshot_dir=tmp_path,
        fetched_at=FETCHED_AT,
    )
    assert SECRET not in snapshot_path.name
    assert SECRET not in snapshot_path.read_bytes().decode("latin-1")


def test_http_client_forbids_other_network_or_trading_tokens() -> None:
    source = (
        REPO_ROOT / "src" / "data" / "dart_corp_code_http_client.py"
    ).read_text(encoding="utf-8").lower()
    forbidden = (
        "requests",
        "httpx",
        "aiohttp",
        "yfinance",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
    )
    for token in forbidden:
        assert token not in source, f"dart_corp_code_http_client.py must not reference {token!r}"


def test_live_client_forbids_network_tokens() -> None:
    source = (
        REPO_ROOT / "src" / "data" / "dart_corp_code_live_client.py"
    ).read_text(encoding="utf-8").lower()
    forbidden = (
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
    for token in forbidden:
        assert token not in source, f"dart_corp_code_live_client.py must not reference {token!r}"
