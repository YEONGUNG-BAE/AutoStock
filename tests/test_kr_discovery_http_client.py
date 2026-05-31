"""Real Intake 3G3-4B — KR discovery HTTP client tests (fake urlopen only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_discovery"
    / "raw_kr_discovery_synthetic_success.json"
)

SECRET = "SECRET_VALUE_TEST"
ENDPOINT_URL = "https://example.test/discovery.json"
ENDPOINT_URL_WITH_SECRET = f"https://example.test/discovery.json?session={SECRET}"

sys.path.insert(0, str(REPO_ROOT / "src"))

from data.kr_discovery_http_client import (
    KrDiscoveryHttpError,
    fetch_kr_discovery_http_payload,
    redact_discovery_http_text,
    sanitize_http_failure,
)


def _transport_payload() -> dict[str, object]:
    payload = json.loads(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))
    return {"records": payload["records"]}


def _fake_urlopen(body: bytes | None = None, *, raises: Exception | None = None) -> object:
    resolved_body = json.dumps(_transport_payload()).encode("utf-8") if body is None else body

    def urlopen(_request: object, timeout: float) -> object:
        if raises is not None:
            raise raises

        class FakeResponse:
            def read(self) -> bytes:
                return resolved_body

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        return FakeResponse()

    return urlopen


def test_fake_urlopen_success_returns_json_object() -> None:
    payload = fetch_kr_discovery_http_payload(
        endpoint_url=ENDPOINT_URL,
        urlopen_fn=_fake_urlopen(),
    )
    assert isinstance(payload, dict)
    assert isinstance(payload["records"], list)
    assert len(payload["records"]) == 5


def test_non_object_json_fails_at_parse_stage() -> None:
    with pytest.raises(KrDiscoveryHttpError) as exc_info:
        fetch_kr_discovery_http_payload(
            endpoint_url=ENDPOINT_URL,
            urlopen_fn=_fake_urlopen(body=json.dumps([1, 2, 3]).encode("utf-8")),
        )
    assert exc_info.value.stage == "parse"


def test_invalid_json_fails_at_parse_stage() -> None:
    with pytest.raises(KrDiscoveryHttpError) as exc_info:
        fetch_kr_discovery_http_payload(
            endpoint_url=ENDPOINT_URL,
            urlopen_fn=_fake_urlopen(body=b"not-json"),
        )
    assert exc_info.value.stage == "parse"


def test_http_exception_fails_at_fetch_stage() -> None:
    with pytest.raises(KrDiscoveryHttpError) as exc_info:
        fetch_kr_discovery_http_payload(
            endpoint_url=ENDPOINT_URL,
            urlopen_fn=_fake_urlopen(
                raises=HTTPError(
                    url=f"{ENDPOINT_URL}?api_key={SECRET}",
                    code=403,
                    msg="Forbidden",
                    hdrs={},
                    fp=None,
                )
            ),
        )
    assert exc_info.value.stage == "fetch"


def test_urlopen_exception_fails_at_fetch_stage() -> None:
    with pytest.raises(KrDiscoveryHttpError) as exc_info:
        fetch_kr_discovery_http_payload(
            endpoint_url=ENDPOINT_URL,
            urlopen_fn=_fake_urlopen(raises=URLError("network down")),
        )
    assert exc_info.value.stage == "fetch"


def test_api_key_query_is_sanitized() -> None:
    message = sanitize_http_failure(
        RuntimeError(f"failed api_key={SECRET}"),
        endpoint_url=ENDPOINT_URL,
        extra_secret_values=(SECRET,),
    )
    assert SECRET not in message
    assert "api_key=<redacted>" in message


def test_crtfc_key_query_is_sanitized() -> None:
    message = sanitize_http_failure(
        RuntimeError(f"failed crtfc_key={SECRET}"),
        endpoint_url=ENDPOINT_URL,
        extra_secret_values=(SECRET,),
    )
    assert SECRET not in message
    assert "crtfc_key=<redacted>" in message


def test_bearer_token_is_sanitized() -> None:
    redacted = redact_discovery_http_text(f"Authorization: Bearer {SECRET}", extra_secret_values=(SECRET,))
    assert SECRET not in redacted
    assert "Bearer <redacted>" in redacted


def test_endpoint_url_secret_query_param_is_sanitized() -> None:
    redacted = redact_discovery_http_text(ENDPOINT_URL_WITH_SECRET, extra_secret_values=(SECRET,))
    assert SECRET not in redacted
    assert "?<redacted>" in redacted


def test_non_enumerated_session_query_param_is_sanitized() -> None:
    redacted = redact_discovery_http_text(
        "request failed for https://example.test/path?session=SECRET&x=1",
        extra_secret_values=("SECRET",),
    )
    assert "SECRET" not in redacted
    assert "https://example.test/path?<redacted>" in redacted


def test_sanitized_error_does_not_expose_secret() -> None:
    message = sanitize_http_failure(
        RuntimeError(f"boom api_key={SECRET}"),
        endpoint_url=ENDPOINT_URL_WITH_SECRET,
        extra_secret_values=(SECRET,),
    )
    assert SECRET not in message


def test_wrapped_http_errors_use_from_none() -> None:
    with pytest.raises(KrDiscoveryHttpError) as exc_info:
        fetch_kr_discovery_http_payload(
            endpoint_url=ENDPOINT_URL,
            urlopen_fn=_fake_urlopen(raises=URLError(f"open failed api_key={SECRET}")),
            extra_secret_values=(SECRET,),
        )
    assert exc_info.value.__cause__ is None
    assert SECRET not in exc_info.value.safe_message


def test_source_module_has_no_env_reads() -> None:
    source = (REPO_ROOT / "src" / "data" / "kr_discovery_http_client.py").read_text(encoding="utf-8").lower()
    assert "os.environ" not in source
    assert "getenv" not in source


def test_urllib_confined_to_discovery_http_client_module() -> None:
    http_client = REPO_ROOT / "src" / "data" / "kr_discovery_http_client.py"
    live_client = REPO_ROOT / "src" / "data" / "kr_discovery_live_client.py"
    ops_script = REPO_ROOT / "ops" / "run_kr_discovery_live_smoke.py"

    assert "urllib.request" in http_client.read_text(encoding="utf-8")
    assert "urllib.request" not in live_client.read_text(encoding="utf-8")
    assert "urllib.request" not in ops_script.read_text(encoding="utf-8")
