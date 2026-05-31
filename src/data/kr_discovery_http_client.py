from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from domain._strings import normalize_required_string

# 3G3-4B: KR discovery HTTP live-smoke client. urllib stdlib HTTP는 이 모듈에만 격리한다.
StageName = Literal["fetch", "parse"]

UrlopenFn = Callable[..., Any]

_URL_IN_TEXT_PATTERN = re.compile(r"https?://[^\s\"']+")
_BEARER_PATTERN = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


class KrDiscoveryHttpError(ValueError):
    """KR discovery HTTP fetch 실패. safe_message만 외부 노출."""

    def __init__(self, stage: StageName, safe_message: str) -> None:
        super().__init__(safe_message)
        self.stage = stage
        self.safe_message = safe_message


def redact_discovery_http_text(
    value: str,
    *,
    extra_secret_values: Sequence[str] = (),
) -> str:
    """HTTP/URL/exception 텍스트에서 query string·credential-like 토큰을 제거한다."""
    redacted = value
    for secret in extra_secret_values:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")

    redacted = _URL_IN_TEXT_PATTERN.sub(lambda match: _redact_url_query_string(match.group(0)), redacted)
    redacted = _BEARER_PATTERN.sub("Bearer <redacted>", redacted)
    redacted = re.sub(r"api_key=[^&\s\"']+", "api_key=<redacted>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"apikey=[^&\s\"']+", "apikey=<redacted>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"crtfc_key=[^&\s\"']+", "crtfc_key=<redacted>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"(?<=[?&])key=[^&\s\"']+", "key=<redacted>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"token=[^&\s\"']+", "token=<redacted>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"access_token=[^&\s\"']+", "access_token=<redacted>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"secret=[^&\s\"']+", "secret=<redacted>", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"password=[^&\s\"']+", "password=<redacted>", redacted, flags=re.IGNORECASE)
    return redacted


def sanitize_http_failure(
    exc: BaseException,
    *,
    endpoint_url: str,
    extra_secret_values: Sequence[str] = (),
) -> str:
    """urllib/JSON/network 예외 → sanitized error text (raw URL/query/secret 미포함)."""
    detail = str(exc).strip()
    if detail:
        raw = f"{type(exc).__name__}: {detail}"
    else:
        raw = type(exc).__name__
    safe = redact_discovery_http_text(
        raw,
        extra_secret_values=(endpoint_url, *extra_secret_values),
    )
    return f"discovery HTTP request failed: {safe}"


def fetch_kr_discovery_http_payload(
    *,
    endpoint_url: str,
    timeout_seconds: float = 15.0,
    urlopen_fn: UrlopenFn | None = None,
    extra_secret_values: Sequence[str] = (),
) -> Mapping[str, Any]:
    """operator-supplied endpoint → JSON object (records list). env/API key/header 없음."""
    normalized_url = normalize_required_string(endpoint_url, field_name="endpoint_url")
    _validate_http_endpoint_url(normalized_url)
    if timeout_seconds <= 0:
        raise KrDiscoveryHttpError("fetch", "timeout_seconds must be greater than 0")

    opener = urlopen_fn or urlopen
    request = Request(normalized_url, method="GET")

    try:
        with opener(request, timeout=timeout_seconds) as response:
            raw_bytes = response.read()
    except HTTPError as exc:
        message = sanitize_http_failure(
            exc,
            endpoint_url=normalized_url,
            extra_secret_values=extra_secret_values,
        )
        raise KrDiscoveryHttpError("fetch", message) from None
    except URLError as exc:
        message = sanitize_http_failure(
            exc,
            endpoint_url=normalized_url,
            extra_secret_values=extra_secret_values,
        )
        raise KrDiscoveryHttpError("fetch", message) from None
    except Exception as exc:
        message = sanitize_http_failure(
            exc,
            endpoint_url=normalized_url,
            extra_secret_values=extra_secret_values,
        )
        raise KrDiscoveryHttpError("fetch", message) from None

    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise KrDiscoveryHttpError("parse", "discovery HTTP response is not valid UTF-8") from None

    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        raise KrDiscoveryHttpError("parse", "discovery HTTP response is not valid JSON") from None

    if not isinstance(payload, dict):
        raise KrDiscoveryHttpError("parse", "discovery HTTP response root must be a JSON object")

    return payload


def _validate_http_endpoint_url(endpoint_url: str) -> None:
    parsed = urlparse(endpoint_url)
    if parsed.scheme not in {"http", "https"}:
        raise KrDiscoveryHttpError("fetch", "endpoint_url must use http or https scheme")
    if not parsed.netloc:
        raise KrDiscoveryHttpError("fetch", "endpoint_url must include a host")


def _redact_url_query_string(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    return urlunparse(parsed._replace(query="<redacted>"))
