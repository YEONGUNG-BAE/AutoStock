from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm.run_manifest import BaselineStatus, RunManifest


def test_run_manifest_is_created_as_candidate() -> None:
    manifest = RunManifest.from_ollama_metadata(
        model_name="qwen3.6:35b",
        show_model_response=_show_model_response(),
        version_response={"version": "0.9.0"},
        think=False,
        temperature=0,
        seed=42,
        num_ctx=4096,
        keep_alive="24h",
    )

    assert manifest.baseline_status == BaselineStatus.CANDIDATE
    assert manifest.to_dict()["baseline_status"] == "candidate"


def test_run_manifest_extracts_model_metadata_from_show_model_response() -> None:
    manifest = RunManifest.from_ollama_metadata(
        model_name="qwen3.6:35b",
        show_model_response=_show_model_response(),
        version_response={"version": "0.9.0"},
        think=False,
        temperature=0,
        seed=42,
        num_ctx=4096,
        keep_alive="24h",
    )

    assert manifest.ollama_version == "0.9.0"
    assert manifest.model_digest == "sha256:TEST_DIGEST"
    assert manifest.model_details == {"parameter_size": "35B", "quantization_level": "Q4_K_M"}
    assert manifest.model_size == 123456


def test_run_manifest_missing_model_metadata_stays_none() -> None:
    manifest = RunManifest.from_ollama_metadata(
        model_name="qwen3.6:35b",
        show_model_response={},
        version_response={},
        think=False,
        temperature=0,
        seed=42,
        num_ctx=4096,
        keep_alive="24h",
    )

    assert manifest.ollama_version is None
    assert manifest.model_digest is None
    assert manifest.model_details is None
    assert manifest.model_size is None


def test_run_manifest_can_be_saved_as_json_and_markdown(tmp_path: Path) -> None:
    manifest = RunManifest.from_ollama_metadata(
        model_name="qwen3.6:35b",
        show_model_response=_show_model_response(),
        version_response={"version": "0.9.0"},
        think=False,
        temperature=0,
        seed=42,
        num_ctx=4096,
        keep_alive="24h",
    )
    json_path = tmp_path / "manifest.json"
    markdown_path = tmp_path / "manifest.md"

    manifest.save_json(json_path)
    manifest.save_markdown(markdown_path)

    assert json.loads(json_path.read_text(encoding="utf-8"))["baseline_status"] == "candidate"
    assert "`model_name`: qwen3.6:35b" in markdown_path.read_text(encoding="utf-8")


def _show_model_response() -> dict[str, object]:
    return {
        "digest": "sha256:TEST_DIGEST",
        "details": {"parameter_size": "35B", "quantization_level": "Q4_K_M"},
        "size": 123456,
    }
