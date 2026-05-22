from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


JsonObject = dict[str, Any]
Transport = Callable[[str, str, JsonObject | None, float], "HttpResponse"]


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: str


class OllamaClientError(RuntimeError):
    """Ollama HTTP client에서 발생한 오류의 기본 타입이다."""


class OllamaApiError(OllamaClientError):
    """Ollama API 오류 응답을 원문과 함께 보존한다."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_payload: Any = None,
        raw_response: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_payload = error_payload
        self.raw_response = raw_response


class OllamaClient:
    """Ollama HTTP API를 stream=false JSON 모드로 호출하는 최소 client다."""

    def __init__(
        self,
        *,
        host: str = "http://localhost:11434",
        timeout_seconds: float = 120,
        retry_count: int = 0,
        transport: Transport | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retry_count = retry_count
        self._transport = transport or _urllib_transport

    def chat_json(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, str]],
        think: bool = False,
        temperature: float = 0,
        seed: int = 42,
        num_ctx: int = 4096,
        keep_alive: str = "24h",
    ) -> JsonObject:
        payload: JsonObject = {
            "model": model,
            "messages": [dict(message) for message in messages],
            "stream": False,
            "format": "json",
            "think": think,
            "options": {
                "temperature": temperature,
                "seed": seed,
                "num_ctx": num_ctx,
            },
            "keep_alive": keep_alive,
        }
        return self._request_json("POST", "/api/chat", payload)

    def show_model(self, model: str) -> JsonObject:
        return self._request_json("POST", "/api/show", {"model": model})

    def list_models(self) -> JsonObject:
        return self._request_json("GET", "/api/tags", None)

    def get_version(self) -> JsonObject:
        return self._request_json("GET", "/api/version", None)

    def _request_json(self, method: str, path: str, payload: JsonObject | None) -> JsonObject:
        attempts = self.retry_count + 1
        last_error: Exception | None = None

        for _ in range(attempts):
            try:
                response = self._transport(method, f"{self.host}{path}", payload, self.timeout_seconds)
                return _decode_response(response)
            except OllamaClientError as exc:
                last_error = exc
            except OSError as exc:
                last_error = OllamaClientError(str(exc))

        if last_error is None:
            raise OllamaClientError("Ollama request failed without an error.")
        raise last_error


def _decode_response(response: HttpResponse) -> JsonObject:
    if response.status_code >= 400:
        error_payload = _parse_json_or_none(response.body)
        raise OllamaApiError(
            f"Ollama API error: status_code={response.status_code}.",
            status_code=response.status_code,
            error_payload=error_payload,
            raw_response=response.body,
        )

    parsed_body = _parse_json_or_none(response.body)
    if not isinstance(parsed_body, dict):
        raise OllamaApiError("Ollama API returned a non-JSON object response.", raw_response=response.body)
    return parsed_body


def _parse_json_or_none(raw_value: str) -> Any:
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return None


def _urllib_transport(method: str, url: str, payload: JsonObject | None, timeout_seconds: float) -> HttpResponse:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
            return HttpResponse(status_code=response.status, body=response_body)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8")
        return HttpResponse(status_code=exc.code, body=error_body)
