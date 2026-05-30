from __future__ import annotations

import re
from typing import Any, Callable

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from domain._strings import normalize_required_string

# 3C2: OpenDART corp-code master ZIP fetch 전용. urllib stdlib HTTP는 이 모듈에만 격리한다.

OPENDART_CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
DEFAULT_TIMEOUT_SECONDS = 10.0

_CRTFC_KEY_QUERY_PATTERN = re.compile(r"crtfc_key=[^&\s\"']+", re.IGNORECASE)
_ZIP_MAGIC_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

UrlopenFn = Callable[..., Any]


class DartCorpCodeHttpError(RuntimeError):
    """OpenDART corp-code master HTTP 실패. message에 api_key·full query URL이 포함되면 안 된다."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def redact_secrets(text: str, *, secret: str | None = None) -> str:
    """로그/에러용 문자열에서 crtfc_key query 및 알려진 secret 값을 제거한다."""
    redacted = text
    if secret:
        redacted = redacted.replace(secret, "[REDACTED]")
    return _CRTFC_KEY_QUERY_PATTERN.sub("crtfc_key=[REDACTED]", redacted)


def sanitize_http_failure(*, reason: str, secret: str | None = None) -> DartCorpCodeHttpError:
    """urllib 예외 str(exc) 대신 sanitized DartCorpCodeHttpError를 만든다."""
    safe_reason = redact_secrets(reason, secret=secret)
    return DartCorpCodeHttpError(f"OpenDART corp-code HTTP request failed: {safe_reason}")


def _is_zip_bytes(payload: bytes) -> bool:
    return payload.startswith(_ZIP_MAGIC_PREFIXES)


def fetch_corp_code_zip_bytes(
    *,
    api_key: str,
    urlopen_fn: UrlopenFn | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """OpenDART corpCode.xml endpoint에서 corp-code master ZIP bytes를 반환한다."""
    normalized_api_key = normalize_required_string(api_key, field_name="api_key")
    opener = urlopen_fn or urlopen

    query = urlencode({"crtfc_key": normalized_api_key})
    request_url = f"{OPENDART_CORP_CODE_URL}?{query}"
    request = Request(request_url, method="GET")

    try:
        with opener(request, timeout=timeout_seconds) as response:
            raw_bytes = response.read()
    except HTTPError as exc:
        raise sanitize_http_failure(
            reason=str(exc),
            secret=normalized_api_key,
        ) from None
    except URLError as exc:
        raise sanitize_http_failure(
            reason=str(exc.reason) if exc.reason is not None else "network error",
            secret=normalized_api_key,
        ) from None

    if not _is_zip_bytes(raw_bytes):
        raise DartCorpCodeHttpError("OpenDART corp-code endpoint did not return a ZIP")

    return raw_bytes
