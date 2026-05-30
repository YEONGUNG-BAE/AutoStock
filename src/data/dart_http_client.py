from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from data.dart_live_client import redact_secrets

# urllib stdlib HTTP는 fred_http_client와 이 모듈에만 격리한다 (3B2 DART live-smoke).
OPENDART_API_BASE_URL = "https://opendart.fss.or.kr/api"
OPENDART_LIST_ENDPOINT = "list.json"
DEFAULT_TIMEOUT_SECONDS = 15.0

_CRTFC_KEY_QUERY_PATTERN = re.compile(r"crtfc_key=[^&\s\"']+", re.IGNORECASE)

UrlopenFn = Callable[..., Any]


class DartHttpError(RuntimeError):
    """OpenDART HTTP 실패. message에는 crtfc_key·query URL이 포함되면 안 된다."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.message = message


def sanitize_http_failure(
    *,
    http_status: int | None = None,
    reason: str,
    secrets: tuple[str, ...] = (),
) -> DartHttpError:
    """urllib 예외 str(exc) 대신 sanitized DartHttpError를 만든다."""
    safe_reason = redact_secrets(reason, secrets=secrets)
    safe_reason = _CRTFC_KEY_QUERY_PATTERN.sub("crtfc_key=[REDACTED]", safe_reason)
    if http_status is not None:
        message = f"OpenDART HTTP request failed (status={http_status}): {safe_reason}"
    else:
        message = f"OpenDART HTTP request failed: {safe_reason}"
    return DartHttpError(message, http_status=http_status)


def fetch_opendart_list_response(
    params: Mapping[str, str],
    *,
    urlopen_fn: UrlopenFn | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Mapping[str, Any]:
    """OpenDART list.json JSON body를 반환한다. full URL·crtfc_key는 외부로 노출하지 않는다."""
    if not isinstance(params, Mapping):
        raise DartHttpError("OpenDART request params must be a mapping")

    api_key = params.get("crtfc_key")
    if api_key is None or not str(api_key).strip():
        raise DartHttpError("OpenDART request params must include crtfc_key")
    secret = str(api_key).strip()

    normalized_params = {str(key): str(value) for key, value in params.items() if value is not None}

    opener = urlopen_fn or urlopen
    query = urlencode(normalized_params)
    request_url = f"{OPENDART_API_BASE_URL}/{OPENDART_LIST_ENDPOINT}?{query}"
    request = Request(request_url, method="GET")

    try:
        with opener(request, timeout=timeout_seconds) as response:
            raw_bytes = response.read()
    except HTTPError as exc:
        raise sanitize_http_failure(
            http_status=exc.code,
            reason="HTTP error response",
            secrets=(secret,),
        ) from None
    except URLError:
        raise sanitize_http_failure(reason="network error", secrets=(secret,)) from None

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        raise DartHttpError("OpenDART HTTP response is not valid JSON") from None

    if not isinstance(payload, dict):
        raise DartHttpError("OpenDART HTTP response root must be a JSON object")
    return payload
