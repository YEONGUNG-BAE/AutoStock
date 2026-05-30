from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from decision.canonical_json import canonical_json_dumps
from domain._strings import normalize_required_string

# urllib stdlib HTTP는 이 모듈에만 격리한다 (1B live-smoke).
FRED_API_BASE_URL = "https://api.stlouisfed.org/fred"
FRED_SERIES_OBSERVATIONS_ENDPOINT = "series/observations"
DEFAULT_API_KEY_ENV = "FRED_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 15.0

_API_KEY_QUERY_PATTERN = re.compile(r"api_key=[^&\s\"']+", re.IGNORECASE)

UrlopenFn = Callable[..., Any]


class FredHttpError(Exception):
    """FRED HTTP 실패. message에는 api_key·query URL이 포함되면 안 된다."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message


def build_sanitized_request_metadata(
    *,
    series_id: str,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    api_key_present: bool,
) -> dict[str, Any]:
    """snapshot request metadata. api_key 값·full query URL 저장 금지."""
    return {
        "endpoint": FRED_SERIES_OBSERVATIONS_ENDPOINT,
        "base_url": FRED_API_BASE_URL,
        "series_id": normalize_required_string(series_id, field_name="series_id"),
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
        "api_key_env": normalize_required_string(api_key_env, field_name="api_key_env"),
        "api_key_present": api_key_present,
    }


def redact_secrets(text: str, *, secret: str | None = None) -> str:
    """로그/에러용 문자열에서 api_key query 및 알려진 secret 값을 제거한다."""
    redacted = text
    if secret:
        redacted = redacted.replace(secret, "[REDACTED]")
    return _API_KEY_QUERY_PATTERN.sub("api_key=[REDACTED]", redacted)


def sanitize_http_failure(*, http_status: int | None = None, reason: str) -> FredHttpError:
    """urllib 예외 str(exc) 대신 sanitized FredHttpError를 만든다."""
    safe_reason = redact_secrets(reason)
    if http_status is not None:
        message = f"FRED HTTP request failed (status={http_status}): {safe_reason}"
    else:
        message = f"FRED HTTP request failed: {safe_reason}"
    return FredHttpError(message, http_status=http_status)


def fetch_series_observations_body(
    series_id: str,
    *,
    api_key: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    urlopen_fn: UrlopenFn | None = None,
) -> dict[str, Any]:
    """FRED series/observations JSON body를 반환한다. full URL·api_key는 외부로 노출하지 않는다."""
    normalized_series_id = normalize_required_string(series_id, field_name="series_id")
    normalized_api_key = normalize_required_string(api_key, field_name="api_key")
    opener = urlopen_fn or urlopen

    query = urlencode(
        {
            "series_id": normalized_series_id,
            "api_key": normalized_api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": "1",
        }
    )
    request_url = f"{FRED_API_BASE_URL}/{FRED_SERIES_OBSERVATIONS_ENDPOINT}?{query}"
    request = Request(request_url, method="GET")

    try:
        with opener(request, timeout=timeout_seconds) as response:
            raw_bytes = response.read()
    except HTTPError as exc:
        raise sanitize_http_failure(http_status=exc.code, reason="HTTP error response") from None
    except URLError:
        raise sanitize_http_failure(reason="network error") from None

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        raise FredHttpError("FRED HTTP response is not valid JSON") from None

    if not isinstance(payload, dict):
        raise FredHttpError("FRED HTTP response root must be a JSON object")
    return payload


def observation_mapping_from_api_body(body: dict[str, Any]) -> dict[str, object]:
    """FRED observations API body → FredMacroAdapter observation mapping."""
    observations = body.get("observations")
    if not isinstance(observations, list) or not observations:
        raise FredHttpError("FRED observations response is empty")

    first = observations[0]
    if not isinstance(first, dict):
        raise FredHttpError("FRED observation item must be a JSON object")

    value = first.get("value")
    if value is None or str(value).strip() in {"", "."}:
        raise FredHttpError("FRED observation value is missing")

    observation_date = first.get("date")
    if not isinstance(observation_date, str) or not observation_date.strip():
        raise FredHttpError("FRED observation date is missing")

    parsed_date = date.fromisoformat(observation_date)
    source_timestamp = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)

    payload: dict[str, object] = {}
    for key, item in first.items():
        if key in {"date", "value"}:
            continue
        payload[key] = item

    return {
        "value": str(value),
        "source_timestamp": source_timestamp,
        **payload,
    }


def build_live_snapshot_payload(
    *,
    series_id: str,
    fetched_at: datetime,
    request_metadata: dict[str, Any],
    observation: dict[str, object],
) -> dict[str, Any]:
    """live-smoke immutable snapshot dict. api_key·full URL 미포함."""
    observation_json: dict[str, Any] = {}
    for key, value in observation.items():
        if isinstance(value, datetime):
            observation_json[key] = value.isoformat()
        else:
            observation_json[key] = value

    return {
        "source_key": "fred",
        "external_service": "FRED API",
        "series_id": normalize_required_string(series_id, field_name="series_id"),
        "fetched_at": fetched_at.isoformat(),
        "request": request_metadata,
        "observation": observation_json,
    }


def assert_snapshot_payload_safe(payload: dict[str, Any], *, api_key: str | None = None) -> None:
    """직렬화 결과에 api_key·query URL이 없는지 검증한다."""
    serialized = canonical_json_dumps(payload)
    if api_key and api_key in serialized:
        raise ValueError("snapshot must not contain api_key value")
    if "api_key=" in serialized.lower():
        raise ValueError("snapshot must not contain api_key query parameter")


def snapshot_filename_for_payload(payload: dict[str, Any], *, fetched_at: datetime) -> str:
    """immutable snapshot 파일명 (design G2 패턴)."""
    body = canonical_json_dumps(payload)
    sha8 = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    compact = fetched_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"raw_{compact}_{sha8}.json"


def write_live_snapshot_file(
    snapshot_path: Path,
    payload: dict[str, Any],
    *,
    api_key: str | None = None,
) -> None:
    """live snapshot JSON을 immutable path에 기록한다."""
    assert_snapshot_payload_safe(payload, api_key=api_key)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(canonical_json_dumps(payload) + "\n", encoding="utf-8")
