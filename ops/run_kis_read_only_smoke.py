#!/usr/bin/env python3
"""Manual KIS read-only smoke script.

Does not submit orders.
Does not call tiny-live/live order endpoints.
Does not run automatically from scheduler/runtime.
Prints only masked/non-sensitive summaries.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, TextIO

from broker.kis_client import ISA_SMOKE_SYMBOL_KR, KisReadOnlyClient
from broker.kis_models import IsaSupportStatus, KisReadOnlySmokeResult, mask_account_number
from broker.kis_transport import StdlibKisHttpTransport
from config.settings import AppSettings, SettingsError, load_settings

DEFAULT_CONFIG_PATH = "config/config.toml.example"

ModeName = Literal["none", "check-config-only", "dry-run", "run"]

# error message sanitization용 패턴 (secret/raw account/token 유출 방지)
_DIGIT_SEQUENCE_RE = re.compile(r"\d{6,}")
_BEARER_TOKEN_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE)
_APP_SECRET_LIKE_RE = re.compile(r"(appsecret|app_secret|access_token)\s*[:=]\s*\S+", re.IGNORECASE)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual KIS read-only smoke (explicit opt-in; no orders).",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"config path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--check-config-only",
        action="store_true",
        help="config/env preflight only; no KIS HTTP calls",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned read-only smoke steps; no KIS HTTP calls",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="perform actual KIS read-only HTTP smoke (explicit manual opt-in)",
    )
    parser.add_argument(
        "--kr-symbol",
        default=ISA_SMOKE_SYMBOL_KR,
        help=f"KR quote/orderbook smoke symbol (default: {ISA_SMOKE_SYMBOL_KR})",
    )
    parser.add_argument(
        "--us-symbol",
        default="AAPL",
        help="reserved US symbol (current smoke check does not call US endpoints)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="runtime timeout override (does not modify config file)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON summary",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print non-sensitive metadata only (no raw response body)",
    )
    return parser


def _resolve_mode(args: argparse.Namespace) -> ModeName | None:
    """상호 배타 mode flag를 검증한다. conflict 시 None."""
    selected = [name for name, enabled in (
        ("run", args.run),
        ("dry-run", args.dry_run),
        ("check-config-only", args.check_config_only),
    ) if enabled]
    if len(selected) > 1:
        return None
    if args.run:
        return "run"
    if args.dry_run:
        return "dry-run"
    if args.check_config_only:
        return "check-config-only"
    return "none"


def _env_value_present(environ: dict[str, str], env_var: str) -> bool:
    return bool(environ.get(env_var, "").strip())


def _env_entry(
    environ: dict[str, str],
    env_var: str,
    *,
    required: bool,
    skipped: bool = False,
    mask_if_present: bool = False,
) -> dict[str, Any]:
    """env var 존재 여부 summary. raw value 출력 금지."""
    if skipped:
        return {"name": env_var, "status": "skipped"}
    present = _env_value_present(environ, env_var)
    entry: dict[str, Any] = {
        "name": env_var,
        "status": "present" if present else "missing",
        "required": required,
    }
    if present and mask_if_present:
        entry["masked"] = mask_account_number(environ[env_var].strip())
    return entry


def _build_env_preflight(settings: AppSettings, environ: dict[str, str]) -> dict[str, Any]:
    """required/optional env var preflight summary."""
    live = settings.broker.live
    roles = settings.broker.account_roles

    required_env: list[dict[str, Any]] = [
        _env_entry(environ, live.app_key_env, required=True),
        _env_entry(environ, live.app_secret_env, required=True),
    ]

    isa_required = roles.use_isa_for_kr_and_gold
    required_env.append(
        _env_entry(
            environ,
            roles.kr_tax_advantaged_account_env,
            required=isa_required,
            skipped=not isa_required,
            mask_if_present=True,
        )
    )

    cma_required = roles.use_cma_for_order_execution
    optional_env: list[dict[str, Any]] = [
        _env_entry(
            environ,
            roles.us_regular_account_env,
            required=False,
            mask_if_present=True,
        ),
        _env_entry(
            environ,
            roles.cash_buffer_account_env,
            required=cma_required,
            skipped=not cma_required,
            mask_if_present=True,
        ),
    ]

    missing_required = [
        item["name"]
        for item in required_env
        if item.get("required") and item["status"] == "missing"
    ]

    return {
        "required_env": required_env,
        "optional_env": optional_env,
        "missing_required": missing_required,
        "env_ok": len(missing_required) == 0,
    }


def _sanitize_error_message(message: str) -> str:
    """error summary에서 digit sequence / bearer token / secret-like fragment를 마스킹한다."""
    sanitized = _BEARER_TOKEN_RE.sub("Bearer ***", message)
    sanitized = _APP_SECRET_LIKE_RE.sub(r"\1=***", sanitized)
    sanitized = _DIGIT_SEQUENCE_RE.sub(lambda match: "*" * min(len(match.group(0)), 8), sanitized)
    return sanitized


def _sanitize_errors(errors: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_sanitize_error_message(error) for error in errors)


def _apply_timeout_override(settings: AppSettings, timeout: float | None) -> AppSettings:
    if timeout is None:
        return settings
    return replace(
        settings,
        broker=replace(
            settings.broker,
            kis_read_only=replace(settings.broker.kis_read_only, timeout_seconds=timeout),
        ),
    )


def _planned_steps(settings: AppSettings, *, kr_symbol: str, us_symbol: str) -> list[str]:
    """dry-run planned read-only steps."""
    roles = settings.broker.account_roles
    steps = ["token request would be called"]
    if roles.use_isa_for_kr_and_gold:
        steps.append("ISA balance inquiry would be called")
    else:
        steps.append("ISA balance inquiry would be skipped (use_isa_for_kr_and_gold=false)")
    steps.append(f"KR quote would be called (symbol={kr_symbol})")
    steps.append(f"KR orderbook would be called (symbol={kr_symbol})")
    steps.append(f"US symbol {us_symbol!r} is unused by current smoke check")
    steps.append("order endpoints would not be called")
    return steps


def _smoke_passed(result: KisReadOnlySmokeResult, settings: AppSettings) -> bool:
    """run mode exit 판정."""
    if not result.token_ok or not result.quote_ok or not result.orderbook_ok:
        return False
    if settings.broker.account_roles.use_isa_for_kr_and_gold:
        if result.isa_support_status == IsaSupportStatus.SKIPPED:
            return True
        return result.balance_ok
    return True


def _build_base_summary(
    *,
    mode: ModeName,
    config_path: Path,
    settings: AppSettings,
    env_preflight: dict[str, Any],
    kr_symbol: str,
    us_symbol: str,
    http_called: bool,
) -> dict[str, Any]:
    live = settings.broker.live
    read_only = settings.broker.kis_read_only
    return {
        "outcome": "PASS" if env_preflight["env_ok"] else "FAIL",
        "mode": mode,
        "config": str(config_path),
        "base_url": live.base_url,
        "read_only_enabled": read_only.enabled,
        "timeout_seconds": read_only.timeout_seconds,
        "required_env": env_preflight["required_env"],
        "optional_env": env_preflight["optional_env"],
        "missing_required": env_preflight["missing_required"],
        "kr_symbol": kr_symbol,
        "us_symbol": us_symbol,
        "us_symbol_used": False,
        "http_called": http_called,
        "orders_called": False,
    }


def _emit_summary(summary: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False), file=out)
        return

    outcome = summary.get("outcome", "FAIL")
    print(f"KIS read-only smoke: {outcome}", file=out)
    for key in (
        "stage",
        "reason",
        "usage",
        "mode",
        "config",
        "base_url",
        "read_only_enabled",
        "timeout_seconds",
        "missing_required",
        "kr_symbol",
        "us_symbol",
        "us_symbol_used",
        "planned_steps",
        "token_ok",
        "balance_ok",
        "quote_ok",
        "orderbook_ok",
        "isa_support_status",
        "error_count",
        "errors",
        "checked_at",
        "http_called",
        "orders_called",
    ):
        if key not in summary:
            continue
        print(f"{key}: {summary[key]}", file=out)

    if "required_env" in summary:
        print("required_env:", file=out)
        for item in summary["required_env"]:
            line = f"  {item['name']}: {item['status']}"
            if item.get("masked"):
                line += f" (masked: {item['masked']})"
            print(line, file=out)

    if "optional_env" in summary:
        print("optional_env:", file=out)
        for item in summary["optional_env"]:
            line = f"  {item['name']}: {item['status']}"
            if item.get("masked"):
                line += f" (masked: {item['masked']})"
            print(line, file=out)


def _fail(
    stage: str,
    reason: str,
    *,
    as_json: bool = False,
    out: TextIO = sys.stdout,
    extra: dict[str, Any] | None = None,
) -> int:
    summary: dict[str, Any] = {"outcome": "FAIL", "stage": stage, "reason": reason}
    if extra:
        summary.update(extra)
    _emit_summary(summary, as_json=as_json, out=out)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout
    config_path = Path(args.config)

    mode = _resolve_mode(args)
    if mode is None:
        return _fail(
            "input",
            "only one of --run, --dry-run, --check-config-only may be used",
            as_json=as_json,
            out=out,
        )

    if args.verbose:
        print(f"verbose: mode={mode}", file=out)
        print(f"verbose: config={config_path}", file=out)
        print(f"verbose: kr_symbol={args.kr_symbol}", file=out)
        print(f"verbose: us_symbol={args.us_symbol}", file=out)

    try:
        settings = load_settings(config_path)
    except SettingsError as exc:
        return _fail("config", str(exc), as_json=as_json, out=out)
    except OSError as exc:
        return _fail("config", str(exc), as_json=as_json, out=out)

    settings = _apply_timeout_override(settings, args.timeout)
    environ = dict(os.environ)
    env_preflight = _build_env_preflight(settings, environ)

    if mode == "none":
        summary = _build_base_summary(
            mode=mode,
            config_path=config_path,
            settings=settings,
            env_preflight=env_preflight,
            kr_symbol=args.kr_symbol,
            us_symbol=args.us_symbol,
            http_called=False,
        )
        summary["outcome"] = "PASS"
        summary["usage"] = "use --check-config-only, --dry-run, or --run"
        _emit_summary(summary, as_json=as_json, out=out)
        return 0

    if not env_preflight["env_ok"]:
        summary = _build_base_summary(
            mode=mode,
            config_path=config_path,
            settings=settings,
            env_preflight=env_preflight,
            kr_symbol=args.kr_symbol,
            us_symbol=args.us_symbol,
            http_called=False,
        )
        summary["outcome"] = "FAIL"
        summary["stage"] = "env"
        _emit_summary(summary, as_json=as_json, out=out)
        return 1

    if mode in {"check-config-only", "dry-run"}:
        summary = _build_base_summary(
            mode=mode,
            config_path=config_path,
            settings=settings,
            env_preflight=env_preflight,
            kr_symbol=args.kr_symbol,
            us_symbol=args.us_symbol,
            http_called=False,
        )
        summary["outcome"] = "PASS"
        if mode == "dry-run":
            summary["planned_steps"] = _planned_steps(
                settings,
                kr_symbol=args.kr_symbol,
                us_symbol=args.us_symbol,
            )
        _emit_summary(summary, as_json=as_json, out=out)
        return 0

    # mode == "run"
    client = KisReadOnlyClient(
        live_settings=settings.broker.live,
        account_role_settings=settings.broker.account_roles,
        read_only_settings=settings.broker.kis_read_only,
        transport=StdlibKisHttpTransport(),
        environ=environ,
    )

    try:
        smoke_result = client.run_read_only_smoke_check(
            kr_symbol=args.kr_symbol,
            us_symbol=args.us_symbol,
        )
    except Exception as exc:  # noqa: BLE001 — ops entrypoint fail-closed
        return _fail(
            "http",
            _sanitize_error_message(str(exc)),
            as_json=as_json,
            out=out,
            extra={
                "mode": mode,
                "config": str(config_path),
                "http_called": True,
                "orders_called": False,
            },
        )

    sanitized_errors = _sanitize_errors(smoke_result.errors)
    passed = _smoke_passed(smoke_result, settings)

    summary = _build_base_summary(
        mode=mode,
        config_path=config_path,
        settings=settings,
        env_preflight=env_preflight,
        kr_symbol=args.kr_symbol,
        us_symbol=args.us_symbol,
        http_called=True,
    )
    summary.update(
        {
            "outcome": "PASS" if passed else "FAIL",
            "stage": None if passed else "http",
            "token_ok": smoke_result.token_ok,
            "balance_ok": smoke_result.balance_ok,
            "quote_ok": smoke_result.quote_ok,
            "orderbook_ok": smoke_result.orderbook_ok,
            "isa_support_status": smoke_result.isa_support_status.value,
            "error_count": len(sanitized_errors),
            "errors": list(sanitized_errors),
            "checked_at": smoke_result.checked_at.isoformat(),
        }
    )
    _emit_summary(summary, as_json=as_json, out=out)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
