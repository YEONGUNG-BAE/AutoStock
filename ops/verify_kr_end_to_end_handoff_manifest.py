#!/usr/bin/env python3
"""KR end-to-end operator handoff manifest verifier (3H8/3H9/3H10/3H11).

3H7 handoff manifest JSON → schema/integrity/metadata 재검증만 수행.
3H9: top-level·artifact entry exact-key schema lock(unknown key 거부).
3H10: optional --base-dir path containment(해석된 canonical path만 비교).
3H11: optional --verification-report-out compact audit report(검증 성공 후에만 기록).
artifact/manifest mutation·명령 실행·live fetch/smoke·config mutation/trading 없음.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Literal, TextIO

StageName = Literal["args", "parse", "validate", "write", "complete"]

_MODE = "kr-end-to-end-handoff-manifest-verification"
_REPORT_MODE = "kr-end-to-end-handoff-manifest-verification-report"
_MANIFEST_MODE = "kr-end-to-end-handoff-manifest"
_GENERATED_BY = "ops/build_kr_end_to_end_handoff_manifest.py"
_REPORT_GENERATED_BY = "ops/verify_kr_end_to_end_handoff_manifest.py"

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


def _resolve_base_dir(base_dir: Path | None) -> Path | None:
    """base_dir가 주어지면 resolve 후 존재·디렉터리 여부를 검증한다."""
    if base_dir is None:
        return None
    if not str(base_dir).strip():
        raise KrEndToEndHandoffManifestVerifyError("args", "base directory path is required")
    resolved = base_dir.resolve()
    if not resolved.exists():
        raise KrEndToEndHandoffManifestVerifyError("validate", "base directory not found")
    if not resolved.is_dir():
        raise KrEndToEndHandoffManifestVerifyError("validate", "base directory is not a directory")
    return resolved


def _assert_path_within_base(path: Path, base_dir: Path, *, field_name: str) -> None:
    """해석된 canonical path만으로 base_dir 내부 포함 여부를 검증한다."""
    resolved_child = path.resolve()
    resolved_base = base_dir.resolve()
    if not resolved_child.is_relative_to(resolved_base):
        raise KrEndToEndHandoffManifestVerifyError(
            "validate",
            f"{field_name} path escapes base directory",
        )


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

    unknown = set(entry.keys()) - _ARTIFACT_ENTRY_KEYS
    if unknown:
        raise KrEndToEndHandoffManifestVerifyError("validate", "artifact entry contains unknown fields")

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


def _verify_artifact_entry(entry: dict[str, Any], *, base_dir: Path | None = None) -> None:
    """단일 artifact entry의 path/size/sha256 및 JSON metadata를 재검증한다."""
    artifact_path = Path(entry["path"])
    if base_dir is not None:
        _assert_path_within_base(artifact_path, base_dir, field_name=entry["role"])
    artifact_path = artifact_path.resolve()
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
    unknown = set(payload.keys()) - _TOP_LEVEL_KEYS
    if unknown:
        raise KrEndToEndHandoffManifestVerifyError("validate", "handoff manifest contains unknown top-level fields")

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


def _verify_handoff_manifest_with_entries(
    path: Path,
    *,
    base_dir: Path | None = None,
) -> tuple[dict[str, object], list[str], Path | None]:
    """handoff manifest를 검증하고 success summary·artifact role 목록·resolved base_dir를 반환한다."""
    manifest_path = path.resolve()
    resolved_base = _resolve_base_dir(base_dir)
    if resolved_base is not None:
        _assert_path_within_base(manifest_path, resolved_base, field_name="manifest")

    payload = load_handoff_manifest(manifest_path)
    validated_entries = _validate_manifest_schema(payload)

    for entry in validated_entries:
        _verify_artifact_entry(entry, base_dir=resolved_base)

    artifact_roles = [entry["role"] for entry in validated_entries]
    summary = _build_success_payload(
        manifest_path,
        artifacts_count=len(validated_entries),
        resolved_base=resolved_base,
    )
    return summary, artifact_roles, resolved_base


def verify_kr_end_to_end_handoff_manifest(
    path: Path,
    *,
    base_dir: Path | None = None,
) -> dict[str, object]:
    """handoff manifest JSON을 로드·검증하고 성공 summary dict를 반환한다(read-only)."""
    summary, _, _ = _verify_handoff_manifest_with_entries(path, base_dir=base_dir)
    return summary


def _build_success_payload(
    manifest_path: Path,
    *,
    artifacts_count: int,
    resolved_base: Path | None = None,
) -> dict[str, object]:
    """CLI/API 성공 JSON payload를 구성한다."""
    payload: dict[str, object] = {
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
    if resolved_base is not None:
        payload["base_dir"] = str(resolved_base)
        payload["path_containment_verified"] = True
    return payload


def _build_verification_report(
    summary: dict[str, object],
    artifact_roles: list[str],
    *,
    resolved_base: Path | None,
) -> dict[str, object]:
    """검증 성공 summary와 role 목록으로 compact audit report JSON을 구성한다."""
    return {
        "version": 1,
        "mode": _REPORT_MODE,
        "status": "ok",
        "stage": "complete",
        "generated_by": _REPORT_GENERATED_BY,
        "manifest": summary["manifest"],
        "base_dir": str(resolved_base) if resolved_base is not None else None,
        "path_containment_verified": resolved_base is not None,
        "artifacts_count": summary["artifacts_count"],
        "verified_artifacts_count": summary["verified_artifacts_count"],
        "hashes_verified": summary["hashes_verified"],
        "metadata_verified": summary["metadata_verified"],
        "schema_verified": True,
        "commands_execute_in_verifier": summary["commands_execute_in_verifier"],
        "review_only": summary["review_only"],
        "artifact_roles": artifact_roles,
    }


def _write_verification_report_output(path: Path, report: dict[str, Any], *, force: bool) -> None:
    """verification report JSON을 atomic replace로 기록한다."""
    report_out = path.resolve()
    if report_out.exists() and not force:
        raise KrEndToEndHandoffManifestVerifyError(
            "write",
            "output already exists: verification_report_out",
        )

    report_out.parent.mkdir(parents=True, exist_ok=True)
    temp_path = report_out.parent / f".tmp_handoff_verification_report_{uuid.uuid4().hex}.json"
    try:
        serialized = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        temp_path.write_text(serialized, encoding="utf-8")
        temp_path.replace(report_out)
    except OSError as exc:
        raise KrEndToEndHandoffManifestVerifyError(
            "write",
            f"output write failed: {type(exc).__name__}",
        ) from None
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def run_verify_kr_end_to_end_handoff_manifest(
    path: Path,
    *,
    base_dir: Path | None = None,
    verification_report_out: Path | None = None,
    force: bool = False,
) -> dict[str, object]:
    """handoff manifest를 검증하고, 선택적으로 verification report JSON을 기록한다."""
    summary, artifact_roles, resolved_base = _verify_handoff_manifest_with_entries(path, base_dir=base_dir)

    if verification_report_out is None:
        return summary

    report_path = verification_report_out.resolve()
    report = _build_verification_report(summary, artifact_roles, resolved_base=resolved_base)
    _write_verification_report_output(report_path, report, force=force)

    result = dict(summary)
    result["verification_report_out"] = str(report_path)
    result["verification_report_written"] = True
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KR end-to-end handoff manifest verifier — read-only integrity/metadata audit.",
    )
    parser.add_argument("--manifest", required=True, help="handoff manifest JSON path")
    parser.add_argument(
        "--base-dir",
        help="optional base directory; manifest and artifact paths must resolve within it",
    )
    parser.add_argument(
        "--verification-report-out",
        default=None,
        help="optional compact verification report JSON path (written only after successful verification)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing verification_report_out when supplied (no-op without --verification-report-out)",
    )
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
    base_dir: Path | None = None
    if args.base_dir is not None:
        if not str(args.base_dir).strip():
            error_payload = {
                "status": "error",
                "stage": "args",
                "message": "base directory path is required",
                "mode": _MODE,
            }
            if args.json:
                _emit_json(error_payload, stream=sys.stdout)
            else:
                print(error_payload["message"], file=sys.stderr)
            return 1
        base_dir = Path(args.base_dir)

    verification_report_out: Path | None = None
    if args.verification_report_out is not None:
        if not str(args.verification_report_out).strip():
            error_payload = {
                "status": "error",
                "stage": "args",
                "message": "verification report output path is required",
                "mode": _MODE,
            }
            if args.json:
                _emit_json(error_payload, stream=sys.stdout)
            else:
                print(error_payload["message"], file=sys.stderr)
            return 1
        verification_report_out = Path(args.verification_report_out)

    try:
        payload = run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            base_dir=base_dir,
            verification_report_out=verification_report_out,
            force=bool(args.force),
        )
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
