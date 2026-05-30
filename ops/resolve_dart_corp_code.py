#!/usr/bin/env python3
"""OpenDART corp-code master resolver (3C1 local fixture + 3C2 live fetch).

로컬 XML/ZIP 모드: network/env/API key 없음.
--live-fetch 모드: operator 명시 시에만 env API key + HTTP fetch + immutable ZIP snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

StageName = Literal["args", "fetch", "snapshot", "parse", "resolve", "complete"]

from data.dart_corp_code_resolver import (
    DartCorpCodeResolverError,
    parse_corp_code_xml_file,
    parse_corp_code_zip_file,
    resolve_corp_code_by_stock_code,
)

DEFAULT_API_KEY_ENV = "DART_API_KEY"


class ResolveCorpCodeError(Exception):
    """resolve_dart_corp_code CLI 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve OpenDART corp_code from local corp-code master or live fetch.",
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
    parser.add_argument(
        "--live-fetch",
        action="store_true",
        help="fetch corp-code master ZIP from OpenDART (operator explicit; requires env API key)",
    )
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help=f"env var name for OpenDART API key in --live-fetch mode (default: {DEFAULT_API_KEY_ENV})",
    )
    parser.add_argument(
        "--snapshot-dir",
        default=None,
        help="directory for immutable raw ZIP snapshot (--live-fetch only)",
    )
    parser.add_argument("--stock-code", required=True, help="KR stock code (e.g. 005930)")
    parser.add_argument(
        "--corp-name",
        default=None,
        help="optional corp_name disambiguator when stock_code is ambiguous",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    return parser


def _validate_source_mode(
    *,
    corp_code_xml: str | None,
    corp_code_zip: str | None,
    live_fetch: bool,
) -> None:
    modes = [bool(corp_code_xml), bool(corp_code_zip), bool(live_fetch)]
    if sum(modes) != 1:
        raise ResolveCorpCodeError(
            "args",
            "exactly one of --corp-code-xml, --corp-code-zip, or --live-fetch is required",
        )


def _read_api_key_from_env(env_name: str) -> str | None:
    normalized_env = env_name.strip()
    if not normalized_env:
        return None
    raw = os.environ.get(normalized_env)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    return value


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


def _resolve_from_entries(
    entries: tuple[Any, ...],
    *,
    stock_code: str,
    corp_name: str | None,
) -> dict[str, Any]:
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


def _run_live_fetch(
    *,
    snapshot_dir: Path,
    stock_code: str,
    corp_name: str | None,
    api_key_env: str,
    fetch_zip_bytes: Callable[[str], bytes] | None,
    fetched_at: datetime | None,
) -> dict[str, Any]:
    if not api_key_env.strip():
        raise ResolveCorpCodeError("args", "api key env var name must not be blank")

    api_key = _read_api_key_from_env(api_key_env)
    if api_key is None:
        raise ResolveCorpCodeError(
            "args",
            f"API key env var {api_key_env.strip()!r} is missing or blank",
        )

    from data.dart_corp_code_http_client import (
        DartCorpCodeHttpError,
        fetch_corp_code_zip_bytes,
    )

    try:
        if fetch_zip_bytes is not None:
            zip_bytes = fetch_zip_bytes(api_key)
        else:
            zip_bytes = fetch_corp_code_zip_bytes(api_key=api_key)
    except DartCorpCodeHttpError as exc:
        raise ResolveCorpCodeError("fetch", exc.message) from exc

    try:
        from data.dart_corp_code_live_client import (
            DartCorpCodeSnapshotError,
            ensure_zip_bytes,
            write_corp_code_zip_snapshot,
        )

        ensure_zip_bytes(zip_bytes)
    except DartCorpCodeSnapshotError as exc:
        raise ResolveCorpCodeError("fetch", str(exc)) from exc

    snapshot_at = fetched_at or datetime.now(UTC)
    try:
        snapshot_path = write_corp_code_zip_snapshot(
            zip_bytes=zip_bytes,
            snapshot_dir=snapshot_dir,
            fetched_at=snapshot_at,
        )
    except DartCorpCodeSnapshotError as exc:
        raise ResolveCorpCodeError("snapshot", str(exc)) from exc

    try:
        entries = parse_corp_code_zip_file(snapshot_path)
    except DartCorpCodeResolverError as exc:
        raise ResolveCorpCodeError("parse", str(exc)) from exc

    payload = _resolve_from_entries(entries, stock_code=stock_code, corp_name=corp_name)
    payload["mode"] = "live-fetch"
    payload["snapshot_path"] = str(snapshot_path)
    return payload


def run_resolve_dart_corp_code(
    *,
    master_path: Path | None = None,
    live_fetch: bool = False,
    snapshot_dir: Path | None = None,
    stock_code: str,
    corp_name: str | None,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    fetch_zip_bytes: Callable[[str], bytes] | None = None,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """로컬 corp-code master 또는 live fetch snapshot에서 stock_code → corp_code를 조회한다."""
    if live_fetch:
        if snapshot_dir is None:
            raise ResolveCorpCodeError("args", "--snapshot-dir is required for --live-fetch")
        return _run_live_fetch(
            snapshot_dir=snapshot_dir,
            stock_code=stock_code,
            corp_name=corp_name,
            api_key_env=api_key_env,
            fetch_zip_bytes=fetch_zip_bytes,
            fetched_at=fetched_at,
        )

    if master_path is None:
        raise ResolveCorpCodeError(
            "args",
            "exactly one of --corp-code-xml, --corp-code-zip, or --live-fetch is required",
        )

    try:
        entries = _parse_master(master_path)
    except DartCorpCodeResolverError as exc:
        raise ResolveCorpCodeError("parse", str(exc)) from exc

    return _resolve_from_entries(entries, stock_code=stock_code, corp_name=corp_name)


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"Resolve DART corp_code: {status}", file=out)
    for key in (
        "mode",
        "stock_code",
        "corp_code",
        "corp_name",
        "modify_date",
        "snapshot_path",
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
        _validate_source_mode(
            corp_code_xml=args.corp_code_xml,
            corp_code_zip=args.corp_code_zip,
            live_fetch=args.live_fetch,
        )
    except ResolveCorpCodeError as exc:
        payload = {
            "status": "error",
            "stage": exc.stage,
            "error": exc.message,
        }
        _emit_result(payload, as_json=as_json, out=out)
        return 1

    try:
        if args.live_fetch:
            if args.snapshot_dir is None:
                raise ResolveCorpCodeError(
                    "args",
                    "--snapshot-dir is required for --live-fetch",
                )
            payload = run_resolve_dart_corp_code(
                live_fetch=True,
                snapshot_dir=Path(args.snapshot_dir),
                stock_code=args.stock_code,
                corp_name=args.corp_name,
                api_key_env=args.api_key_env,
            )
        else:
            master_path = Path(args.corp_code_xml or args.corp_code_zip)
            payload = run_resolve_dart_corp_code(
                master_path=master_path,
                stock_code=args.stock_code,
                corp_name=args.corp_name,
            )
    except ResolveCorpCodeError as exc:
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
