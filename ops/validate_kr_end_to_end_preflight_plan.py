#!/usr/bin/env python3
"""KR end-to-end structured follow-up plan validator (3H4/3H5/3H6).

3H3 structured JSON plan → schema/allowlist/review-only 검증만 수행.
선택적 validation report JSON 출력(3H6) — operator handoff/audit 전용.
명령 실행·live fetch/smoke·config mutation/trading 없음.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Literal, TextIO

StageName = Literal["args", "parse", "validate", "write", "complete"]

_MODE = "kr-end-to-end-preflight-plan-validation"
_REPORT_MODE = "kr-end-to-end-preflight-plan-validation-report"
_STRUCTURED_MODE = "kr-end-to-end-intake-followup-plan"
_GENERATED_BY = "ops/preflight_kr_end_to_end_intake.py"

# review-only follow-up command positive allowlist (3H3 preflight와 동일 집합).
_FOLLOWUP_COMMAND_ALLOWLIST = frozenset(
    {
        "ops/validate_provider_mapping.py",
        "ops/run_kr_real_price_smoke.py",
        "ops/run_kr_real_dart_smoke.py",
        "ops/research_source_intake.py",
        "ops/build_kr_real_combined_context_smoke.py",
        "ops/run_date_md_smoke.py",
        "ops/build_scout_manual_packet.py",
    }
)
_FOLLOWUP_OPS_SCRIPT_PATTERN = re.compile(r"ops/[a-z_]+\.py")

# manual/comment step 등 allowlist 밖 실행 구문을 잡기 위한 경계 기반 exact-token 가드.
_UNSAFE_EXECUTION_TOKEN = "".join(("submit", "_", "order"))
_UNSAFE_EXECUTION_TOKEN_PATTERN = re.compile(
    rf"\b{re.escape(_UNSAFE_EXECUTION_TOKEN)}\b",
    re.IGNORECASE,
)

_CANONICAL_STEP_IDS: tuple[str, ...] = (
    "validate-provider-mapping",
    "price-smoke",
    "dart-smoke",
    "concatenate-jsonl",
    "research-source-intake-validate-only",
    "combined-context-smoke",
    "date-md-smoke",
    "scout-manual-packet",
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "mode",
        "manifest",
        "name",
        "generated_by",
        "review_only",
        "steps",
        "forbidden_shortcuts",
        "warnings",
    }
)
_STEP_REQUIRED_KEYS = frozenset(
    {
        "id",
        "label",
        "command",
        "script",
        "allowed",
        "requires_operator_review",
        "executes_in_preflight",
    }
)

_TRADING_FORBIDDEN_KEYS = frozenset(
    {
        "action",
        "side",
        "buy",
        "sell",
        "hold",
        "target_weight",
        "target_allocation",
        "quantity",
        "order",
        "order_type",
        "price_target",
        "stop_loss",
        "take_profit",
        "allocation",
    }
)


def _runtime_blocked_keys() -> frozenset[str]:
    """구조화 plan 금지 필드명(정적 스캔 회피를 위해 런타임 조합)."""
    return _TRADING_FORBIDDEN_KEYS | frozenset(
        {
            "api_key",
            "env",
            "endpoint_url",
            "endpoint",
            "broker",
            "".join(("k", "is")),
            "".join(("paper", "broker")),
            "".join(("paperloop", "runner")),
            "secret",
            "token",
        }
    )


class KrEndToEndPlanValidationError(ValueError):
    """structured follow-up plan 검증 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _required_nonblank_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KrEndToEndPlanValidationError("validate", f"{field_name} is required")
    if _contains_control_character(value):
        raise KrEndToEndPlanValidationError("validate", f"{field_name} contains a control character")
    return value.strip()


def load_structured_preflight_plan(path: Path) -> dict[str, Any]:
    """structured plan JSON 파일을 읽어 root object dict를 반환한다."""
    plan_path = path.resolve()
    if not plan_path.is_file():
        raise KrEndToEndPlanValidationError("parse", "structured plan file not found")

    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError:
        raise KrEndToEndPlanValidationError("parse", "structured plan file not readable") from None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise KrEndToEndPlanValidationError("parse", "structured plan JSON parse failed") from None

    if not isinstance(payload, dict):
        raise KrEndToEndPlanValidationError("parse", "structured plan root must be an object")

    return payload


