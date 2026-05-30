from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from data.fred_http_client import (
    FredHttpError,
    assert_snapshot_payload_safe,
    build_live_snapshot_payload,
    build_sanitized_request_metadata,
    fetch_series_observations_body,
    observation_mapping_from_api_body,
    redact_secrets,
    sanitize_http_failure,
    write_live_snapshot_file,
)

SECRET = "SECRET_FRED_KEY_TEST"
FETCHED_AT = datetime(2026, 5, 29, 1, 2, 3, tzinfo=UTC)


def test_build_sanitized_request_metadata_excludes_api_key_value() -> None:
    metadata = build_sanitized_request_metadata(
        series_id="DGS10",
        api_key_env="FRED_API_KEY",
        api_key_present=True,
    )
    assert metadata["series_id"] == "DGS10"
    assert metadata["api_key_present"] is True
    assert "api_key" not in metadata
    assert "?" not in metadata["base_url"]


def test_sanitize_http_failure_does_not_echo_url_with_secret() -> None:
    err = sanitize_http_failure(http_status=403, reason=f"failed url api_key={SECRET}")
    assert SECRET not in err.message
    assert "api_key=[REDACTED]" in err.message


def test_fetch_series_observations_body_http_error_does_not_leak_secret() -> None:
    def raising_urlopen(request: object, timeout: float) -> object:
        raise HTTPError(
            url=f"https://api.stlouisfed.org/fred/series/observations?api_key={SECRET}&series_id=DGS10",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(b""),
        )

    with pytest.raises(FredHttpError) as exc_info:
        fetch_series_observations_body(
            "DGS10",
            api_key=SECRET,
            urlopen_fn=raising_urlopen,
        )
    assert SECRET not in exc_info.value.message


def test_fetch_series_observations_body_success_parses_observation() -> None:
    body_payload = {
        "observations": [
            {"date": "2026-05-28", "value": "4.25", "realtime_start": "2026-05-28"},
        ]
    }

    class FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        return FakeResponse(json.dumps(body_payload).encode("utf-8"))

    body = fetch_series_observations_body(
        "DGS10",
        api_key=SECRET,
        urlopen_fn=fake_urlopen,
    )
    observation = observation_mapping_from_api_body(body)
    assert observation["value"] == "4.25"
    assert observation["source_timestamp"] == datetime(2026, 5, 28, 0, 0, tzinfo=UTC)


def test_live_snapshot_payload_and_file_exclude_api_key(tmp_path: Path) -> None:
    metadata = build_sanitized_request_metadata(
        series_id="DGS10",
        api_key_env="FRED_API_KEY",
        api_key_present=True,
    )
    payload = build_live_snapshot_payload(
        series_id="DGS10",
        fetched_at=FETCHED_AT,
        request_metadata=metadata,
        observation={
            "value": "4.25",
            "source_timestamp": datetime(2026, 5, 28, 0, 0, tzinfo=UTC),
        },
    )
    assert_snapshot_payload_safe(payload, api_key=SECRET)
    assert "api_key" not in payload["request"]
    assert "?" not in json.dumps(payload)

    snapshot_path = tmp_path / "raw_test.json"
    write_live_snapshot_file(snapshot_path, payload, api_key=SECRET)
    written = snapshot_path.read_text(encoding="utf-8")
    assert SECRET not in written
    assert "api_key=" not in written.lower()


def test_fred_and_dart_http_clients_may_use_urllib_request() -> None:
    for relative in (
        "src/data/fred_http_client.py",
        "src/data/dart_http_client.py",
    ):
        http_client = (REPO_ROOT / relative).read_text(encoding="utf-8").lower()
        assert "urllib.request" in http_client

    for relative in (
        "ops/fetch_research_sources.py",
        "src/data/research_source_fetcher.py",
        "src/data/fred_source_fetcher.py",
        "src/data/dart_live_client.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8").lower()
        assert "urllib.request" not in source, relative
        assert "urllib.parse" not in source, relative
        assert "urllib.error" not in source, relative


def test_fred_http_client_forbids_other_network_or_trading_tokens() -> None:
    source = (REPO_ROOT / "src" / "data" / "fred_http_client.py").read_text(encoding="utf-8").lower()
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
        assert token not in source, f"fred_http_client.py must not reference {token!r}"


def test_redact_secrets_removes_known_key() -> None:
    text = redact_secrets(f"api_key={SECRET}&series_id=DGS10", secret=SECRET)
    assert SECRET not in text
    assert "api_key=[REDACTED]" in text
