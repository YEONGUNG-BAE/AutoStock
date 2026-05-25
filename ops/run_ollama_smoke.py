#!/usr/bin/env python3
"""로컬 Ollama 환경에서 JsonRunner JSON-only smoke를 수동 실행한다.

투자 판단 schema, broker, KIS, paper ledger를 호출하지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config.settings import SettingsError, load_settings
from llm.json_runner import JsonRunner, JsonRunnerOptions
from llm.ollama_client import OllamaClient, OllamaClientError

DEFAULT_CONFIG_PATH = "config/config.toml.example"

DEFAULT_SMOKE_PROMPT = """Return JSON only. No markdown. No prose.
Schema:
{
  "ok": true,
  "message": "pong",
  "number": 1
}"""


class OllamaSmokeResponse(BaseModel):
    """투자 판단과 무관한 dummy smoke schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: Literal[True]
    message: Annotated[str, Field(min_length=1)]
    number: Literal[1]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AutoStock local Ollama JSON runner smoke (dummy schema only).",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"config path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Ollama host URL override (default: config llm.host)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Ollama model name override (default: config llm.model)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="request timeout override (default: config llm.timeout_seconds)",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="smoke prompt override (default: built-in dummy JSON prompt)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print non-sensitive request metadata (no raw prompt/response body)",
    )
    return parser


def _fail(stage: str, reason: str) -> int:
    print("Ollama smoke: FAIL")
    print(f"stage: {stage}")
    print(f"reason: {reason}")
    return 1


def _prompt_fingerprint(prompt: str) -> str:
    """verbose 출력용 prompt 길이/해시 (원문 출력 금지)."""
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    return f"len={len(prompt)} sha256={digest}"


def _resolve_runtime_llm(
    args: argparse.Namespace,
) -> tuple[str, Path, str, str, float, object]:
    """config를 읽기 전용으로 로드하고 CLI override를 적용한다."""
    config_path = Path(args.config)
    try:
        settings = load_settings(config_path)
    except SettingsError as exc:
        raise _ConfigLoadError(str(exc)) from exc
    except OSError as exc:
        raise _ConfigLoadError(str(exc)) from exc

    host = args.host or settings.llm.host
    model = args.model or settings.llm.model
    timeout_seconds = args.timeout if args.timeout is not None else settings.llm.timeout_seconds
    return str(config_path), host, model, timeout_seconds, settings


class _ConfigLoadError(Exception):
    """config 로드 실패를 stage=config로 매핑하기 위한 내부 예외."""


def _check_connection(client: OllamaClient, model: str) -> str | None:
    """Ollama server/model reachable 확인. 실패 시 reason 문자열 반환."""
    try:
        client.get_version()
    except OllamaClientError as exc:
        return f"Ollama server unreachable: {exc}"

    try:
        client.show_model(model)
    except OllamaClientError as exc:
        return f"configured model unreachable: {model!r} ({exc})"

    return None


def _map_runner_error(result_error_type: str | None) -> str:
    if result_error_type == "parse_error":
        return "parse"
    if result_error_type == "validation_error":
        return "validation"
    return "connection"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    prompt = args.prompt or DEFAULT_SMOKE_PROMPT

    try:
        config_path, host, model, timeout_seconds, settings = _resolve_runtime_llm(args)
    except _ConfigLoadError as exc:
        return _fail("config", str(exc))

    if args.verbose:
        print(f"verbose: config={config_path}")
        print(f"verbose: host={host}")
        print(f"verbose: model={model}")
        print(f"verbose: timeout_seconds={timeout_seconds}")
        print(f"verbose: prompt {_prompt_fingerprint(prompt)}")

    client = OllamaClient(
        host=host,
        timeout_seconds=timeout_seconds,
        retry_count=settings.llm.retry_count,
    )

    connection_error = _check_connection(client, model)
    if connection_error is not None:
        return _fail("connection", connection_error)

    runner = JsonRunner(client)
    options = JsonRunnerOptions(
        model=model,
        think=settings.llm.default_think,
        temperature=0,
        seed=settings.llm.seed,
        num_ctx=settings.llm.default_num_ctx,
        keep_alive=settings.llm.keep_alive,
    )
    messages = [{"role": "user", "content": prompt}]

    try:
        result = runner.run(OllamaSmokeResponse, messages, options)
    except Exception as exc:
        return _fail("connection", f"unexpected runner error: {exc}")

    if not result.ok:
        stage = _map_runner_error(result.error_type)
        reason = result.error_message or "unknown smoke failure"
        if stage == "parse" and result.raw_content and result.raw_content.strip().startswith("```"):
            reason = "markdown fence detected; JSON-only response required"
        return _fail(stage, reason)

    # JsonRunner Pydantic 검증 통과 후 semantic invariant 재확인
    try:
        validated = result.validated
        if validated is None:
            return _fail("validation", "validated payload is missing after successful run")
        OllamaSmokeResponse.model_validate(validated.model_dump())
    except ValidationError as exc:
        return _fail("validation", str(exc))

    json_parsed = "yes" if result.parsed_json is not None else "no"
    latency_ms = int(result.latency_ms)

    print("Ollama smoke: PASS")
    print(f"config: {config_path}")
    print(f"host: {host}")
    print(f"model: {model}")
    print("temperature: 0")
    print(f"json_parsed: {json_parsed}")
    print("schema_validated: yes")
    print(f"latency_ms: {latency_ms}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