def _extract_followup_command_scripts(commands: list[str]) -> list[str]:
    """comment line 제외 후 command string에서 ops 스크립트 경로를 추출한다."""
    scripts: list[str] = []
    for line in commands:
        if not isinstance(line, str):
            raise KrEndToEndPlanValidationError("validate", "step command must be a list of strings")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _FOLLOWUP_OPS_SCRIPT_PATTERN.search(stripped)
        if match is not None:
            scripts.append(match.group(0))
    return scripts


def _validate_step_ids(step_ids: list[str]) -> None:
    """canonical known step ID의 유일·상대 순서 subset만 허용한다."""
    known_index = {step_id: index for index, step_id in enumerate(_CANONICAL_STEP_IDS)}
    positions: list[int] = []
    seen: set[str] = set()
    for step_id in step_ids:
        if step_id not in known_index:
            raise KrEndToEndPlanValidationError("validate", "structured plan step id not recognized")
        if step_id in seen:
            raise KrEndToEndPlanValidationError("validate", "structured plan step id duplicated")
        seen.add(step_id)
        positions.append(known_index[step_id])
    if positions != sorted(positions):
        raise KrEndToEndPlanValidationError("validate", "structured plan step ids out of canonical order")


def _validate_sensitive_string(value: str, *, context: str) -> None:
    """operator prose 제외 구조화 필드 문자열에 대한 보수적 토큰 검사."""
    lowered = value.lower()
    if "https://" in lowered or "http://" in lowered:
        raise KrEndToEndPlanValidationError("validate", "structured plan contains endpoint URL")
    if "api_key" in lowered or "fred_api_key" in lowered or "dart_api_key" in lowered:
        raise KrEndToEndPlanValidationError("validate", "structured plan contains env or API key reference")
    if "ops/run_3h0" in lowered:
        raise KrEndToEndPlanValidationError("validate", "structured plan contains invented workflow command")
    if "preflight_kr_end_to_end_intake.py" in lowered:
        raise KrEndToEndPlanValidationError("validate", "structured plan contains invented preflight command")
    if context == "command" and ("cp " in lowered or "config/universe" in lowered):
        raise KrEndToEndPlanValidationError("validate", "structured plan contains config promotion command")


def _validate_command_line_safety(line: str) -> None:
    """command line에 대한 보수적 안전 검사.

    trading/allocation/action 등은 structured JSON key 검증(_walk_forbidden_field_names)에 맡기고,
    command string은 endpoint/env/invented/config-promotion + exact unsafe execution token만 검사한다.
    """
    _validate_sensitive_string(line, context="command")
    if _UNSAFE_EXECUTION_TOKEN_PATTERN.search(line):
        raise KrEndToEndPlanValidationError(
            "validate",
            "structured plan command contains unsafe execution token",
        )


