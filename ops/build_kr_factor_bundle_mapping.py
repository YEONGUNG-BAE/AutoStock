#!/usr/bin/env python3
"""Operator-local factor input bundle manifest workflow (3G4-3).

bundle manifest TOML → 3G4-2 factor-ranked mapping workflow.
network/env/API key/live factor scoring/trading 호출 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO

from build_kr_factor_ranked_mapping import (
    BuildKrFactorRankedMappingError,
    run_build_kr_factor_ranked_mapping,
)

StageName = Literal["args", "parse", "generate", "rank", "resolve", "write", "validate", "complete"]

_BUNDLE_ROOT_KEYS = frozenset({"version", "name", "description", "base_market", "inputs", "outputs", "names", "selection"})
_BUNDLE_INPUT_KEYS = frozenset({"candidate_pool", "factor_inputs", "corp_code_xml", "corp_code_zip"})
_BUNDLE_OUTPUT_KEYS = frozenset(
    {
        "factor_signals_out",
        "ranked_out",
        "selected_candidates_out",
        "universe_out",
        "provider_mapping_out",
    }
)
_BUNDLE_NAMES_KEYS = frozenset(
    {
        "factor_output_name",
        "factor_output_description",
        "selection_name",
        "selection_description",
        "universe_name",
        "provider_mapping_name",
    }
)
_BUNDLE_SELECTION_KEYS = frozenset({"sector", "max_total", "max_per_sector", "top_n"})
_BUNDLE_FORBIDDEN_KEYS = frozenset(
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
        "corp_code",
        "api_key",
        "env",
    }
)

_OUT_DIR_FILENAMES = {
    "factor_signals_out": "factor_signals.generated.toml",
    "ranked_out": "ranked.json",
    "selected_candidates_out": "selected_candidates.toml",
    "universe_out": "universe.generated.toml",
    "provider_mapping_out": "provider_mappings.generated.toml",
}


class BuildKrFactorBundleMappingError(Exception):
    """build_kr_factor_bundle_mapping workflow 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class KrFactorBundle:
    """operator-local factor input bundle manifest (local paths; unresolved)."""

    version: int
    name: str
    description: str
    base_market: str
    candidate_pool: str
    factor_inputs: str
    corp_code_xml: str | None
    corp_code_zip: str | None
    factor_signals_out: str
    ranked_out: str
    selected_candidates_out: str
    universe_out: str
    provider_mapping_out: str
    factor_output_name: str
    factor_output_description: str | None
    selection_name: str
    selection_description: str | None
    universe_name: str
    provider_mapping_name: str
    sectors: tuple[str, ...] | None
    max_total: int | None
    max_per_sector: int | None
    top_n: int | None


