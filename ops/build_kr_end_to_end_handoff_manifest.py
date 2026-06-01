#!/usr/bin/env python3
"""KR end-to-end operator handoff manifest builder (3H7/3H14).

기존 preflight/handoff artifact 경로·무결성 메타데이터만 색인.
3H14: 생성 manifest는 기존 verifier로 validate-before-commit 후에만 atomic replace.
artifact body embed·명령 실행·live fetch/smoke·config mutation/trading 없음.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Literal, TextIO

from verify_kr_end_to_end_handoff_manifest import (
    KrEndToEndHandoffManifestVerifyError,
    verify_kr_end_to_end_handoff_manifest,
)

StageName = Literal["args", "parse", "validate", "write", "complete"]

_CLI_MODE = "kr-end-to-end-handoff-manifest-build"
_MANIFEST_MODE = "kr-end-to-end-handoff-manifest"
_GENERATED_BY = "ops/build_kr_end_to_end_handoff_manifest.py"

_STRUCTURED_PLAN_MODE = "kr-end-to-end-intake-followup-plan"
_VALIDATION_REPORT_MODE = "kr-end-to-end-preflight-plan-validation-report"

# manifest artifact role → (CLI dest name, kind)
_ARTIFACT_SPECS: tuple[tuple[str, str, str], ...] = (
    ("preflight_summary", "preflight_summary", "json"),
    ("plan_md", "plan_md", "markdown"),
    ("structured_plan", "structured_plan", "json"),
    ("validation_report", "validation_report", "json"),
)


class KrEndToEndHandoffManifestError(ValueError):
    """handoff manifest builder 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _read_file_bytes(path: Path) -> bytes:
    """artifact 파일 바이트를 읽는다."""
    try:
        return path.read_bytes()
    except OSError:
        raise KrEndToEndHandoffManifestError("validate", "artifact file not readable") from None


def _sha256_hex(data: bytes) -> str:
    """바이트 payload의 sha256 lowercase hex digest를 반환한다."""
    return hashlib.sha256(data).hexdigest()