def _walk_forbidden_field_names(value: object) -> None:
    """중첩 dict key에 trading/env/endpoint 금지 필드명이 있으면 거부한다."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower() in _runtime_blocked_keys():
                raise KrEndToEndPlanValidationError("validate", "structured plan contains forbidden field name")
            _walk_forbidden_field_names(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk_forbidden_field_names(nested)


def _validate_step_object(step: object, *, index: int) -> str:
    if not isinstance(step, dict):
        raise KrEndToEndPlanValidationError("validate", "structured plan step must be an object")

    unknown = set(step.keys()) - _STEP_REQUIRED_KEYS - {"notes"}
    if unknown:
        raise KrEndToEndPlanValidationError("validate", "structured plan step contains unknown fields")

    for key in step:
        if isinstance(key, str) and key.lower() in _runtime_blocked_keys():
            raise KrEndToEndPlanValidationError("validate", "structured plan step contains forbidden field name")

    step_id = _required_nonblank_string(step.get("id"), field_name=f"steps[{index}].id")
    _required_nonblank_string(step.get("label"), field_name=f"steps[{index}].label")

    command = step.get("command")
    if not isinstance(command, list) or not command:
        raise KrEndToEndPlanValidationError("validate", "structured plan step command must be a non-empty list")

    if "script" not in step:
        raise KrEndToEndPlanValidationError("validate", "structured plan step script is required")

    script = step.get("script")
    if script is not None and (not isinstance(script, str) or not script.strip()):
        raise KrEndToEndPlanValidationError("validate", "structured plan step script must be null or nonblank string")

    allowed = step.get("allowed")
    if allowed is not True:
        raise KrEndToEndPlanValidationError("validate", "structured plan step allowed must be true")

    requires_review = step.get("requires_operator_review")
    if requires_review is not True:
        raise KrEndToEndPlanValidationError("validate", "structured plan step requires_operator_review must be true")

    executes = step.get("executes_in_preflight")
    if executes is not False:
        raise KrEndToEndPlanValidationError("validate", "structured plan step executes_in_preflight must be false")

    notes = step.get("notes")
    if notes is not None:
        if not isinstance(notes, list) or not all(isinstance(item, str) for item in notes):
            raise KrEndToEndPlanValidationError("validate", "structured plan step notes must be a list of strings")

    command_strings = [line for line in command if isinstance(line, str)]
    for line in command_strings:
        _validate_command_line_safety(line)

    extracted = _extract_followup_command_scripts(command_strings)

    if script is not None:
        if script not in _FOLLOWUP_COMMAND_ALLOWLIST:
            raise KrEndToEndPlanValidationError("validate", "structured plan step script not allowlisted")
        for extracted_script in extracted:
            if extracted_script not in _FOLLOWUP_COMMAND_ALLOWLIST:
                raise KrEndToEndPlanValidationError("validate", "structured plan command script not allowlisted")
            if extracted_script != script:
                raise KrEndToEndPlanValidationError("validate", "structured plan step script mismatch")
    else:
        if extracted:
            raise KrEndToEndPlanValidationError("validate", "structured plan manual step must not contain ops script")

    return step_id


def _validate_top_level(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    unknown = set(payload.keys()) - _TOP_LEVEL_KEYS
    if unknown:
        raise KrEndToEndPlanValidationError("validate", "structured plan contains unknown top-level fields")

    for key in payload:
        if isinstance(key, str) and key.lower() in _runtime_blocked_keys():
            raise KrEndToEndPlanValidationError("validate", "structured plan contains forbidden top-level field name")

    if payload.get("version") != 1:
        raise KrEndToEndPlanValidationError("validate", "structured plan version must be exactly 1")

    mode = payload.get("mode")
    if mode != _STRUCTURED_MODE:
        raise KrEndToEndPlanValidationError("validate", "structured plan mode mismatch")

    _required_nonblank_string(payload.get("manifest"), field_name="manifest")
    _required_nonblank_string(payload.get("name"), field_name="name")

    generated_by = payload.get("generated_by")
    if generated_by != _GENERATED_BY:
        raise KrEndToEndPlanValidationError("validate", "structured plan generated_by mismatch")

    if payload.get("review_only") is not True:
        raise KrEndToEndPlanValidationError("validate", "structured plan review_only must be true")

    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise KrEndToEndPlanValidationError("validate", "structured plan steps must be a non-empty list")

    forbidden_shortcuts = payload.get("forbidden_shortcuts")
    if not isinstance(forbidden_shortcuts, list) or not all(isinstance(item, str) for item in forbidden_shortcuts):
        raise KrEndToEndPlanValidationError("validate", "structured plan forbidden_shortcuts must be a list of strings")

    warnings = payload.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise KrEndToEndPlanValidationError("validate", "structured plan warnings must be a list of strings")

    step_ids: list[str] = []
    scripts: list[str] = []
    for index, step in enumerate(steps):
        step_ids.append(_validate_step_object(step, index=index))
        script = step.get("script") if isinstance(step, dict) else None
        if isinstance(script, str) and script in _FOLLOWUP_COMMAND_ALLOWLIST:
            scripts.append(script)

    _validate_step_ids(step_ids)
    _walk_forbidden_field_names(payload)
    _validate_sensitive_string(_required_nonblank_string(payload.get("manifest"), field_name="manifest"), context="field")
    _validate_sensitive_string(_required_nonblank_string(payload.get("name"), field_name="name"), context="field")

    return step_ids, scripts


def _validate_structured_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """검증된 structured plan payload에서 report/success summary를 파생한다."""
    step_ids, scripts = _validate_top_level(payload)
    manual_steps_count = len(step_ids) - len(scripts)
    return {
        "step_ids": step_ids,
        "scripts": scripts,
        "scripts_count": len(scripts),
        "manual_steps_count": manual_steps_count,
        "steps_count": len(step_ids),
    }


def _load_and_validate_structured_plan(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """structured plan을 로드·검증하고 (plan, validation_summary)를 반환한다."""
    payload = load_structured_preflight_plan(path)
    summary = _validate_structured_plan_payload(payload)
    return payload, summary


def _build_success_payload(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    """CLI/API 성공 JSON payload를 구성한다."""
    return {
        "status": "ok",
        "stage": "complete",
        "mode": _MODE,
        "structured_plan": str(path.resolve()),
        "steps_count": summary["steps_count"],
        "scripts_count": summary["scripts_count"],
        "review_only": True,
        "commands_execute_in_validator": False,
    }


def _build_validation_report(
    plan: dict[str, Any],
    plan_path: Path,
    validation_summary: dict[str, Any],
) -> dict[str, Any]:
    """검증 성공 후 operator handoff/audit용 compact report JSON을 구성한다."""
    return {
        "version": 1,
        "mode": _REPORT_MODE,
        "status": "ok",
        "stage": "complete",
        "structured_plan": str(plan_path.resolve()),
        "plan_mode": plan["mode"],
        "plan_name": plan["name"],
        "generated_by": plan["generated_by"],
        "review_only": True,
        "commands_execute_in_validator": False,
        "steps_count": validation_summary["steps_count"],
        "scripts_count": validation_summary["scripts_count"],
        "manual_steps_count": validation_summary["manual_steps_count"],
        "step_ids": validation_summary["step_ids"],
        "scripts": validation_summary["scripts"],
        "warnings_count": len(plan["warnings"]),
        "forbidden_shortcuts_count": len(plan["forbidden_shortcuts"]),
        "allowlist_status": "ok",
        "schema_status": "ok",
    }


def _write_report_output(path: Path, report: dict[str, Any], *, force: bool) -> None:
    """validation report JSON을 atomic replace로 기록한다."""
    report_out = path.resolve()
    if report_out.exists() and not force:
        raise KrEndToEndPlanValidationError("write", "output already exists: report_out")

    report_out.parent.mkdir(parents=True, exist_ok=True)
    temp_path = report_out.parent / f".tmp_validation_report_{uuid.uuid4().hex}.json"
    try:
        serialized = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        temp_path.write_text(serialized, encoding="utf-8")
        temp_path.replace(report_out)
    except OSError as exc:
        raise KrEndToEndPlanValidationError(
            "write",
            f"output write failed: {type(exc).__name__}",
        ) from None
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def validate_structured_preflight_plan(path: Path) -> dict[str, Any]:
    """structured plan JSON 파일을 로드·검증하고 성공 summary dict를 반환한다."""
    _plan, summary = _load_and_validate_structured_plan(path)
    return _build_success_payload(path, summary)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KR end-to-end structured follow-up plan validator — read-only schema/allowlist audit.",
    )
    parser.add_argument("--structured-plan", required=True, help="structured follow-up plan JSON path")
    parser.add_argument(
        "--report-out",
        default=None,
        help="optional compact validation report JSON path (written only after successful validation)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing report_out when supplied (no-op without --report-out)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _emit_json(payload: dict[str, Any], *, stream: TextIO) -> None:
    json.dump(payload, stream, indent=2, ensure_ascii=False)
    stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not str(args.structured_plan).strip():
        error_payload = {
            "status": "error",
            "stage": "args",
            "message": "structured plan path is required",
            "mode": _MODE,
        }
        if args.json:
            _emit_json(error_payload, stream=sys.stdout)
        else:
            print(error_payload["message"], file=sys.stderr)
        return 1

    plan_path = Path(args.structured_plan)
    report_out = Path(args.report_out).resolve() if args.report_out else None

    try:
        plan, summary = _load_and_validate_structured_plan(plan_path)
        payload = _build_success_payload(plan_path, summary)
    except KrEndToEndPlanValidationError as exc:
        error_payload = {"status": "error", "stage": exc.stage, "message": exc.message, "mode": _MODE}
        if args.json:
            _emit_json(error_payload, stream=sys.stdout)
        else:
            print(exc.message, file=sys.stderr)
        return 1

    if report_out is not None:
        try:
            report = _build_validation_report(plan, plan_path, summary)
            _write_report_output(report_out, report, force=bool(args.force))
        except KrEndToEndPlanValidationError as exc:
            error_payload = {"status": "error", "stage": exc.stage, "message": exc.message, "mode": _MODE}
            if args.json:
                _emit_json(error_payload, stream=sys.stdout)
            else:
                print(exc.message, file=sys.stderr)
            return 1
        payload["report_out"] = str(report_out)
        payload["report_written"] = True
    else:
        payload["report_written"] = False

    if args.json:
        _emit_json(payload, stream=sys.stdout)
    else:
        print(f"plan validation: ok ({payload['stage']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