def _contains_control_character(value: str) -> bool:
    """ASCII control(0x00–0x1F) 및 DEL(0x7F) 포함 여부."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuildKrFactorBundleMappingError("parse", f"{field_name} is required")
    if _contains_control_character(value):
        raise BuildKrFactorBundleMappingError("parse", f"{field_name} contains a control character")
    return value.strip()


def _optional_text(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BuildKrFactorBundleMappingError("parse", f"{field_name} must be a string")
    if not value.strip():
        raise BuildKrFactorBundleMappingError("parse", f"{field_name} must be nonblank when present")
    if _contains_control_character(value):
        raise BuildKrFactorBundleMappingError("parse", f"{field_name} contains a control character")
    return value.strip()


def _required_path_text(value: object, *, field_name: str) -> str:
    """경로 문자열 필수 검증(제어 문자 거부)."""
    return _required_text(value, field_name=field_name)


def _validate_unknown_keys(raw: dict[str, object], allowed: frozenset[str], *, table_name: str) -> None:
    unknown = set(raw.keys()) - allowed
    forbidden = unknown & _BUNDLE_FORBIDDEN_KEYS
    if forbidden:
        joined = ", ".join(sorted(forbidden))
        raise BuildKrFactorBundleMappingError("parse", f"forbidden {table_name} fields: {joined}")
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise BuildKrFactorBundleMappingError("parse", f"unknown {table_name} fields: {joined}")


def _validate_positive_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BuildKrFactorBundleMappingError("parse", f"{field_name} must be a positive integer")
    if value <= 0:
        raise BuildKrFactorBundleMappingError("parse", f"{field_name} must be a positive integer")
    return value


def _parse_table(raw: object, *, table_name: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise BuildKrFactorBundleMappingError("parse", f"{table_name} must be a table")
    return raw


def load_kr_factor_bundle_toml(path: Path) -> KrFactorBundle:
    """operator-local factor input bundle manifest TOML을 strict schema로 파싱한다."""
    if not path.is_file():
        raise BuildKrFactorBundleMappingError("parse", f"bundle manifest not found: {path}")

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise BuildKrFactorBundleMappingError("parse", f"invalid bundle TOML: {exc}") from exc

    if not isinstance(raw, dict):
        raise BuildKrFactorBundleMappingError("parse", "bundle TOML root must be a table")

    _validate_unknown_keys(raw, _BUNDLE_ROOT_KEYS, table_name="root")

    version = raw.get("version")
    if version != 1:
        raise BuildKrFactorBundleMappingError("parse", "version must be exactly 1")

    name = _required_text(raw.get("name"), field_name="name")
    description = _required_text(raw.get("description"), field_name="description")
    base_market = _required_text(raw.get("base_market"), field_name="base_market")
    if base_market != "KR":
        raise BuildKrFactorBundleMappingError("parse", "base_market must be exactly KR")

    inputs_raw = _parse_table(raw.get("inputs"), table_name="inputs")
    _validate_unknown_keys(inputs_raw, _BUNDLE_INPUT_KEYS, table_name="inputs")

    candidate_pool = _required_path_text(inputs_raw.get("candidate_pool"), field_name="inputs.candidate_pool")
    factor_inputs = _required_path_text(inputs_raw.get("factor_inputs"), field_name="inputs.factor_inputs")

    corp_code_xml_raw = inputs_raw.get("corp_code_xml")
    corp_code_zip_raw = inputs_raw.get("corp_code_zip")
    corp_code_xml = (
        _required_path_text(corp_code_xml_raw, field_name="inputs.corp_code_xml")
        if corp_code_xml_raw is not None
        else None
    )
    corp_code_zip = (
        _required_path_text(corp_code_zip_raw, field_name="inputs.corp_code_zip")
        if corp_code_zip_raw is not None
        else None
    )
    if corp_code_xml is not None and corp_code_zip is not None:
        raise BuildKrFactorBundleMappingError(
            "parse",
            "exactly one of inputs.corp_code_xml or inputs.corp_code_zip is required",
        )
    if corp_code_xml is None and corp_code_zip is None:
        raise BuildKrFactorBundleMappingError(
            "parse",
            "exactly one of inputs.corp_code_xml or inputs.corp_code_zip is required",
        )

    outputs_raw = _parse_table(raw.get("outputs"), table_name="outputs")
    _validate_unknown_keys(outputs_raw, _BUNDLE_OUTPUT_KEYS, table_name="outputs")
    factor_signals_out = _required_path_text(
        outputs_raw.get("factor_signals_out"),
        field_name="outputs.factor_signals_out",
    )
    ranked_out = _required_path_text(outputs_raw.get("ranked_out"), field_name="outputs.ranked_out")
    selected_candidates_out = _required_path_text(
        outputs_raw.get("selected_candidates_out"),
        field_name="outputs.selected_candidates_out",
    )
    universe_out = _required_path_text(outputs_raw.get("universe_out"), field_name="outputs.universe_out")
    provider_mapping_out = _required_path_text(
        outputs_raw.get("provider_mapping_out"),
        field_name="outputs.provider_mapping_out",
    )

    names_raw = _parse_table(raw.get("names"), table_name="names")
    _validate_unknown_keys(names_raw, _BUNDLE_NAMES_KEYS, table_name="names")
    factor_output_name = _required_text(
        names_raw.get("factor_output_name"),
        field_name="names.factor_output_name",
    )
    factor_output_description = _optional_text(
        names_raw.get("factor_output_description"),
        field_name="names.factor_output_description",
    )
    selection_name = _required_text(names_raw.get("selection_name"), field_name="names.selection_name")
    selection_description = _optional_text(
        names_raw.get("selection_description"),
        field_name="names.selection_description",
    )
    universe_name = _required_text(names_raw.get("universe_name"), field_name="names.universe_name")
    provider_mapping_name = _required_text(
        names_raw.get("provider_mapping_name"),
        field_name="names.provider_mapping_name",
    )

    sectors: tuple[str, ...] | None = None
    max_total: int | None = None
    max_per_sector: int | None = None
    top_n: int | None = None
    selection_raw = raw.get("selection")
    if selection_raw is not None:
        selection_table = _parse_table(selection_raw, table_name="selection")
        _validate_unknown_keys(selection_table, _BUNDLE_SELECTION_KEYS, table_name="selection")
        sector_raw = selection_table.get("sector")
        if sector_raw is not None:
            if not isinstance(sector_raw, list) or not sector_raw:
                raise BuildKrFactorBundleMappingError("parse", "selection.sector must be a non-empty array")
            parsed_sectors: list[str] = []
            for index, sector_value in enumerate(sector_raw):
                if not isinstance(sector_value, str) or not sector_value.strip():
                    raise BuildKrFactorBundleMappingError(
                        "parse",
                        f"selection.sector[{index}] must be a nonblank string",
                    )
                if _contains_control_character(sector_value):
                    raise BuildKrFactorBundleMappingError(
                        "parse",
                        f"selection.sector[{index}] contains a control character",
                    )
                parsed_sectors.append(sector_value.strip())
            sectors = tuple(parsed_sectors)
        if "max_total" in selection_table:
            max_total = _validate_positive_int(selection_table["max_total"], field_name="selection.max_total")
        if "max_per_sector" in selection_table:
            max_per_sector = _validate_positive_int(
                selection_table["max_per_sector"],
                field_name="selection.max_per_sector",
            )
        if "top_n" in selection_table:
            top_n = _validate_positive_int(selection_table["top_n"], field_name="selection.top_n")

    return KrFactorBundle(
        version=1,
        name=name,
        description=description,
        base_market=base_market,
        candidate_pool=candidate_pool,
        factor_inputs=factor_inputs,
        corp_code_xml=corp_code_xml,
        corp_code_zip=corp_code_zip,
        factor_signals_out=factor_signals_out,
        ranked_out=ranked_out,
        selected_candidates_out=selected_candidates_out,
        universe_out=universe_out,
        provider_mapping_out=provider_mapping_out,
        factor_output_name=factor_output_name,
        factor_output_description=factor_output_description,
        selection_name=selection_name,
        selection_description=selection_description,
        universe_name=universe_name,
        provider_mapping_name=provider_mapping_name,
        sectors=sectors,
        max_total=max_total,
        max_per_sector=max_per_sector,
        top_n=top_n,
    )


def _resolve_path(bundle_dir: Path, value: str) -> Path:
    """bundle manifest 기준 상대/절대 경로를 resolve한다."""
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate.resolve()
    return (bundle_dir / value).resolve()


def _resolve_output_paths(
    bundle: KrFactorBundle,
    *,
    bundle_dir: Path,
    out_dir: Path | None,
) -> dict[str, Path]:
    if out_dir is not None:
        return {key: (out_dir / filename).resolve() for key, filename in _OUT_DIR_FILENAMES.items()}
    return {
        "factor_signals_out": _resolve_path(bundle_dir, bundle.factor_signals_out),
        "ranked_out": _resolve_path(bundle_dir, bundle.ranked_out),
        "selected_candidates_out": _resolve_path(bundle_dir, bundle.selected_candidates_out),
        "universe_out": _resolve_path(bundle_dir, bundle.universe_out),
        "provider_mapping_out": _resolve_path(bundle_dir, bundle.provider_mapping_out),
    }


def _validate_out_dir_arg(out_dir: Path | None) -> None:
    if out_dir is None:
        return
    out_dir_text = str(out_dir)
    if not out_dir_text.strip():
        raise BuildKrFactorBundleMappingError("args", "--out-dir must be a nonblank path")
    if _contains_control_character(out_dir_text):
        raise BuildKrFactorBundleMappingError("args", "--out-dir contains a control character")


def run_build_kr_factor_bundle_mapping(
    bundle_path: Path,
    out_dir: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """bundle manifest → 3G4-2 factor-ranked mapping workflow 실행."""
    _validate_out_dir_arg(out_dir)
    bundle = load_kr_factor_bundle_toml(bundle_path)
    bundle_dir = bundle_path.parent.resolve()
    output_paths = _resolve_output_paths(bundle, bundle_dir=bundle_dir, out_dir=out_dir)

    try:
        workflow_payload = run_build_kr_factor_ranked_mapping(
            candidate_pool_path=_resolve_path(bundle_dir, bundle.candidate_pool),
            factor_inputs_path=_resolve_path(bundle_dir, bundle.factor_inputs),
            corp_code_xml=(
                _resolve_path(bundle_dir, bundle.corp_code_xml) if bundle.corp_code_xml is not None else None
            ),
            corp_code_zip=(
                _resolve_path(bundle_dir, bundle.corp_code_zip) if bundle.corp_code_zip is not None else None
            ),
            factor_signals_out=output_paths["factor_signals_out"],
            ranked_out=output_paths["ranked_out"],
            selected_candidates_out=output_paths["selected_candidates_out"],
            universe_out=output_paths["universe_out"],
            provider_mapping_out=output_paths["provider_mapping_out"],
            factor_output_name=bundle.factor_output_name,
            factor_output_description=bundle.factor_output_description,
            selection_name=bundle.selection_name,
            selection_description=bundle.selection_description,
            universe_name=bundle.universe_name,
            provider_mapping_name=bundle.provider_mapping_name,
            sectors=set(bundle.sectors) if bundle.sectors is not None else None,
            max_total=bundle.max_total,
            max_per_sector=bundle.max_per_sector,
            top_n=bundle.top_n,
            force=force,
        )
    except BuildKrFactorRankedMappingError as exc:
        raise BuildKrFactorBundleMappingError(exc.stage, exc.message) from exc

    return {
        "status": "ok",
        "stage": "complete",
        "mode": "factor-bundle-ranked-mapping-workflow",
        "bundle": str(bundle_path.resolve()),
        "out_dir": str(out_dir.resolve()) if out_dir is not None else None,
        "factor_signals_out": str(output_paths["factor_signals_out"]),
        "ranked_out": str(output_paths["ranked_out"]),
        "selected_candidates_out": str(output_paths["selected_candidates_out"]),
        "universe_out": str(output_paths["universe_out"]),
        "provider_mapping_out": str(output_paths["provider_mapping_out"]),
        "signals_count": workflow_payload["signals_count"],
        "ranked_count": workflow_payload["ranked_count"],
        "selected_count": workflow_payload["selected_count"],
        "validation": workflow_payload["validation"],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build KR universe/provider mapping from operator-local factor input bundle manifest "
            "(3G4-3 wrapper over 3G4-2; local files only)."
        ),
    )
    parser.add_argument("--bundle", required=True, help="operator-local factor input bundle manifest TOML path")
    parser.add_argument(
        "--out-dir",
        default=None,
        help="optional output directory override (recommended operator path)",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing output files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Build KR factor bundle mapping: {status}", file=out)
    for key in (
        "stage",
        "mode",
        "bundle",
        "out_dir",
        "factor_signals_out",
        "ranked_out",
        "selected_candidates_out",
        "universe_out",
        "provider_mapping_out",
        "signals_count",
        "ranked_count",
        "selected_count",
        "validation",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    try:
        out_dir = Path(args.out_dir) if args.out_dir is not None else None
        payload = run_build_kr_factor_bundle_mapping(
            bundle_path=Path(args.bundle),
            out_dir=out_dir,
            force=args.force,
        )
    except BuildKrFactorBundleMappingError as exc:
        payload = {
            "status": "error",
            "stage": exc.stage,
            "error": exc.message,
        }
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    _emit_result(payload, as_json=as_json, out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
