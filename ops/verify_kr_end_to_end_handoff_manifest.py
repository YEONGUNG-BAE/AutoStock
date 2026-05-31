#!/usr/bin/env python3
"""KR end-to-end operator handoff manifest verifier (3H8).

3H7 handoff manifest JSON → schema/integrity/metadata 재검증만 수행.
artifact/manifest mutation·명령 실행·live fetch/smoke·config mutation/trading 없음.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

StageName = Literal["args", "parse", "validate", "complete"]

_MODE = "kr-end-to-end-handoff-manifest-verification"
_MANIFEST_MODE = "kr-end-to-end-handoff-manifest"
_GENERATED_BY = "ops/build_kr_end_to_end_handoff_manifest.py"

_STRUCTURED_PLAN_MODE = "kr-end-to-end-intake-followup-plan"
_VALIDATION_REPORT_MODE = "kr-end-to-end-preflight-plan-validation-report"

_CANONICAL_ROLES: tuple[str, ...] = (
    "preflight_summary",
    "plan_md",
    "structured_plan",
    "validation_report",
)
_ROLE_KIND: dict[str, str] = {
    "preflight_summary": "json",
    "plan_md": "markdown",
    "structured_plan": "json",
    "validation_report": "json",
}

_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "mode",
        "status",
        "stage",
        "generated_by",
        "artifacts",
        "artifacts_count",
        "all_artifacts_present",
        "commands_execute_in_builder",
        "review_only",
    }
)
_ARTIFACT_ENTRY_KEYS = frozenset(
    {
        "role",
        "path",
        "exists",
        "kind",
        "size_bytes",
        "sha256",
        "json_mode",
        "json_status",
        "json_stage",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class KrEndToEndHandoffManifestVerifyError(ValueError):
    """handoff manifest verifier 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _required_nonblank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KrEndToEndHandoffManifestVerifyError("validate", f"{field_name} is required")
    return value.strip()


def load_handoff_manifest(path: Path) -> dict[str, Any]:
    """handoff manifest JSON 파일을 읽어 root object dict를 반환한다."""
    manifest_path = path.resolve()
    if not manifest_path.is_file():
        raise KrEndToEndHandoffManifestVerifyError("parse", "handoff manifest file not found")

    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        raise KrEndToEndHandoffManifestVerifyError("parse", "handoff manifest file not readable") from None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise KrEndToEndHandoffManifestVerifyError("parse", "handoff manifest JSON parse failed") from None

    if not isinstance(payload, dict):
        raise KrEndToEndHandoffManifestVerifyError("parse", "handoff manifest root must be an object")

    return payload


