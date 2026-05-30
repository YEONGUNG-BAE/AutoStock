#!/usr/bin/env python3
"""Provider mapping registry 로컬 검증 CLI (3D1).

universe TOML + provider mapping TOML을 읽어 coverage를 검증한다.
network/env/API key/read/write 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

StageName = Literal["args", "load", "validate", "complete"]

from data.provider_mapping_registry import (
    ProviderMappingError,
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


class ValidateProviderMappingError(Exception):
    """validate_provider_mapping CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate provider mapping registry coverage against a universe file.",
    )
    parser.add_argument("--universe", required=True, help="universe TOML path")
    parser.add_argument(
        "--provider-mapping",
        required=True,
        help="provider mapping registry TOML path",
    )
    parser.add_argument(
        "--no-require-yfinance",
        action="store_true",
        help="do not require yfinance provider mapping for enabled universe symbols",
    )
    parser.add_argument(
        "--no-require-dart",
        action="store_true",
        help="do not require DART provider mapping for enabled KR universe symbols",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def run_validate_provider_mapping(
    *,
    universe_path: Path,
    provider_mapping_path: Path,
    require_yfinance: bool = True,
    require_dart: bool = True,
) -> dict[str, Any]:
    """universe와 provider mapping registry coverage를 검증한다."""
    try:
        universe = load_universe_toml(universe_path)
    except (FileNotFoundError, ValueError) as exc:
        raise ValidateProviderMappingError("load", str(exc)) from exc

    try:
        registry = load_provider_mapping_toml(provider_mapping_path)
    except (FileNotFoundError, ProviderMappingError) as exc:
        raise ValidateProviderMappingError("load", str(exc)) from exc

    try:
        validate_provider_mappings_cover_universe(
            registry,
            universe,
            require_yfinance=require_yfinance,
            require_dart=require_dart,
        )
    except ProviderMappingError as exc:
        raise ValidateProviderMappingError("validate", str(exc)) from exc

    covered_symbols = [
        {"market": entry.market, "symbol": entry.symbol}
        for entry in universe.enabled_symbols
    ]
    return {
        "status": "ok",
        "stage": "complete",
        "universe_name": universe.name,
        "mapping_name": registry.name,
        "enabled_universe_symbols": len(universe.enabled_symbols),
        "enabled_mappings": len(registry.enabled_mappings),
        "covered_symbols": covered_symbols,
    }


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Validate provider mapping: {status}", file=out)
    for key in (
        "universe_name",
        "mapping_name",
        "enabled_universe_symbols",
        "enabled_mappings",
        "covered_symbols",
        "stage",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    try:
        payload = run_validate_provider_mapping(
            universe_path=Path(args.universe),
            provider_mapping_path=Path(args.provider_mapping),
            require_yfinance=not args.no_require_yfinance,
            require_dart=not args.no_require_dart,
        )
    except ValidateProviderMappingError as exc:
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
