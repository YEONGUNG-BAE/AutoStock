#!/usr/bin/env python3
"""OpenDART corp-code master 로컬 fixture resolver (3C1).

로컬 XML/ZIP만 읽는다. network/env/API key/read/write 없음.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

StageName = Literal["args", "parse", "resolve", "complete"]

from data.dart_corp_code_resolver import (
    DartCorpCodeResolverError,
    parse_corp_code_xml_file,
    parse_corp_code_zip_file,
    resolve_corp_code_by_stock_code,
)

class ResolveCorpCodeError(Exception):
    """resolve_dart_corp_code CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve OpenDART corp_code from local corp-code master fixture/XML.",
    )
    parser.add_argument(
        "--corp-code-xml",
        default=None,
        help="local corp-code master XML path",
    )
    parser.add_argument(
        "--corp-code-zip",
        default=None,
        help="local corp-code master ZIP path (single XML member inside)",
    )
    parser.add_argument("--stock-code", required=True, help="KR stock code (e.g. 005930)")
    parser.add_argument(
        "--corp-name",
        default=None,
        help="optional corp_name disambiguator when stock_code is ambiguous",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _parse_master(path: Path) -> tuple[Any, ...]:
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return parse_corp_code_zip_file(path)
    if suffix == ".xml":
        return parse_corp_code_xml_file(path)
    raise ResolveCorpCodeError(
        "args",
        f"unsupported corp-code master file type: {path.suffix!r} (use .xml or .zip)",
    )


def run_resolve_dart_corp_code(
    *,
    master_path: Path,
    stock_code: str,
    corp_name: str | None,
) -> dict[str, Any]:
    """로컬 corp-code master에서 stock_code → corp_code를 조회한다."""
    try:
        entries = _parse_master(master_path)
    except DartCorpCodeResolverError as exc:
        raise ResolveCorpCodeError("parse", str(exc)) from exc

    try:
        match = resolve_corp_code_by_stock_code(
            entries,
            stock_code,
            corp_name=corp_name,
        )
    except DartCorpCodeResolverError as exc:
        raise ResolveCorpCodeError("resolve", str(exc)) from exc

    return {
        "status": "ok",
        "stage": "complete",
        "stock_code": match.stock_code,
        "corp_code": match.corp_code,
        "corp_name": match.corp_name,
        "modify_date": match.modify_date,
    }


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Resolve DART corp_code: {status}", file=out)
    for key in ("stock_code", "corp_code", "corp_name", "modify_date", "stage", "error"):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout
    stage: StageName = "args"

    if bool(args.corp_code_xml) == bool(args.corp_code_zip):
        payload = {
            "status": "error",
            "stage": "args",
            "error": "exactly one of --corp-code-xml or --corp-code-zip is required",
        }
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    master_path = Path(args.corp_code_xml or args.corp_code_zip)

    try:
        payload = run_resolve_dart_corp_code(
            master_path=master_path,
            stock_code=args.stock_code,
            corp_name=args.corp_name,
        )
    except ResolveCorpCodeError as exc:
        stage = exc.stage
        payload = {
            "status": "error",
            "stage": stage,
            "error": exc.message,
        }
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    _emit_result(payload, as_json=as_json, out=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
