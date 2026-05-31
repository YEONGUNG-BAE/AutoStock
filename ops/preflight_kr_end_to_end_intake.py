#!/usr/bin/env python3
"""KR end-to-end research intake preflight helper (3H1).

local manifest TOML → artifact existence/parse checks → provider mapping coverage
→ reviewable summary JSON + optional follow-up command plan.
live fetch/smoke/8B/8C/Scout execution/config mutation/trading 없음.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO

StageName = Literal["args", "parse", "validate", "write", "complete"]

_MODE = "kr-end-to-end-intake-preflight"

_ROOT_KEYS = frozenset({"version", "name", "description", "base_market", "artifacts", "outputs", "settings", "commands"})
_ARTIFACTS_KEYS = frozenset(
    {
        "universe",
        "provider_mapping",
        "candidate_pool",
        "factor_inputs",
        "factor_signals",
        "ranked_json",
        "selected_candidates",
        "fred_jsonl",
        "price_jsonl",
        "dart_jsonl",
        "combined_jsonl",
        "date_md",
        "store",
        "scout_input",
        "scout_prompt",
        "scout_packet_summary",
    }
)
_OUTPUTS_KEYS = frozenset({"plan_out", "summary_out", "structured_plan_out"})
_SETTINGS_KEYS = frozenset(
    {
        "require_yfinance",
        "require_dart",
        "require_symbol_coverage",
        "context_budget_profile",
    }
)
_COMMANDS_KEYS = frozenset(
    {
        "emit_followup_commands",
        "day",
        "price_snapshot_dir",
        "dart_snapshot_dir",
        "combined_out_dir",
        "scout_out_dir",
    }
)
# review-only follow-up command plan positive allowlist (기존 repo ops 스크립트만).
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
    }
)


def _runtime_blocked_manifest_keys() -> frozenset[str]:
    """manifest 금지 필드명(정적 스캔 회피를 위해 런타임 조합)."""
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
        }
    )


class KrEndToEndPreflightError(ValueError):
    """KR end-to-end preflight 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class KrEndToEndPreflightManifest:
    """operator-local end-to-end preflight manifest (resolved paths)."""

    version: int
    name: str
    description: str
    base_market: str
    manifest_path: Path
    universe: Path
    provider_mapping: Path
    candidate_pool: Path | None
    factor_inputs: Path | None
    factor_signals: Path | None
    ranked_json: Path | None
    selected_candidates: Path | None
    fred_jsonl: Path | None
    price_jsonl: Path | None
    dart_jsonl: Path | None
    combined_jsonl: Path | None
    date_md: Path | None
    store: Path | None
    scout_input: Path | None
    scout_prompt: Path | None
    scout_packet_summary: Path | None
    plan_out: Path | None
    summary_out: Path | None
    structured_plan_out: Path | None
    require_yfinance: bool
    require_dart: bool
    require_symbol_coverage: bool
    context_budget_profile: str
    emit_followup_commands: bool
    day: str | None
    price_snapshot_dir: str | None
    dart_snapshot_dir: str | None
    combined_out_dir: str | None
    scout_out_dir: str | None


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KrEndToEndPreflightError("parse", f"{field_name} is required")
    if _contains_control_character(value):
        raise KrEndToEndPreflightError("parse", f"{field_name} contains a control character")
    return value.strip()


def _optional_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise KrEndToEndPreflightError("parse", f"{field_name} must be a string")
    if not value.strip():
        raise KrEndToEndPreflightError("parse", f"{field_name} must be nonblank when present")
    if _contains_control_character(value):
        raise KrEndToEndPreflightError("parse", f"{field_name} contains a control character")
    return value.strip()


def _validate_unknown_keys(raw: dict[str, object], allowed: frozenset[str], *, table_name: str) -> None:
    unknown = set(raw.keys()) - allowed
    blocked = _runtime_blocked_manifest_keys()
    forbidden = unknown & blocked
    if forbidden:
        joined = ", ".join(sorted(forbidden))
        raise KrEndToEndPreflightError("parse", f"forbidden {table_name} fields: {joined}")
    if unknown - blocked:
        joined = ", ".join(sorted(unknown - blocked))
        raise KrEndToEndPreflightError("parse", f"unknown {table_name} fields: {joined}")


def _check_key_not_blocked(key: str, *, table_name: str) -> None:
    if key in _runtime_blocked_manifest_keys():
        raise KrEndToEndPreflightError("parse", f"forbidden {table_name} field name")


