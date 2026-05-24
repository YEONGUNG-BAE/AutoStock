from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from llm.ollama_client import OllamaApiError, OllamaClient, OllamaClientError


SchemaT = TypeVar("SchemaT", bound=BaseModel)


@dataclass(frozen=True)
class JsonRunnerOptions:
    model: str
    think: bool = False
    temperature: float = 0
    seed: int = 42
    num_ctx: int = 4096
    keep_alive: str = "24h"

    def __post_init__(self) -> None:
        if self.temperature != 0:
            raise ValueError("temperature must be 0 for deterministic trading decisions.")


@dataclass(frozen=True)
class JsonRunResult(Generic[SchemaT]):
    raw_response: Any
    raw_content: str | None
    parsed_json: Any
    validated: SchemaT | None
    latency_ms: float
    error_type: str | None = None
    error_message: str | None = None
    validation_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_type is None and self.validated is not None


class JsonRunner:
    """Ollama JSON 응답의 원문, JSON 파싱, Pydantic 검증 결과를 분리해 보존한다."""

    def __init__(self, client: OllamaClient) -> None:
        self._client = client

    def run(
        self,
        schema_type: type[SchemaT],
        messages: Sequence[Mapping[str, str]],
        options: JsonRunnerOptions,
    ) -> JsonRunResult[SchemaT]:
        started_at = time.perf_counter()
        raw_response: Any = None
        raw_content: str | None = None
        parsed_json: Any = None

        try:
            raw_response = self._client.chat_json(
                model=options.model,
                messages=messages,
                think=options.think,
                temperature=options.temperature,
                seed=options.seed,
                num_ctx=options.num_ctx,
                keep_alive=options.keep_alive,
            )
            raw_content = _extract_message_content(raw_response)
            parsed_json = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            return JsonRunResult(
                raw_response=raw_response,
                raw_content=raw_content,
                parsed_json=None,
                validated=None,
                latency_ms=_elapsed_ms(started_at),
                error_type="parse_error",
                error_message=str(exc),
            )
        except OllamaApiError as exc:
            return JsonRunResult(
                raw_response=exc.error_payload if exc.error_payload is not None else exc.raw_response,
                raw_content=exc.raw_response,
                parsed_json=None,
                validated=None,
                latency_ms=_elapsed_ms(started_at),
                error_type="ollama_api_error",
                error_message=str(exc),
            )
        except OllamaClientError as exc:
            return JsonRunResult(
                raw_response=raw_response,
                raw_content=raw_content,
                parsed_json=parsed_json,
                validated=None,
                latency_ms=_elapsed_ms(started_at),
                error_type="ollama_client_error",
                error_message=str(exc),
            )

        try:
            validated = schema_type.model_validate(parsed_json)
        except ValidationError as exc:
            return JsonRunResult(
                raw_response=raw_response,
                raw_content=raw_content,
                parsed_json=parsed_json,
                validated=None,
                latency_ms=_elapsed_ms(started_at),
                error_type="validation_error",
                error_message=str(exc),
                validation_error=exc.json(),
            )

        return JsonRunResult(
            raw_response=raw_response,
            raw_content=raw_content,
            parsed_json=parsed_json,
            validated=validated,
            latency_ms=_elapsed_ms(started_at),
        )


def _extract_message_content(raw_response: Any) -> str:
    if not isinstance(raw_response, dict):
        raise OllamaClientError("Ollama response must be a JSON object.")

    message = raw_response.get("message")
    if not isinstance(message, dict):
        raise OllamaClientError("Ollama response missing message object.")

    content = message.get("content")
    if not isinstance(content, str):
        raise OllamaClientError("Ollama response missing message.content string.")

    return content


def _elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000