def _validate_sha256_format(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise KrEndToEndHandoffManifestVerifyError("validate", f"{field_name} must be a string")
    if not _SHA256_PATTERN.fullmatch(value):
        raise KrEndToEndHandoffManifestVerifyError("validate", f"{field_name} must be lowercase hex")
    return value


def _validate_artifact_roles(roles: list[str]) -> None:
    """canonical artifact role의 유일·상대 순서 subset만 허용한다."""
    known_index = {role: index for index, role in enumerate(_CANONICAL_ROLES)}
    positions: list[int] = []
    seen: set[str] = set()
    for role in roles:
        if role not in known_index:
            raise KrEndToEndHandoffManifestVerifyError("validate", "artifact role not recognized")
        if role in seen:
            raise KrEndToEndHandoffManifestVerifyError("validate", "artifact role duplicated")
        seen.add(role)
        positions.append(known_index[role])
    if positions != sorted(positions):
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact roles out of canonical order")


def _validate_artifact_entry_schema(entry: object, *, index: int) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact entry must be an object")

    missing = _ARTIFACT_ENTRY_KEYS - set(entry.keys())
    if missing:
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact entry missing required fields")

    role = _required_nonblank_string(entry.get("role"), field_name=f"artifacts[{index}].role")
    expected_kind = _ROLE_KIND.get(role)
    if expected_kind is None:
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact role not recognized")

    kind = entry.get("kind")
    if kind != expected_kind:
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact kind mismatch for role")

    if entry.get("exists") is not True:
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact exists must be true")

    path_text = _required_nonblank_string(entry.get("path"), field_name=f"artifacts[{index}].path")

    size_bytes = entry.get("size_bytes")
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact size_bytes must be a nonnegative integer")

    digest = _validate_sha256_format(entry.get("sha256"), field_name=f"artifacts[{index}].sha256")

    json_mode = entry.get("json_mode")
    if json_mode is not None and not isinstance(json_mode, str):
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact json_mode must be string or null")

    json_status = entry.get("json_status")
    if json_status is not None and not isinstance(json_status, str):
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact json_status must be string or null")

    json_stage = entry.get("json_stage")
    if json_stage is not None and not isinstance(json_stage, str):
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact json_stage must be string or null")

    return {
        "role": role,
        "path": path_text,
        "kind": kind,
        "size_bytes": size_bytes,
        "sha256": digest,
        "json_mode": json_mode,
        "json_status": json_status,
        "json_stage": json_stage,
    }


def _parse_json_artifact(data: bytes, *, role: str) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        raise KrEndToEndHandoffManifestVerifyError("parse", f"{role} JSON parse failed") from None
    if not isinstance(payload, dict):
        raise KrEndToEndHandoffManifestVerifyError("parse", f"{role} root must be an object")
    return payload


def _actual_preflight_summary_metadata(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    actual_status = payload.get("status") if isinstance(payload.get("status"), str) else None
    if actual_status is not None and actual_status != "ok":
        raise KrEndToEndHandoffManifestVerifyError(
            "validate",
            "preflight summary status must be ok when present",
        )
    actual_mode = payload.get("mode") if isinstance(payload.get("mode"), str) else None
    actual_stage = payload.get("stage") if isinstance(payload.get("stage"), str) else None
    return actual_mode, actual_status, actual_stage


def _actual_structured_plan_metadata(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    if payload.get("mode") != _STRUCTURED_PLAN_MODE:
        raise KrEndToEndHandoffManifestVerifyError("validate", "structured plan mode mismatch")
    if payload.get("review_only") is not True:
        raise KrEndToEndHandoffManifestVerifyError("validate", "structured plan review_only must be true")
    return _STRUCTURED_PLAN_MODE, None, None


def _actual_validation_report_metadata(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    if payload.get("mode") != _VALIDATION_REPORT_MODE:
        raise KrEndToEndHandoffManifestVerifyError("validate", "validation report mode mismatch")
    if payload.get("status") != "ok":
        raise KrEndToEndHandoffManifestVerifyError("validate", "validation report status must be ok")
    if payload.get("stage") != "complete":
        raise KrEndToEndHandoffManifestVerifyError("validate", "validation report stage must be complete")
    return _VALIDATION_REPORT_MODE, "ok", "complete"


def _compare_recorded_json_metadata(
    entry: dict[str, Any],
    actual_mode: str | None,
    actual_status: str | None,
    actual_stage: str | None,
) -> None:
    if entry["json_mode"] != actual_mode:
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact json_mode mismatch")
    if entry["json_status"] != actual_status:
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact json_status mismatch")
    if entry["json_stage"] != actual_stage:
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact json_stage mismatch")


def _verify_artifact_entry(entry: dict[str, Any]) -> None:
    """단일 artifact entry의 path/size/sha256 및 JSON metadata를 재검증한다."""
    artifact_path = Path(entry["path"]).resolve()
    if not artifact_path.exists():
        raise KrEndToEndHandoffManifestVerifyError("validate", f"{entry['role']} artifact not found")
    if not artifact_path.is_file():
        raise KrEndToEndHandoffManifestVerifyError("validate", f"{entry['role']} artifact is not a file")

    try:
        data = artifact_path.read_bytes()
    except OSError:
        raise KrEndToEndHandoffManifestVerifyError("validate", f"{entry['role']} artifact file not readable") from None

    actual_size = len(data)
    if actual_size != entry["size_bytes"]:
        raise KrEndToEndHandoffManifestVerifyError("validate", f"{entry['role']} artifact size mismatch")

    actual_digest = hashlib.sha256(data).hexdigest()
    if actual_digest != entry["sha256"]:
        raise KrEndToEndHandoffManifestVerifyError("validate", f"{entry['role']} artifact sha256 mismatch")

    if entry["kind"] != "json":
        return

    payload = _parse_json_artifact(data, role=entry["role"])
    role = entry["role"]
    if role == "preflight_summary":
        actual_metadata = _actual_preflight_summary_metadata(payload)
    elif role == "structured_plan":
        if entry["json_status"] is not None:
            raise KrEndToEndHandoffManifestVerifyError("validate", "structured plan recorded json_status must be null")
        if entry["json_stage"] is not None:
            raise KrEndToEndHandoffManifestVerifyError("validate", "structured plan recorded json_stage must be null")
        actual_metadata = _actual_structured_plan_metadata(payload)
    elif role == "validation_report":
        actual_metadata = _actual_validation_report_metadata(payload)
    else:
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact role not recognized")

    _compare_recorded_json_metadata(entry, *actual_metadata)


def _validate_manifest_schema(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """handoff manifest top-level schema와 artifact entry schema를 검증한다."""
    missing = _TOP_LEVEL_KEYS - set(payload.keys())
    if missing:
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest missing required fields")

    if payload.get("version") != 1:
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest version must be exactly 1")

    if payload.get("mode") != _MANIFEST_MODE:
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest mode mismatch")

    if payload.get("status") != "ok":
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest status must be ok")

    if payload.get("stage") != "complete":
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest stage must be complete")

    if payload.get("generated_by") != _GENERATED_BY:
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest generated_by mismatch")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest artifacts must be a non-empty list")

    artifacts_count = payload.get("artifacts_count")
    if not isinstance(artifacts_count, int) or isinstance(artifacts_count, bool):
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest artifacts_count must be an integer")
    if artifacts_count != len(artifacts):
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest artifacts_count mismatch")

    if payload.get("all_artifacts_present") is not True:
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest all_artifacts_present must be true")

    if payload.get("commands_execute_in_builder") is not False:
        raise KrEndToEndHandoffManifestVerifyError(
            "validate",
            "handoff manifest commands_execute_in_builder must be false",
        )

    if payload.get("review_only") is not True:
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest review_only must be true")

    validated_entries: list[dict[str, Any]] = []
    roles: list[str] = []
    for index, raw_entry in enumerate(artifacts):
        validated = _validate_artifact_entry_schema(raw_entry, index=index)
        validated_entries.append(validated)
        roles.append(validated["role"])

    _validate_artifact_roles(roles)
    return validated_entries


def verify_kr_end_to_end_handoff_manifest(path: Path) -> dict[str, Any]:
    """handoff manifest JSON을 로드·검증하고 성공 summary dict를 반환한다."""
    manifest_path = path.resolve()
    payload = load_handoff_manifest(manifest_path)
    validated_entries = _validate_manifest_schema(payload)

    for entry in validated_entries:
        _verify_artifact_entry(entry)

    return _build_success_payload(manifest_path, artifacts_count=len(validated_entries))


def _build_success_payload(manifest_path: Path, *, artifacts_count: int) -> dict[str, Any]:
    """CLI/API 성공 JSON payload를 구성한다."""
    return {
        "status": "ok",
        "stage": "complete",
        "mode": _MODE,
        "manifest": str(manifest_path),
        "artifacts_count": artifacts_count,
        "verified_artifacts_count": artifacts_count,
        "hashes_verified": True,
        "metadata_verified": True,
        "commands_execute_in_verifier": False,
        "review_only": True,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KR end-to-end handoff manifest verifier — read-only integrity/metadata audit.",
    )
    parser.add_argument("--manifest", required=True, help="handoff manifest JSON path")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _emit_json(payload: dict[str, Any], *, stream: TextIO) -> None:
    json.dump(payload, stream, indent=2, ensure_ascii=False)
    stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not str(args.manifest).strip():
        error_payload = {
            "status": "error",
            "stage": "args",
            "message": "manifest path is required",
            "mode": _MODE,
        }
        if args.json:
            _emit_json(error_payload, stream=sys.stdout)
        else:
            print(error_payload["message"], file=sys.stderr)
        return 1

    manifest_path = Path(args.manifest)

    try:
        payload = verify_kr_end_to_end_handoff_manifest(manifest_path)
    except KrEndToEndHandoffManifestVerifyError as exc:
        error_payload = {"status": "error", "stage": exc.stage, "message": exc.message, "mode": _MODE}
        if args.json:
            _emit_json(error_payload, stream=sys.stdout)
        else:
            print(exc.message, file=sys.stderr)
        return 1

    if args.json:
        _emit_json(payload, stream=sys.stdout)
    else:
        print(f"handoff manifest verification: ok ({payload['stage']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
