from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm.ollama_client import HttpResponse, OllamaApiError, OllamaClient


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None, float]] = []

    def __call__(self, method: str, url: str, payload: dict[str, Any] | None, timeout_seconds: float) -> HttpResponse:
        self.calls.append((method, url, payload, timeout_seconds))
        return self.responses.pop(0)


def test_chat_payload_includes_json_mode_and_think_false() -> None:
    transport = FakeTransport([_chat_response()])
    client = OllamaClient(transport=transport)

    client.chat_json(model="qwen3.6:35b", messages=[{"role": "user", "content": "ping"}])

    _, _, payload, _ = transport.calls[0]
    assert payload is not None
    assert payload["model"] == "qwen3.6:35b"
    assert payload["messages"] == [{"role": "user", "content": "ping"}]
    assert payload["stream"] is False
    assert payload["format"] == "json"
    assert payload["think"] is False


def test_chat_payload_includes_options_and_keep_alive() -> None:
    transport = FakeTransport([_chat_response()])
    client = OllamaClient(transport=transport)

    client.chat_json(
        model="qwen3.6:35b",
        messages=[{"role": "user", "content": "ping"}],
        temperature=0,
        seed=42,
        num_ctx=4096,
        keep_alive="24h",
    )

    _, _, payload, _ = transport.calls[0]
    assert payload is not None
    assert payload["options"] == {"temperature": 0, "seed": 42, "num_ctx": 4096}
    assert payload["keep_alive"] == "24h"


def test_timeout_setting_is_passed_to_transport() -> None:
    transport = FakeTransport([_chat_response()])
    client = OllamaClient(timeout_seconds=7.5, transport=transport)

    client.chat_json(model="qwen3.6:35b", messages=[{"role": "user", "content": "ping"}])

    assert transport.calls[0][3] == 7.5


def test_retry_default_is_zero() -> None:
    transport = FakeTransport([HttpResponse(status_code=500, body='{"error": "temporary"}')])
    client = OllamaClient(transport=transport)

    with pytest.raises(OllamaApiError):
        client.chat_json(model="qwen3.6:35b", messages=[{"role": "user", "content": "ping"}])

    assert len(transport.calls) == 1


def test_retry_count_retries_the_configured_number_of_times() -> None:
    transport = FakeTransport(
        [
            HttpResponse(status_code=500, body='{"error": "temporary"}'),
            HttpResponse(status_code=500, body='{"error": "temporary"}'),
            _chat_response(),
        ]
    )
    client = OllamaClient(retry_count=2, transport=transport)

    response = client.chat_json(model="qwen3.6:35b", messages=[{"role": "user", "content": "ping"}])

    assert len(transport.calls) == 3
    assert response["message"]["content"] == '{"ok": true, "action": "HOLD", "summary_one_liner": "ok"}'


def test_ollama_api_error_preserves_error_payload() -> None:
    transport = FakeTransport([HttpResponse(status_code=400, body='{"error": "think is unsupported"}')])
    client = OllamaClient(transport=transport)

    with pytest.raises(OllamaApiError) as exc_info:
        client.chat_json(model="old-model", messages=[{"role": "user", "content": "ping"}], think=True)

    assert exc_info.value.status_code == 400
    assert exc_info.value.error_payload == {"error": "think is unsupported"}
    assert exc_info.value.raw_response == '{"error": "think is unsupported"}'


def _chat_response() -> HttpResponse:
    return HttpResponse(
        status_code=200,
        body=json.dumps(
            {
                "message": {
                    "content": '{"ok": true, "action": "HOLD", "summary_one_liner": "ok"}',
                }
            }
        ),
    )
