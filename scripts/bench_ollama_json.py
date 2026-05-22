from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from llm.json_runner import JsonRunner, JsonRunnerOptions
from llm.ollama_client import OllamaApiError, OllamaClient, OllamaClientError
from llm.run_manifest import RunManifest
from schemas.llm_smoke import LlmSmokeResponse


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = OllamaClient(timeout_seconds=args.timeout_seconds, retry_count=0)
    runner = JsonRunner(client)
    options = JsonRunnerOptions(
        model=args.model,
        think=args.think,
        temperature=args.temperature,
        seed=args.seed,
        num_ctx=args.num_ctx,
        keep_alive=args.keep_alive,
    )

    show_model_response = _safe_show_model(client, args.model)
    version_response = _safe_get_version(client)
    manifest = RunManifest.from_ollama_metadata(
        model_name=args.model,
        show_model_response=show_model_response,
        version_response=version_response,
        think=args.think,
        temperature=args.temperature,
        seed=args.seed,
        num_ctx=args.num_ctx,
        keep_alive=args.keep_alive,
    )
    manifest.save_json(output_dir / "run_manifest.json")
    manifest.save_markdown(output_dir / "run_manifest.md")

    results: list[dict[str, object]] = []
    for run_index in range(args.runs):
        result = runner.run(LlmSmokeResponse, _smoke_messages(), options)
        raw_path = output_dir / f"run_{run_index + 1:03d}_raw.json"
        raw_path.write_text(
            json.dumps(result.raw_response, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        results.append(
            {
                "run_index": run_index + 1,
                "latency_ms": result.latency_ms,
                "parse_success": _is_parse_success(result.error_type),
                "validation_success": result.validated is not None,
                "error_type": result.error_type,
                "raw_response_path": str(raw_path),
            }
        )

    summary = _summarize(results)
    (output_dir / "summary.json").write_text(
        json.dumps({"manifest": manifest.to_dict(), "runs": results, "summary": summary}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a manual Ollama JSON smoke benchmark.")
    parser.add_argument("--model", default="qwen3.6:35b")
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--think", type=_parse_bool_arg, default=False)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--keep-alive", default="24h")
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--case", choices=["smoke"], default="smoke")
    parser.add_argument("--output-dir", default="memory/ollama_json_bench")
    return parser.parse_args()


def _parse_bool_arg(value: str) -> bool:
    normalized_value = value.lower()
    if normalized_value == "true":
        return True
    if normalized_value == "false":
        return False
    raise argparse.ArgumentTypeError("--think must be true or false.")


def _smoke_messages() -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": "Return only one JSON object matching the requested schema.",
        },
        {
            "role": "user",
            "content": '{"ok": true, "action": "HOLD", "summary_one_liner": "smoke test"}',
        },
    ]


def _safe_show_model(client: OllamaClient, model: str) -> dict[str, object]:
    try:
        return client.show_model(model)
    except OllamaApiError as exc:
        return {
            "error": str(exc),
            "payload": exc.error_payload,
            "raw_response": exc.raw_response,
        }
    except OllamaClientError as exc:
        return {"error": str(exc), "payload": None}


def _safe_get_version(client: OllamaClient) -> dict[str, object]:
    try:
        return client.get_version()
    except OllamaApiError as exc:
        return {
            "error": str(exc),
            "payload": exc.error_payload,
            "raw_response": exc.raw_response,
        }
    except OllamaClientError as exc:
        return {"error": str(exc), "payload": None}


def _is_parse_success(error_type: str | None) -> bool:
    return error_type not in {"parse_error", "ollama_api_error", "ollama_client_error"}


def _summarize(results: list[dict[str, object]]) -> dict[str, object]:
    run_count = len(results)
    parse_success_count = sum(1 for result in results if result["parse_success"])
    validation_success_count = sum(1 for result in results if result["validation_success"])
    latencies = [float(result["latency_ms"]) for result in results]
    error_types = [result["error_type"] for result in results if result["error_type"] is not None]
    return {
        "runs": run_count,
        "parse_success_count": parse_success_count,
        "validation_success_count": validation_success_count,
        "parse_success_rate": parse_success_count / run_count if run_count else 0,
        "validation_success_rate": validation_success_count / run_count if run_count else 0,
        "latency_ms_min": min(latencies) if latencies else None,
        "latency_ms_max": max(latencies) if latencies else None,
        "latency_ms_avg": sum(latencies) / run_count if run_count else None,
        "error_types": error_types,
    }


if __name__ == "__main__":
    raise SystemExit(main())