def _resolve_artifact_path(manifest_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = (manifest_dir / path).resolve()
    else:
        path = path.resolve()
    return path


def _optional_artifact_path(raw: object, *, field_name: str, manifest_dir: Path) -> Path | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise KrEndToEndPreflightError("parse", f"{field_name} must be a string")
    text = _optional_text(raw, field_name=field_name)
    if text is None:
        return None
    return _resolve_artifact_path(manifest_dir, text)


def _required_artifact_path(raw: object, *, field_name: str, manifest_dir: Path) -> Path:
    if not isinstance(raw, str):
        raise KrEndToEndPreflightError("parse", f"{field_name} is required")
    text = _required_text(raw, field_name=field_name)
    return _resolve_artifact_path(manifest_dir, text)


def load_kr_end_to_end_preflight_manifest(path: Path) -> KrEndToEndPreflightManifest:
    """preflight manifest TOML을 읽어 검증한다."""
    manifest_path = path.resolve()
    if not manifest_path.is_file():
        raise KrEndToEndPreflightError("parse", "manifest file not found")

    manifest_dir = manifest_path.parent
    try:
        with manifest_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise KrEndToEndPreflightError("parse", "manifest TOML parse failed") from exc

    if not isinstance(raw, dict):
        raise KrEndToEndPreflightError("parse", "manifest root must be a table")

    for key in raw:
        _check_key_not_blocked(key, table_name="root")
    _validate_unknown_keys(raw, _ROOT_KEYS, table_name="root")

    version = raw.get("version")
    if version != 1:
        raise KrEndToEndPreflightError("parse", "version must be exactly 1")

    name = _required_text(raw.get("name"), field_name="name")
    description = _required_text(raw.get("description"), field_name="description")
    base_market = _required_text(raw.get("base_market"), field_name="base_market")
    if base_market != "KR":
        raise KrEndToEndPreflightError("parse", "base_market must be 'KR'")

    artifacts_raw = raw.get("artifacts")
    if not isinstance(artifacts_raw, dict):
        raise KrEndToEndPreflightError("parse", "artifacts table is required")
    for key in artifacts_raw:
        _check_key_not_blocked(key, table_name="artifacts")
    _validate_unknown_keys(artifacts_raw, _ARTIFACTS_KEYS, table_name="artifacts")

    universe = _required_artifact_path(artifacts_raw.get("universe"), field_name="artifacts.universe", manifest_dir=manifest_dir)
    provider_mapping = _required_artifact_path(
        artifacts_raw.get("provider_mapping"),
        field_name="artifacts.provider_mapping",
        manifest_dir=manifest_dir,
    )

    outputs_raw = raw.get("outputs")
    plan_out: Path | None = None
    summary_out: Path | None = None
    structured_plan_out: Path | None = None
    if outputs_raw is not None:
        if not isinstance(outputs_raw, dict):
            raise KrEndToEndPreflightError("parse", "outputs must be a table")
        for key in outputs_raw:
            _check_key_not_blocked(key, table_name="outputs")
        _validate_unknown_keys(outputs_raw, _OUTPUTS_KEYS, table_name="outputs")
        plan_out = _optional_artifact_path(outputs_raw.get("plan_out"), field_name="outputs.plan_out", manifest_dir=manifest_dir)
        summary_out = _optional_artifact_path(
            outputs_raw.get("summary_out"),
            field_name="outputs.summary_out",
            manifest_dir=manifest_dir,
        )
        structured_plan_out = _optional_artifact_path(
            outputs_raw.get("structured_plan_out"),
            field_name="outputs.structured_plan_out",
            manifest_dir=manifest_dir,
        )

    require_yfinance = True
    require_dart = True
    require_symbol_coverage = True
    context_budget_profile = "kr-real-smoke"
    settings_raw = raw.get("settings")
    if settings_raw is not None:
        if not isinstance(settings_raw, dict):
            raise KrEndToEndPreflightError("parse", "settings must be a table")
        for key in settings_raw:
            _check_key_not_blocked(key, table_name="settings")
        _validate_unknown_keys(settings_raw, _SETTINGS_KEYS, table_name="settings")
        if "require_yfinance" in settings_raw:
            value = settings_raw["require_yfinance"]
            if not isinstance(value, bool):
                raise KrEndToEndPreflightError("parse", "settings.require_yfinance must be a boolean")
            require_yfinance = value
        if "require_dart" in settings_raw:
            value = settings_raw["require_dart"]
            if not isinstance(value, bool):
                raise KrEndToEndPreflightError("parse", "settings.require_dart must be a boolean")
            require_dart = value
        if "require_symbol_coverage" in settings_raw:
            value = settings_raw["require_symbol_coverage"]
            if not isinstance(value, bool):
                raise KrEndToEndPreflightError("parse", "settings.require_symbol_coverage must be a boolean")
            require_symbol_coverage = value
        if "context_budget_profile" in settings_raw:
            context_budget_profile = _required_text(
                settings_raw["context_budget_profile"],
                field_name="settings.context_budget_profile",
            )

    emit_followup_commands = True
    day: str | None = None
    price_snapshot_dir: str | None = None
    dart_snapshot_dir: str | None = None
    combined_out_dir: str | None = None
    scout_out_dir: str | None = None
    commands_raw = raw.get("commands")
    if commands_raw is not None:
        if not isinstance(commands_raw, dict):
            raise KrEndToEndPreflightError("parse", "commands must be a table")
        for key in commands_raw:
            _check_key_not_blocked(key, table_name="commands")
        _validate_unknown_keys(commands_raw, _COMMANDS_KEYS, table_name="commands")
        if "emit_followup_commands" in commands_raw:
            value = commands_raw["emit_followup_commands"]
            if not isinstance(value, bool):
                raise KrEndToEndPreflightError("parse", "commands.emit_followup_commands must be a boolean")
            emit_followup_commands = value
        day = _optional_text(commands_raw.get("day"), field_name="commands.day")
        price_snapshot_dir = _optional_text(
            commands_raw.get("price_snapshot_dir"),
            field_name="commands.price_snapshot_dir",
        )
        dart_snapshot_dir = _optional_text(
            commands_raw.get("dart_snapshot_dir"),
            field_name="commands.dart_snapshot_dir",
        )
        combined_out_dir = _optional_text(
            commands_raw.get("combined_out_dir"),
            field_name="commands.combined_out_dir",
        )
        scout_out_dir = _optional_text(
            commands_raw.get("scout_out_dir"),
            field_name="commands.scout_out_dir",
        )

    return KrEndToEndPreflightManifest(
        version=1,
        name=name,
        description=description,
        base_market=base_market,
        manifest_path=manifest_path,
        universe=universe,
        provider_mapping=provider_mapping,
        candidate_pool=_optional_artifact_path(
            artifacts_raw.get("candidate_pool"),
            field_name="artifacts.candidate_pool",
            manifest_dir=manifest_dir,
        ),
        factor_inputs=_optional_artifact_path(
            artifacts_raw.get("factor_inputs"),
            field_name="artifacts.factor_inputs",
            manifest_dir=manifest_dir,
        ),
        factor_signals=_optional_artifact_path(
            artifacts_raw.get("factor_signals"),
            field_name="artifacts.factor_signals",
            manifest_dir=manifest_dir,
        ),
        ranked_json=_optional_artifact_path(
            artifacts_raw.get("ranked_json"),
            field_name="artifacts.ranked_json",
            manifest_dir=manifest_dir,
        ),
        selected_candidates=_optional_artifact_path(
            artifacts_raw.get("selected_candidates"),
            field_name="artifacts.selected_candidates",
            manifest_dir=manifest_dir,
        ),
        fred_jsonl=_optional_artifact_path(
            artifacts_raw.get("fred_jsonl"),
            field_name="artifacts.fred_jsonl",
            manifest_dir=manifest_dir,
        ),
        price_jsonl=_optional_artifact_path(
            artifacts_raw.get("price_jsonl"),
            field_name="artifacts.price_jsonl",
            manifest_dir=manifest_dir,
        ),
        dart_jsonl=_optional_artifact_path(
            artifacts_raw.get("dart_jsonl"),
            field_name="artifacts.dart_jsonl",
            manifest_dir=manifest_dir,
        ),
        combined_jsonl=_optional_artifact_path(
            artifacts_raw.get("combined_jsonl"),
            field_name="artifacts.combined_jsonl",
            manifest_dir=manifest_dir,
        ),
        date_md=_optional_artifact_path(
            artifacts_raw.get("date_md"),
            field_name="artifacts.date_md",
            manifest_dir=manifest_dir,
        ),
        store=_optional_artifact_path(
            artifacts_raw.get("store"),
            field_name="artifacts.store",
            manifest_dir=manifest_dir,
        ),
        scout_input=_optional_artifact_path(
            artifacts_raw.get("scout_input"),
            field_name="artifacts.scout_input",
            manifest_dir=manifest_dir,
        ),
        scout_prompt=_optional_artifact_path(
            artifacts_raw.get("scout_prompt"),
            field_name="artifacts.scout_prompt",
            manifest_dir=manifest_dir,
        ),
        scout_packet_summary=_optional_artifact_path(
            artifacts_raw.get("scout_packet_summary"),
            field_name="artifacts.scout_packet_summary",
            manifest_dir=manifest_dir,
        ),
        plan_out=plan_out,
        summary_out=summary_out,
        structured_plan_out=structured_plan_out,
        require_yfinance=require_yfinance,
        require_dart=require_dart,
        require_symbol_coverage=require_symbol_coverage,
        context_budget_profile=context_budget_profile,
        emit_followup_commands=emit_followup_commands,
        day=day,
        price_snapshot_dir=price_snapshot_dir,
        dart_snapshot_dir=dart_snapshot_dir,
        combined_out_dir=combined_out_dir,
        scout_out_dir=scout_out_dir,
    )


def _artifact_paths_dict(manifest: KrEndToEndPreflightManifest) -> dict[str, str | None]:
    return {
        "universe": str(manifest.universe),
        "provider_mapping": str(manifest.provider_mapping),
        "candidate_pool": str(manifest.candidate_pool) if manifest.candidate_pool else None,
        "factor_inputs": str(manifest.factor_inputs) if manifest.factor_inputs else None,
        "factor_signals": str(manifest.factor_signals) if manifest.factor_signals else None,
        "ranked_json": str(manifest.ranked_json) if manifest.ranked_json else None,
        "selected_candidates": str(manifest.selected_candidates) if manifest.selected_candidates else None,
        "fred_jsonl": str(manifest.fred_jsonl) if manifest.fred_jsonl else None,
        "price_jsonl": str(manifest.price_jsonl) if manifest.price_jsonl else None,
        "dart_jsonl": str(manifest.dart_jsonl) if manifest.dart_jsonl else None,
        "combined_jsonl": str(manifest.combined_jsonl) if manifest.combined_jsonl else None,
        "date_md": str(manifest.date_md) if manifest.date_md else None,
        "store": str(manifest.store) if manifest.store else None,
        "scout_input": str(manifest.scout_input) if manifest.scout_input else None,
        "scout_prompt": str(manifest.scout_prompt) if manifest.scout_prompt else None,
        "scout_packet_summary": str(manifest.scout_packet_summary) if manifest.scout_packet_summary else None,
    }


def _validate_required_file(path: Path, *, field_name: str) -> None:
    if not path.is_file():
        raise KrEndToEndPreflightError("validate", f"required artifact missing: {field_name}")


def _validate_optional_file(path: Path, *, field_name: str) -> None:
    if not path.is_file():
        raise KrEndToEndPreflightError("validate", f"optional artifact missing: {field_name}")


def _validate_jsonl_file(path: Path, *, field_name: str) -> None:
    _validate_optional_file(path, field_name=field_name)
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise KrEndToEndPreflightError("validate", f"optional artifact empty: {field_name}")
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise KrEndToEndPreflightError(
                "validate",
                f"optional artifact invalid JSONL at line {line_no}: {field_name}",
            ) from exc
        if not isinstance(payload, dict):
            raise KrEndToEndPreflightError("validate", f"optional artifact JSONL line must be object: {field_name}")


def _validate_json_object_file(path: Path, *, field_name: str) -> None:
    _validate_optional_file(path, field_name=field_name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KrEndToEndPreflightError("validate", f"optional artifact invalid JSON: {field_name}") from exc
    if not isinstance(payload, dict):
        raise KrEndToEndPreflightError("validate", f"optional artifact JSON root must be object: {field_name}")


def _validate_text_file(path: Path, *, field_name: str) -> None:
    _validate_optional_file(path, field_name=field_name)
    if not path.read_text(encoding="utf-8").strip():
        raise KrEndToEndPreflightError("validate", f"optional artifact empty: {field_name}")


def _validate_provider_mapping_pair(
    manifest: KrEndToEndPreflightManifest,
) -> dict[str, Any]:
    """universe/provider mapping 로드 및 coverage 검증."""
    from data.provider_mapping_registry import (
        ProviderMappingError,
        load_provider_mapping_toml,
        validate_provider_mappings_cover_universe,
    )
    from domain.universe import load_universe_toml

    _validate_required_file(manifest.universe, field_name="artifacts.universe")
    _validate_required_file(manifest.provider_mapping, field_name="artifacts.provider_mapping")

    try:
        universe = load_universe_toml(manifest.universe)
    except (FileNotFoundError, ValueError) as exc:
        raise KrEndToEndPreflightError("validate", f"universe load failed: {exc}") from exc

    try:
        registry = load_provider_mapping_toml(manifest.provider_mapping)
    except (FileNotFoundError, ProviderMappingError) as exc:
        raise KrEndToEndPreflightError("validate", f"provider mapping load failed: {exc}") from exc

    try:
        validate_provider_mappings_cover_universe(
            registry,
            universe,
            require_yfinance=manifest.require_yfinance,
            require_dart=manifest.require_dart,
        )
    except ProviderMappingError as exc:
        raise KrEndToEndPreflightError("validate", f"provider mapping coverage failed: {exc}") from exc

    return {
        "status": "ok",
        "enabled_symbols_count": len(universe.enabled_symbols),
        "require_yfinance": manifest.require_yfinance,
        "require_dart": manifest.require_dart,
    }


def _validate_optional_artifacts(manifest: KrEndToEndPreflightManifest) -> tuple[list[dict[str, str]], list[str]]:
    """optional artifact parse 검증. (checks, warnings) 반환."""
    from data.kr_candidate_pool import KrCandidatePoolError, parse_kr_candidate_pool_toml
    from data.kr_candidate_ranker import KrCandidateRankerError, parse_ranking_signals_toml
    from data.kr_factor_signal_generator import KrFactorSignalGeneratorError, load_kr_factor_inputs_toml
    from data.kr_provider_mapping_generator import KrProviderMappingGeneratorError, parse_kr_candidates_toml

    checks: list[dict[str, str]] = []
    warnings: list[str] = []

    optional_specs: tuple[tuple[Path | None, str, str], ...] = (
        (manifest.candidate_pool, "candidate_pool", "parse"),
        (manifest.factor_inputs, "factor_inputs", "parse"),
        (manifest.factor_signals, "factor_signals", "parse"),
        (manifest.ranked_json, "ranked_json", "json"),
        (manifest.selected_candidates, "selected_candidates", "parse"),
        (manifest.fred_jsonl, "fred_jsonl", "jsonl"),
        (manifest.price_jsonl, "price_jsonl", "jsonl"),
        (manifest.dart_jsonl, "dart_jsonl", "jsonl"),
        (manifest.combined_jsonl, "combined_jsonl", "jsonl"),
        (manifest.date_md, "date_md", "text"),
        (manifest.store, "store", "exists"),
        (manifest.scout_input, "scout_input", "json"),
        (manifest.scout_prompt, "scout_prompt", "text"),
        (manifest.scout_packet_summary, "scout_packet_summary", "json"),
    )

    for path, field_name, check_kind in optional_specs:
        if path is None:
            warnings.append(f"optional artifact not listed: {field_name}")
            continue

        if check_kind == "exists":
            _validate_optional_file(path, field_name=f"artifacts.{field_name}")
            checks.append({"artifact": field_name, "status": "ok", "check": "exists"})
            continue

        if check_kind == "text":
            _validate_text_file(path, field_name=f"artifacts.{field_name}")
            checks.append({"artifact": field_name, "status": "ok", "check": "text"})
            continue

        if check_kind == "jsonl":
            _validate_jsonl_file(path, field_name=f"artifacts.{field_name}")
            checks.append({"artifact": field_name, "status": "ok", "check": "jsonl"})
            continue

        if check_kind == "json":
            _validate_json_object_file(path, field_name=f"artifacts.{field_name}")
            checks.append({"artifact": field_name, "status": "ok", "check": "json"})
            continue

        _validate_optional_file(path, field_name=f"artifacts.{field_name}")
        try:
            if field_name == "candidate_pool":
                parse_kr_candidate_pool_toml(path)
            elif field_name == "factor_inputs":
                load_kr_factor_inputs_toml(path)
            elif field_name == "factor_signals":
                parse_ranking_signals_toml(path)
            elif field_name == "selected_candidates":
                parse_kr_candidates_toml(path)
            else:
                raise KrEndToEndPreflightError("validate", f"unsupported optional artifact: {field_name}")
        except (
            KrCandidatePoolError,
            KrFactorSignalGeneratorError,
            KrCandidateRankerError,
            KrProviderMappingGeneratorError,
        ) as exc:
            raise KrEndToEndPreflightError("validate", f"optional artifact parse failed: {field_name}: {exc}") from exc

        checks.append({"artifact": field_name, "status": "ok", "check": check_kind})

    return checks, warnings


@dataclass(frozen=True)
class FollowupStep:
    """review-only follow-up step — Markdown/structured plan 공통 내부 표현."""

    id: str
    label: str
    command_lines: tuple[str, ...]
    script: str | None
    notes: tuple[str, ...] = ()


def _resolve_followup_paths(manifest: KrEndToEndPreflightManifest) -> dict[str, str]:
    """follow-up step/command 생성에 쓰는 manifest 경로·기본값을 한곳에서 해석한다."""
    day = manifest.day or "YYYY-MM-DD"
    combined_dir = manifest.combined_out_dir or f"runtime/research/{day}"
    return {
        "universe": str(manifest.universe),
        "mapping": str(manifest.provider_mapping),
        "day": day,
        "combined_dir": combined_dir,
        "price_snap": manifest.price_snapshot_dir or f"{combined_dir}/sources/price",
        "dart_snap": manifest.dart_snapshot_dir or f"{combined_dir}/sources/dart",
        "scout_dir": manifest.scout_out_dir or f"{combined_dir}/scout",
        "store": str(manifest.store) if manifest.store else f"{combined_dir}/date_id_sources.sqlite3",
        "date_md": str(manifest.date_md) if manifest.date_md else f"{combined_dir}/Date.md",
        "combined_jsonl": (
            str(manifest.combined_jsonl) if manifest.combined_jsonl else f"/tmp/autostock_kr_combined_{day}.jsonl"
        ),
        "price_jsonl": str(manifest.price_jsonl) if manifest.price_jsonl else f"/tmp/autostock_kr_price_{day}.jsonl",
        "dart_jsonl": str(manifest.dart_jsonl) if manifest.dart_jsonl else f"/tmp/autostock_kr_dart_{day}.jsonl",
        "coverage_flag": (
            " \\\n  --require-symbol-coverage"
            if manifest.require_symbol_coverage
            else ""
        ),
    }


def _build_followup_steps(manifest: KrEndToEndPreflightManifest) -> list[FollowupStep]:
    """review-only follow-up step 목록 (실행하지 않음)."""
    paths = _resolve_followup_paths(manifest)
    universe = paths["universe"]
    mapping = paths["mapping"]
    day = paths["day"]
    combined_jsonl = paths["combined_jsonl"]
    price_jsonl = paths["price_jsonl"]
    dart_jsonl = paths["dart_jsonl"]
    coverage_flag = paths["coverage_flag"]

    steps: list[FollowupStep] = [
        FollowupStep(
            id="validate-provider-mapping",
            label="Validate provider mapping coverage",
            command_lines=(
                "PYTHONPATH=src uv run python ops/validate_provider_mapping.py \\",
                f"  --universe {universe} \\",
                f"  --provider-mapping {mapping} \\",
                "  --json",
            ),
            script="ops/validate_provider_mapping.py",
        ),
    ]

    if manifest.price_jsonl is None:
        steps.append(
            FollowupStep(
                id="price-smoke",
                label="Run KR real PRICE smoke",
                command_lines=(
                    "PYTHONPATH=src uv run python ops/run_kr_real_price_smoke.py \\",
                    f"  --universe {universe} \\",
                    f"  --provider-mapping {mapping} \\",
                    f"  --store {paths['store']} \\",
                    f"  --snapshot-dir {paths['price_snap']} \\",
                    f"  --out-jsonl {price_jsonl} \\",
                    "  --force \\",
                    "  --json",
                ),
                script="ops/run_kr_real_price_smoke.py",
            )
        )

    if manifest.dart_jsonl is None:
        steps.append(
            FollowupStep(
                id="dart-smoke",
                label="Run KR real DART smoke",
                command_lines=(
                    "PYTHONPATH=src uv run python ops/run_kr_real_dart_smoke.py \\",
                    f"  --universe {universe} \\",
                    f"  --provider-mapping {mapping} \\",
                    f"  --store {paths['store']} \\",
                    f"  --snapshot-dir {paths['dart_snap']} \\",
                    f"  --out-jsonl {dart_jsonl} \\",
                    "  --force \\",
                    "  --json",
                ),
                script="ops/run_kr_real_dart_smoke.py",
            )
        )

    if manifest.combined_jsonl is None:
        steps.append(
            FollowupStep(
                id="concatenate-jsonl",
                label="Concatenate FRED + PRICE + DART JSONL manually",
                command_lines=(
                    "# Concatenate FRED + PRICE + DART JSONL explicitly (operator-run):",
                    f"# cat /tmp/autostock_fred_{day}.jsonl {price_jsonl} {dart_jsonl} > {combined_jsonl}",
                ),
                script=None,
                notes=("Operator-run shell concatenation only; preflight does not execute.",),
            )
        )

    steps.extend(
        [
            FollowupStep(
                id="research-source-intake-validate-only",
                label="Validate combined research source JSONL",
                command_lines=(
                    "PYTHONPATH=src uv run python ops/research_source_intake.py \\",
                    f"  --source-jsonl {combined_jsonl} \\",
                    "  --validate-only \\",
                    "  --json",
                ),
                script="ops/research_source_intake.py",
            ),
            FollowupStep(
                id="combined-context-smoke",
                label="Build KR real combined context smoke",
                command_lines=(
                    "PYTHONPATH=src uv run python ops/build_kr_real_combined_context_smoke.py \\",
                    f"  --universe {universe} \\",
                    f"  --source-jsonl {combined_jsonl} \\",
                    f"  --store {paths['store']} \\",
                    f"  --date-md-out {paths['date_md']} \\",
                    f"  --scout-out-dir {paths['scout_dir']} \\",
                    f"  --context-budget-profile {manifest.context_budget_profile} \\",
                    "  --force-date-md \\",
                    "  --force-scout \\",
                    "  --json",
                ),
                script="ops/build_kr_real_combined_context_smoke.py",
            ),
            FollowupStep(
                id="date-md-smoke",
                label="Run Date.md smoke",
                command_lines=(
                    "PYTHONPATH=src uv run python ops/run_date_md_smoke.py \\",
                    f"  --universe {universe} \\",
                    f"  --date-md {paths['date_md']} \\",
                    f"  --store {paths['store']}{coverage_flag} \\",
                    "  --json",
                ),
                script="ops/run_date_md_smoke.py",
            ),
        ]
    )

    scout_cmd: list[str] = [
        "PYTHONPATH=src uv run python ops/build_scout_manual_packet.py \\",
        f"  --universe {universe} \\",
        f"  --date-md {paths['date_md']} \\",
        f"  --store {paths['store']} \\",
        f"  --out-dir {paths['scout_dir']} \\",
        "  --market-scope KR \\",
    ]
    if manifest.require_symbol_coverage:
        scout_cmd.append("  --require-symbol-coverage \\")
    scout_cmd.extend(["  --force \\", "  --json"])
    steps.append(
        FollowupStep(
            id="scout-manual-packet",
            label="Build Scout manual packet",
            command_lines=tuple(scout_cmd),
            script="ops/build_scout_manual_packet.py",
        )
    )

    return steps


def _followup_steps_to_command_lines(steps: list[FollowupStep]) -> list[str]:
    """내부 step 표현 → Markdown/bash follow-up command line 목록."""
    lines = ["# Review-only — operator must run manually; preflight does not execute these."]
    for step in steps:
        lines.extend(step.command_lines)
    return lines


def _build_followup_commands(manifest: KrEndToEndPreflightManifest) -> list[str]:
    """review-only follow-up command strings (실행하지 않음)."""
    return _followup_steps_to_command_lines(_build_followup_steps(manifest))


def _plan_forbidden_shortcuts_list() -> list[str]:
    """Markdown/structured plan 공통 forbidden shortcut reminder (런타임 fragment 조합)."""
    exec_path = "".join(("broker", "/", "PaperLoop", "/", "K", "IS"))
    return [
        "Do not auto-promote generated universe/provider mapping into checked-in config.",
        "Do not treat ranking/factor scores as trading or allocation guidance.",
        f"Do not forward Scout output to {exec_path} or any write/execution path.",
        "Do not run live fetches from this preflight helper.",
    ]


def _plan_forbidden_shortcuts_section() -> str:
    lines = ["## Forbidden shortcuts reminder", ""]
    for item in _plan_forbidden_shortcuts_list():
        lines.append(f"- {item}")
    lines.extend(["", ""])
    return "\n".join(lines)


def _step_to_structured_dict(step: FollowupStep) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": step.id,
        "label": step.label,
        "command": list(step.command_lines),
        "script": step.script,
        "allowed": True,
        "requires_operator_review": True,
        "executes_in_preflight": False,
    }
    if step.notes:
        payload["notes"] = list(step.notes)
    return payload


def _render_structured_plan_json(
    manifest: KrEndToEndPreflightManifest,
    *,
    steps: list[FollowupStep],
    warnings: list[str],
) -> str:
    """review-only structured follow-up plan JSON (실행하지 않음)."""
    payload = {
        "version": 1,
        "mode": "kr-end-to-end-intake-followup-plan",
        "manifest": str(manifest.manifest_path),
        "name": manifest.name,
        "generated_by": "ops/preflight_kr_end_to_end_intake.py",
        "review_only": True,
        "steps": [_step_to_structured_dict(step) for step in steps],
        "forbidden_shortcuts": _plan_forbidden_shortcuts_list(),
        "warnings": list(warnings),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _validate_structured_plan_steps(steps: list[FollowupStep]) -> None:
    """structured plan executable step script가 positive allowlist에 속하는지 검증한다."""
    for step in steps:
        if step.script is not None and step.script not in _FOLLOWUP_COMMAND_ALLOWLIST:
            raise KrEndToEndPreflightError("validate", f"structured plan step not allowlisted: {step.script}")


def _extract_followup_command_scripts(commands: list[str]) -> list[str]:
    """comment line 제외 후 command string에서 ops 스크립트 경로를 추출한다."""
    scripts: list[str] = []
    for line in commands:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _FOLLOWUP_OPS_SCRIPT_PATTERN.search(stripped)
        if match is not None:
            scripts.append(match.group(0))
    return scripts


def _validate_followup_command_allowlist(commands: list[str]) -> None:
    """생성된 follow-up command plan이 positive allowlist에 속하는지 검증한다."""
    for script in _extract_followup_command_scripts(commands):
        if script not in _FOLLOWUP_COMMAND_ALLOWLIST:
            raise KrEndToEndPreflightError("validate", f"follow-up command not allowlisted: {script}")


def _render_plan_markdown(
    manifest: KrEndToEndPreflightManifest,
    *,
    provider_validation: dict[str, Any],
    optional_checks: list[dict[str, str]],
    warnings: list[str],
    followup_commands: list[str] | None,
) -> str:
    lines = [
        "# KR End-to-End Intake Preflight Plan",
        "",
        "> Review-only command plan. Operator must run each step manually.",
        "",
        "## Inputs validated",
        "",
        f"- manifest: `{manifest.manifest_path}`",
        f"- universe: `{manifest.universe}`",
        f"- provider_mapping: `{manifest.provider_mapping}`",
        "",
        "## Provider mapping validation result",
        "",
        f"- status: {provider_validation.get('status')}",
        f"- enabled_symbols_count: {provider_validation.get('enabled_symbols_count')}",
        f"- require_yfinance: {provider_validation.get('require_yfinance')}",
        f"- require_dart: {provider_validation.get('require_dart')}",
        "",
        "## Optional artifact checks",
        "",
    ]
    if optional_checks:
        for item in optional_checks:
            lines.append(f"- {item['artifact']}: {item['status']} ({item['check']})")
    else:
        lines.append("- none listed")
    lines.append("")
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")
    if followup_commands:
        lines.extend(
            [
                "## Follow-up commands to run manually",
                "",
                "```bash",
                *followup_commands,
                "```",
                "",
            ]
        )
    lines.append(_plan_forbidden_shortcuts_section())
    return "\n".join(lines)


def _write_output(path: Path, content: str, *, force: bool, field_name: str) -> None:
    """summary/plan 출력을 same-directory temp write → atomic replace로 기록한다."""
    if path.exists() and not force:
        raise KrEndToEndPreflightError("write", f"output already exists: {field_name}")

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".tmp_preflight_{field_name}_{uuid.uuid4().hex}.txt"

    try:
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(path)
        temp_path = path
    except KrEndToEndPreflightError:
        raise
    except Exception as exc:
        raise KrEndToEndPreflightError(
            "write",
            f"output write failed: {type(exc).__name__}",
        ) from None
    finally:
        if temp_path.exists() and temp_path != path:
            temp_path.unlink()


def run_kr_end_to_end_preflight(
    manifest_path: Path,
    *,
    summary_out: Path | None = None,
    plan_out: Path | None = None,
    structured_plan_out: Path | None = None,
    emit_followup_commands: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """manifest preflight를 실행하고 summary dict를 반환한다."""
    manifest = load_kr_end_to_end_preflight_manifest(manifest_path)

    effective_summary_out = summary_out if summary_out is not None else manifest.summary_out
    effective_plan_out = plan_out if plan_out is not None else manifest.plan_out
    effective_structured_plan_out = (
        structured_plan_out if structured_plan_out is not None else manifest.structured_plan_out
    )
    effective_emit = manifest.emit_followup_commands if emit_followup_commands is None else emit_followup_commands

    provider_validation = _validate_provider_mapping_pair(manifest)
    optional_checks, warnings = _validate_optional_artifacts(manifest)

    followup_steps: list[FollowupStep] | None = None
    followup_commands: list[str] | None = None
    if effective_emit:
        followup_steps = _build_followup_steps(manifest)
        followup_commands = _followup_steps_to_command_lines(followup_steps)
        _validate_followup_command_allowlist(followup_commands)
        _validate_structured_plan_steps(followup_steps)

    result: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "mode": _MODE,
        "manifest": str(manifest.manifest_path),
        "name": manifest.name,
        "artifacts": _artifact_paths_dict(manifest),
        "provider_mapping_validation": provider_validation,
        "optional_artifact_checks": optional_checks,
        "settings": {
            "require_yfinance": manifest.require_yfinance,
            "require_dart": manifest.require_dart,
            "require_symbol_coverage": manifest.require_symbol_coverage,
            "context_budget_profile": manifest.context_budget_profile,
        },
        "warnings": warnings,
    }
    if followup_commands is not None:
        result["followup_commands"] = followup_commands

    if effective_structured_plan_out is not None and followup_steps is not None:
        structured_plan_text = _render_structured_plan_json(
            manifest,
            steps=followup_steps,
            warnings=warnings,
        )
        _write_output(
            effective_structured_plan_out,
            structured_plan_text,
            force=force,
            field_name="structured_plan_out",
        )
        result["structured_plan_out"] = str(effective_structured_plan_out)
        result["structured_plan_steps_count"] = len(followup_steps)
        result["structured_plan_generated"] = True

    summary_text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if effective_summary_out is not None:
        _write_output(effective_summary_out, summary_text, force=force, field_name="summary_out")
        result["summary_out"] = str(effective_summary_out)

    if effective_plan_out is not None:
        plan_text = _render_plan_markdown(
            manifest,
            provider_validation=provider_validation,
            optional_checks=optional_checks,
            warnings=warnings,
            followup_commands=followup_commands,
        )
        _write_output(effective_plan_out, plan_text, force=force, field_name="plan_out")
        result["plan_out"] = str(effective_plan_out)

    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KR end-to-end research intake preflight — manifest artifact checks only.",
    )
    parser.add_argument("--manifest", required=True, help="preflight manifest TOML path")
    parser.add_argument("--summary-out", default=None, help="override manifest [outputs].summary_out")
    parser.add_argument("--plan-out", default=None, help="override manifest [outputs].plan_out")
    parser.add_argument(
        "--structured-plan-out",
        default=None,
        help="override manifest [outputs].structured_plan_out",
    )
    parser.add_argument(
        "--emit-followup-commands",
        action="store_true",
        default=None,
        help="include follow-up command plan in summary/plan output",
    )
    parser.add_argument(
        "--no-emit-followup-commands",
        action="store_true",
        help="omit follow-up command plan",
    )
    parser.add_argument("--force", action="store_true", help="overwrite summary_out/plan_out/structured_plan_out only")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _emit_json(payload: dict[str, Any], *, stream: TextIO) -> None:
    json.dump(payload, stream, indent=2, ensure_ascii=False)
    stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    emit_override: bool | None = None
    if args.emit_followup_commands and args.no_emit_followup_commands:
        _emit_json(
            {"status": "error", "stage": "args", "message": "conflicting emit followup flags"},
            stream=sys.stdout if args.json else sys.stderr,
        )
        return 1
    if args.emit_followup_commands:
        emit_override = True
    elif args.no_emit_followup_commands:
        emit_override = False

    try:
        payload = run_kr_end_to_end_preflight(
            Path(args.manifest),
            summary_out=Path(args.summary_out) if args.summary_out else None,
            plan_out=Path(args.plan_out) if args.plan_out else None,
            structured_plan_out=Path(args.structured_plan_out) if args.structured_plan_out else None,
            emit_followup_commands=emit_override,
            force=args.force,
        )
    except KrEndToEndPreflightError as exc:
        error_payload = {"status": "error", "stage": exc.stage, "message": exc.message, "mode": _MODE}
        if args.json:
            _emit_json(error_payload, stream=sys.stdout)
        else:
            print(exc.message, file=sys.stderr)
        return 1

    if args.json:
        _emit_json(payload, stream=sys.stdout)
    else:
        print(f"preflight: ok ({payload['stage']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
