from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from data.dart_http_client import (
    DartHttpError,
    fetch_opendart_list_response,
    sanitize_http_failure,
)
from data.dart_live_client import redact_secrets

SECRET = "SECRET_DART_KEY_TEST"


def _success_body() -> dict[str, object]:
    return {
        "status": "000",
        "message": "정상",
        "page_no": 1,
        "page_count": 100,
        "total_count": 1,
        "total_page": 1,
        "list": [
            {
                "corp_code": "00126380",
                "report_nm": "Synthetic DART report 1",
                "rcept_no": "202605300001",
                "rcept_dt": "20260530",
            }
        ],
    }


def test_fetch_opendart_list_response_success_with_fake_urlopen() -> None:
    body_payload = _success_body()

    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps(body_payload).encode("utf-8")

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        assert hasattr(request, "full_url")
        url_text = request.full_url  # type: ignore[attr-defined]
        assert "list.json" in url_text
        assert "corp_code=00126380" in url_text
        return FakeResponse()

    payload = fetch_opendart_list_response(
        {
            "crtfc_key": SECRET,
            "corp_code": "00126380",
            "bgn_de": "20260530",
            "page_count": "100",
        },
        urlopen_fn=fake_urlopen,
    )
    assert payload["status"] == "000"
    assert SECRET not in json.dumps(payload)


def test_fetch_opendart_http_error_does_not_leak_secret() -> None:
    def raising_urlopen(request: object, timeout: float) -> object:
        raise HTTPError(
            url=f"https://opendart.fss.or.kr/api/list.json?crtfc_key={SECRET}&corp_code=00126380",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(b""),
        )

    with pytest.raises(DartHttpError) as exc_info:
        fetch_opendart_list_response(
            {"crtfc_key": SECRET, "corp_code": "00126380", "bgn_de": "20260530"},
            urlopen_fn=raising_urlopen,
        )
    assert SECRET not in exc_info.value.message


def test_fetch_opendart_url_error_does_not_leak_secret() -> None:
    def raising_urlopen(request: object, timeout: float) -> object:
        raise URLError(f"network failed crtfc_key={SECRET}")

    with pytest.raises(DartHttpError) as exc_info:
        fetch_opendart_list_response(
            {"crtfc_key": SECRET, "corp_code": "00126380", "bgn_de": "20260530"},
            urlopen_fn=raising_urlopen,
        )
    assert SECRET not in exc_info.value.message


def test_fetch_opendart_invalid_json_fails() -> None:
    class FakeResponse:
        def read(self) -> bytes:
            return b"not-json"

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with pytest.raises(DartHttpError, match="not valid JSON"):
        fetch_opendart_list_response(
            {"crtfc_key": SECRET, "corp_code": "00126380", "bgn_de": "20260530"},
            urlopen_fn=lambda _request, timeout: FakeResponse(),
        )


def test_fetch_opendart_non_object_json_fails() -> None:
    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps([]).encode("utf-8")

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with pytest.raises(DartHttpError, match="JSON object"):
        fetch_opendart_list_response(
            {"crtfc_key": SECRET, "corp_code": "00126380", "bgn_de": "20260530"},
            urlopen_fn=lambda _request, timeout: FakeResponse(),
        )


def test_sanitize_http_failure_redacts_secret() -> None:
    err = sanitize_http_failure(http_status=403, reason=f"failed crtfc_key={SECRET}", secrets=(SECRET,))
    assert SECRET not in err.message
    assert "crtfc_key=[REDACTED]" in err.message


def test_dart_http_client_forbids_other_network_or_trading_tokens() -> None:
    source = (REPO_ROOT / "src" / "data" / "dart_http_client.py").read_text(encoding="utf-8").lower()
    forbidden = (
        "requests",
        "httpx",
        "aiohttp",
        "yfinance",
        "kis",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
    )
    for token in forbidden:
        assert token not in source, f"dart_http_client must not reference {token!r}"


def test_dart_http_client_uses_urllib_only() -> None:
    source = (REPO_ROOT / "src" / "data" / "dart_http_client.py").read_text(encoding="utf-8")
    assert "urllib.request" in source
    assert "urllib.parse" in source
