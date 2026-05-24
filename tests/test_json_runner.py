from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm.json_runner import JsonRunner, JsonRunnerOptions
from llm.ollama_client import HttpResponse, OllamaClient
from schemas.llm_smoke import LlmSmokeResponse


class FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, Any] | None, float]] = []

    def __call__(self, method: str, url: str, payload: dict[str, Any] | None, timeout_seconds: float) -> HttpResponse:
        self.calls.append((method, url, payload, timeout_seconds))
        return self.response


def test_json_runner_options_rejects_non_zero_temperature() -> None:
    with pytest.raises(ValueError, match="temperature must be 0"):
        JsonRunnerOptions(model="qwen3.6:35b", temperature=0.1)


def test_json_runner_options_accepts_zero_temperature() -> None:
    options = JsonRunnerOptions(model="qwen3.6:35b", temperature=0)
    assert options.temperature == 0


def test_json_runner_parses_and_validates_normal_json_response() -> None:
    runner = _runner_for_content('{"ok": true, "action": "HOLD", "summary_one_liner": "ok"}')

    result = runner.run(LlmSmokeResponse, _messages(), _options())

    assert result.ok is True
    assert result.parsed_json == {"ok": True, "action": "HOLD", "summary_one_liner": "ok"}
    assert result.validated == LlmSmokeResponse(ok=True, action="HOLD", summary_one_liner="ok")
    assert result.raw_response["message"]["content"] == '{"ok": true, "action": "HOLD", "summary_one_liner": "ok"}'


def test_json_parse_failure_preserves_raw_response_and_uses_parse_error() -> None:
    runner = _runner_for_content("not-json")

    result = runner.run(LlmSmokeResponse, _messages(), _options())

    assert result.ok is False
    assert result.error_type == "parse_error"
    assert result.raw_content == "not-json"
    assert result.raw_response["message"]["content"] == "not-json"
    assert result.parsed_json is None


def test_validation_failure_preserves_raw_response_and_validation_error() -> None:
    runner = _runner_for_content('{"ok": true, "action": "BUY", "summary_one_liner": "bad"}')

    result = runner.run(LlmSmokeResponse, _messages(), _options())

    assert result.ok is False
    assert result.error_type == "validation_error"
    assert result.raw_content == '{"ok": true, "action": "BUY", "summary_one_liner": "bad"}'
    assert result.parsed_json == {"ok": True, "action": "BUY", "summary_one_liner": "bad"}
    assert result.validation_error is not None


def test_markdown_fence_response_fails_without_sanitize() -> None:
    runner = _runner_for_content('```json\n{"ok": true, "action": "HOLD", "summary_one_liner": "ok"}\n```')

    result = runner.run(LlmSmokeResponse, _messages(), _options())

    assert result.ok is False
    assert result.error_type == "parse_error"
    assert result.raw_content.startswith("```json")


def test_ollama_api_error_payload_is_preserved() -> None:
    transport = FakeTransport(HttpResponse(status_code=400, body='{"error": "think is unsupported"}'))
    runner = JsonRunner(OllamaClient(transport=transport))

    result = runner.run(LlmSmokeResponse, _messages(), _options(think=True))

    assert result.ok is False
    assert result.error_type == "ollama_api_error"
    assert result.raw_response == {"error": "think is unsupported"}
    assert result.parsed_json is None
    assert result.raw_content == '{"error": "think is unsupported"}'


def _runner_for_content(content: str) -> JsonRunner:
    transport = FakeTransport(
        HttpResponse(
            status_code=200,
            body=json.dumps({"message": {"content": content}}),
        )
    )
    return JsonRunner(OllamaClient(transport=transport))


def _messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "Return smoke JSON."}]


def _options(*, think: bool = False) -> JsonRunnerOptions:
    return JsonRunnerOptions(model="qwen3.6:35b", think=think)
