from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class BaselineStatus(StrEnum):
    CANDIDATE = "candidate"
    STABLE = "stable"


@dataclass(frozen=True)
class RunManifest:
    ollama_version: str | None
    model_name: str
    model_digest: str | None
    model_details: dict[str, Any] | None
    model_size: int | str | None
    recorded_at: str
    think: bool
    temperature: float
    seed: int
    num_ctx: int
    keep_alive: str
    baseline_status: BaselineStatus = BaselineStatus.CANDIDATE

    @classmethod
    def from_ollama_metadata(
        cls,
        *,
        model_name: str,
        show_model_response: dict[str, Any],
        version_response: dict[str, Any] | None,
        think: bool,
        temperature: float,
        seed: int,
        num_ctx: int,
        keep_alive: str,
        baseline_status: BaselineStatus = BaselineStatus.CANDIDATE,
    ) -> "RunManifest":
        model_details = _extract_model_details(show_model_response)
        return cls(
            ollama_version=_extract_ollama_version(version_response),
            model_name=model_name,
            model_digest=_extract_model_digest(show_model_response),
            model_details=model_details,
            model_size=_extract_model_size(show_model_response, model_details),
            recorded_at=datetime.now(UTC).isoformat(),
            think=think,
            temperature=temperature,
            seed=seed,
            num_ctx=num_ctx,
            keep_alive=keep_alive,
            baseline_status=baseline_status,
        )

    def to_dict(self) -> dict[str, Any]:
        manifest = asdict(self)
        manifest["baseline_status"] = self.baseline_status.value
        return manifest

    def save_json(self, path: str | Path) -> None:
        target_path = Path(path)
        _atomic_write_text(
            target_path,
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )

    def save_markdown(self, path: str | Path) -> None:
        manifest = self.to_dict()
        lines = ["# Ollama Run Manifest", ""]
        for key, value in manifest.items():
            rendered_value = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
            lines.append(f"- `{key}`: {rendered_value}")
        _atomic_write_text(Path(path), "\n".join(str(line) for line in lines) + "\n")


def _extract_ollama_version(version_response: dict[str, Any] | None) -> str | None:
    if not isinstance(version_response, dict):
        return None
    version = version_response.get("version")
    return version if isinstance(version, str) else None


def _extract_model_digest(show_model_response: dict[str, Any]) -> str | None:
    digest = show_model_response.get("digest") or show_model_response.get("model_digest")
    return digest if isinstance(digest, str) else None


def _extract_model_details(show_model_response: dict[str, Any]) -> dict[str, Any] | None:
    details = show_model_response.get("details")
    return details if isinstance(details, dict) else None


def _extract_model_size(show_model_response: dict[str, Any], model_details: dict[str, Any] | None) -> int | str | None:
    size = show_model_response.get("size") or show_model_response.get("model_size")
    if isinstance(size, int | str):
        return size

    if model_details is None:
        return None

    parameter_size = model_details.get("parameter_size")
    return parameter_size if isinstance(parameter_size, int | str) else None


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)
