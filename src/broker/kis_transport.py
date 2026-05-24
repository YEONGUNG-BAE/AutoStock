from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class KisHttpResponse:
    """KIS HTTP 응답 래퍼. raw text와 파싱된 JSON body를 함께 보관한다."""

    status_code: int
    headers: Mapping[str, str]
    text: str
    json_body: dict[str, Any] | list[Any] | None
    request_id: str | None = None


class KisHttpTransport(Protocol):
    """테스트에서 fake transport로 대체 가능한 KIS HTTP transport 프로토콜."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        timeout_seconds: float,
    ) -> KisHttpResponse:
        """HTTP 요청을 수행하고 KisHttpResponse를 반환한다."""
        ...


class StdlibKisHttpTransport:
    """표준 라이브러리 urllib 기반 KIS HTTP transport."""

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        timeout_seconds: float,
    ) -> KisHttpResponse:
        final_url = _append_query_params(url, params)
        data = None
        if json_body is not None:
            data = json.dumps(dict(json_body)).encode("utf-8")

        request = urllib.request.Request(
            final_url,
            data=data,
            headers=dict(headers),
            method=method.upper(),
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                text = response.read().decode("utf-8")
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                return KisHttpResponse(
                    status_code=response.status,
                    headers=response_headers,
                    text=text,
                    json_body=_parse_json_body(text),
                    request_id=response_headers.get("tr_cont") or response_headers.get("x-request-id"),
                )
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            response_headers = {key.lower(): value for key, value in exc.headers.items()} if exc.headers else {}
            return KisHttpResponse(
                status_code=exc.code,
                headers=response_headers,
                text=text,
                json_body=_parse_json_body(text),
                request_id=response_headers.get("tr_cont") or response_headers.get("x-request-id"),
            )


def _append_query_params(url: str, params: Mapping[str, str] | None) -> str:
    if not params:
        return url
    query = "&".join(f"{key}={value}" for key, value in params.items())
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{query}"


def _parse_json_body(text: str) -> dict[str, Any] | list[Any] | None:
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict | list):
        return parsed
    return None


__all__ = [
    "KisHttpResponse",
    "KisHttpTransport",
    "StdlibKisHttpTransport",
]