def _parse_json_root(data: bytes, *, role: str) -> dict[str, Any]:
    """JSON artifact root object를 파싱한다."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        raise KrEndToEndHandoffManifestError("parse", f"{role} JSON parse failed") from None
    if not isinstance(payload, dict):
        raise KrEndToEndHandoffManifestError("parse", f"{role} root must be an object")
    return payload


def _validate_preflight_summary_payload(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """preflight summary JSON에서 nullable mode/status/stage를 추출·검증한다."""
    json_mode = payload.get("mode")
    json_status = payload.get("status")
    json_stage = payload.get("stage")
    if json_status is not None and json_status != "ok":
        raise KrEndToEndHandoffManifestError("validate", "preflight summary status must be ok when present")
    return (
        json_mode if isinstance(json_mode, str) else None,
        json_status if isinstance(json_status, str) else None,
        json_stage if isinstance(json_stage, str) else None,
    )


def _validate_structured_plan_payload(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """structured plan JSON mode/review_only를 검증하고 nullable status/stage를 반환한다."""
    if payload.get("mode") != _STRUCTURED_PLAN_MODE:
        raise KrEndToEndHandoffManifestError("validate", "structured plan mode mismatch")
    if payload.get("review_only") is not True:
        raise KrEndToEndHandoffManifestError("validate", "structured plan review_only must be true")
    return _STRUCTURED_PLAN_MODE, None, None


def _validate_validation_report_payload(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """validation report JSON mode/status/stage를 검증한다."""
    if payload.get("mode") != _VALIDATION_REPORT_MODE:
        raise KrEndToEndHandoffManifestError("validate", "validation report mode mismatch")
    if payload.get("status") != "ok":
        raise KrEndToEndHandoffManifestError("validate", "validation report status must be ok")
    if payload.get("stage") != "complete":
        raise KrEndToEndHandoffManifestError("validate", "validation report stage must be complete")
    return _VALIDATION_REPORT_MODE, "ok", "complete"


def _index_artifact(role: str, kind: str, path: Path) -> dict[str, Any]:
    """단일 artifact의 path/existence/size/sha256 및 JSON 메타데이터를 색인한다."""
    resolved = path.resolve()
    if not resolved.exists():
        raise KrEndToEndHandoffManifestError("validate", f"{role} artifact not found")
    if not resolved.is_file():
        raise KrEndToEndHandoffManifestError("validate", f"{role} artifact is not a file")

    data = _read_file_bytes(resolved)
    size_bytes = len(data)
    digest = _sha256_hex(data)

    json_mode: str | None = None
    json_status: str | None = None
    json_stage: str | None = None

    if kind == "json":
        payload = _parse_json_root(data, role=role)
        if role == "preflight_summary":
            json_mode, json_status, json_stage = _validate_preflight_summary_payload(payload)
        elif role == "structured_plan":
            json_mode, json_status, json_stage = _validate_structured_plan_payload(payload)
        elif role == "validation_report":
            json_mode, json_status, json_stage = _validate_validation_report_payload(payload)

    return {
        "role": role,
        "path": str(resolved),
        "exists": True,
        "kind": kind,
        "size_bytes": size_bytes,
        "sha256": digest,
        "json_mode": json_mode,
        "json_status": json_status,
        "json_stage": json_stage,
    }


def _collect_artifact_inputs(args: argparse.Namespace) -> list[tuple[str, str, Path]]:
    """CLI에서 제공된 artifact 입력만 role 순서로 수집한다."""
    provided: list[tuple[str, str, Path]] = []
    for role, dest, kind in _ARTIFACT_SPECS:
        raw = getattr(args, dest, None)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            raise KrEndToEndHandoffManifestError("args", f"{dest} path is required when supplied")
        provided.append((role, kind, Path(text)))
    return provided


def build_handoff_manifest(artifact_paths: dict[str, Path]) -> dict[str, Any]:
    """검증된 artifact 경로 dict로 handoff manifest dict를 구성한다."""
    artifacts: list[dict[str, Any]] = []
    for role, _dest, kind in _ARTIFACT_SPECS:
        path = artifact_paths.get(role)
        if path is None:
            continue
        artifacts.append(_index_artifact(role, kind, path))

    return {
        "version": 1,
        "mode": _MANIFEST_MODE,
        "status": "ok",
        "stage": "complete",
        "generated_by": _GENERATED_BY,
        "artifacts": artifacts,
        "artifacts_count": len(artifacts),
        "all_artifacts_present": True,
        "commands_execute_in_builder": False,
        "review_only": True,
    }


def _write_manifest_output(path: Path, manifest: dict[str, Any], *, force: bool) -> None:
    """handoff manifest JSON을 temp write → verifier 검증 → atomic replace로 기록한다."""
    manifest_out = path.resolve()
    if manifest_out.exists() and not force:
        raise KrEndToEndHandoffManifestError("write", "output already exists: manifest_out")

    manifest_out.parent.mkdir(parents=True, exist_ok=True)
    temp_path = manifest_out.parent / f".tmp_handoff_manifest_{uuid.uuid4().hex}.json"
    try:
        serialized = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        temp_path.write_text(serialized, encoding="utf-8")
        verify_kr_end_to_end_handoff_manifest(temp_path)
        temp_path.replace(manifest_out)
    except KrEndToEndHandoffManifestVerifyError as exc:
        raise KrEndToEndHandoffManifestError("validate", exc.message) from None
    except OSError as exc:
        raise KrEndToEndHandoffManifestError(
            "write",
            f"output write failed: {type(exc).__name__}",
        ) from None
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def build_kr_end_to_end_handoff_manifest(
    *,
    manifest_out: Path,
    preflight_summary: Path | None = None,
    plan_md: Path | None = None,
    structured_plan: Path | None = None,
    validation_report: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """artifact를 검증·색인하고 manifest_out에 atomic write한다."""
    artifact_paths: dict[str, Path] = {}
    if preflight_summary is not None:
        artifact_paths["preflight_summary"] = preflight_summary
    if plan_md is not None:
        artifact_paths["plan_md"] = plan_md
    if structured_plan is not None:
        artifact_paths["structured_plan"] = structured_plan
    if validation_report is not None:
        artifact_paths["validation_report"] = validation_report

    if not artifact_paths:
        raise KrEndToEndHandoffManifestError("args", "at least one artifact input is required")

    manifest = build_handoff_manifest(artifact_paths)
    _write_manifest_output(manifest_out, manifest, force=force)
    return manifest


def _build_success_payload(manifest_out: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """CLI/API 성공 JSON payload를 구성한다."""
    return {
        "status": "ok",
        "stage": "complete",
        "mode": _CLI_MODE,
        "manifest_out": str(manifest_out.resolve()),
        "artifacts_count": manifest["artifacts_count"],
        "all_artifacts_present": manifest["all_artifacts_present"],
        "commands_execute_in_builder": False,
        "review_only": True,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KR end-to-end handoff manifest builder — read-only artifact integrity index.",
    )
    parser.add_argument("--preflight-summary", default=None, help="preflight summary JSON path")
    parser.add_argument("--plan-md", default=None, help="follow-up plan markdown path")
    parser.add_argument("--structured-plan", default=None, help="structured follow-up plan JSON path")
    parser.add_argument("--validation-report", default=None, help="structured plan validation report JSON path")
    parser.add_argument("--manifest-out", required=True, help="handoff manifest JSON output path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing manifest_out only",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _emit_json(payload: dict[str, Any], *, stream: TextIO) -> None:
    json.dump(payload, stream, indent=2, ensure_ascii=False)
    stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not str(args.manifest_out).strip():
        error_payload = {
            "status": "error",
            "stage": "args",
            "message": "manifest out path is required",
            "mode": _CLI_MODE,
        }
        if args.json:
            _emit_json(error_payload, stream=sys.stdout)
        else:
            print(error_payload["message"], file=sys.stderr)
        return 1

    manifest_out = Path(args.manifest_out)

    try:
        artifact_inputs = _collect_artifact_inputs(args)
        if not artifact_inputs:
            raise KrEndToEndHandoffManifestError("args", "at least one artifact input is required")

        artifact_paths = {role: path for role, _kind, path in artifact_inputs}
        manifest = build_handoff_manifest(artifact_paths)
        _write_manifest_output(manifest_out, manifest, force=bool(args.force))
    except KrEndToEndHandoffManifestError as exc:
        error_payload = {"status": "error", "stage": exc.stage, "message": exc.message, "mode": _CLI_MODE}
        if args.json:
            _emit_json(error_payload, stream=sys.stdout)
        else:
            print(exc.message, file=sys.stderr)
        return 1

    payload = _build_success_payload(manifest_out, manifest)
    if args.json:
        _emit_json(payload, stream=sys.stdout)
    else:
        print(f"handoff manifest: ok ({payload['stage']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
