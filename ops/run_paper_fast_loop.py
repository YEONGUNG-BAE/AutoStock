#!/usr/bin/env python3
"""RTM-7c.4a operator CLI for the offline paper fast-loop composition.

Six mutually-exclusive modes:

* ``--validate-only`` (default): load+validate the on-disk execution-inputs snapshot
  and run single-symbol preflight. No execution, no DB writes, no network.
* ``--inspect-existing``: read-only inspection of the configured ledger / journal /
  active-decision-store (``mode=ro`` + ``PRAGMA query_only=ON``).
* ``--precheck-runtime``: read-only attended runtime precheck — reuses inspect-existing and
  fingerprints every artifact before/after to prove read-only. Config is loaded with an EMPTY
  environ so no credential/env var is read (a ``${ENV}`` reference fails closed). Machine PASS
  is NOT an activation authorization (``activation_authorized`` stays false).
* ``--verify-precheck-receipt``: stdin-only strict receipt schema + hash verification; no
  config, no env, no DB, no filesystem write, no network (RTM-7c.4e).
* ``--replay FIXTURE``: deterministic offline replay against a built-in normalized-event
  fixture, using a fresh OS temp dir (never the configured ``runtime/`` paths).
* ``--run``: REFUSED. Returns ``outcome=NO_GO`` / ``reason_code=live_run_not_implemented``
  with a non-zero exit code BEFORE reading any credential, opening any network socket,
  touching the production DB, or creating any filesystem path.

``--json`` emits a sanitized machine-readable summary. Credentials, raw frames,
exception reprs, tracebacks, and DB dumps are never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

from composition.paper_fast_loop import (
    AVAILABLE_REPLAY_FIXTURES,
    InspectionOutcome,
    MachineCheckOutcome,
    PaperFastLoopOutcome,
    build_paper_fast_loop_plan,
    inspect_paper_fast_loop,
    precheck_runtime,
    replay_offline,
)
from composition.precheck_receipt_verifier import (
    ReceiptVerificationOutcome,
    RuntimePrecheckReceiptVerification,
    verify_runtime_precheck_receipt_payload,
)
from config.settings import AppSettings, SettingsError, load_settings

DEFAULT_CONFIG_PATH = "config/config.toml.example"
_KST = ZoneInfo("Asia/Seoul")
_RUN_REFUSED_REASON = "live_run_not_implemented"
_VERIFY_RECEIPT_STDIN_LIMIT = 1 << 20  # 1 MiB — untrusted stdin bound


class CliInputError(Exception):
    """CLI 입력/모드 위반. 메시지에 credential을 담지 않는다."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline paper fast-loop composition operator tool (no orders, no money, no network).",
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH, help=f"config path (default: {DEFAULT_CONFIG_PATH})"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "config + execution-input snapshot validation only; opens/creates/writes no "
            "database, runs no position preflight, no network (default)"
        ),
    )
    parser.add_argument(
        "--inspect-existing",
        action="store_true",
        help=(
            "read-only startup-readiness inspection: snapshot + active decision + DB "
            "schema/state + single-symbol position; reconciles nothing, fail-closed NO_GO"
        ),
    )
    parser.add_argument(
        "--precheck-runtime",
        action="store_true",
        help=(
            "read-only attended runtime precheck: reuses inspect-existing and fingerprints "
            "every artifact before/after to prove read-only; machine PASS is NOT an activation "
            "authorization (activation_authorized stays false; explicit Operator approval + "
            "manual writer-stop confirmation required)"
        ),
    )
    parser.add_argument(
        "--verify-precheck-receipt",
        action="store_true",
        help=(
            "stdin-only strict precheck receipt verification (schema + hash); no config, "
            "no env, no DB, no filesystem write, no network; VALID is NOT activation "
            "authorization"
        ),
    )
    parser.add_argument(
        "--replay",
        metavar="FIXTURE",
        default=None,
        help=f"deterministic offline replay fixture; one of {list(AVAILABLE_REPLAY_FIXTURES)}",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="REFUSED: live run not implemented (returns NO_GO with non-zero exit)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON summary")
    return parser


def _resolve_mode(args: argparse.Namespace) -> str:
    selected = [
        name
        for name, on in (
            ("run", args.run),
            ("inspect-existing", args.inspect_existing),
            ("precheck-runtime", args.precheck_runtime),
            ("verify-precheck-receipt", args.verify_precheck_receipt),
            ("replay", args.replay is not None),
            ("validate-only", args.validate_only),
        )
        if on
    ]
    if len(selected) > 1:
        raise CliInputError(
            "modes --validate-only / --inspect-existing / --precheck-runtime / "
            "--verify-precheck-receipt / --replay / --run are mutually exclusive."
        )
    return selected[0] if selected else "validate-only"


def _emit(summary: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False), file=out)
        return
    print(f"paper fast-loop: {summary.get('outcome', 'FAIL')}", file=out)
    for key, value in summary.items():
        if key == "outcome":
            continue
        print(f"{key}: {value}", file=out)


def _fail(reason: str, *, as_json: bool, out: TextIO) -> int:
    _emit({"outcome": "FAIL", "reason_code": reason}, as_json=as_json, out=out)
    return 1


def _run_refused_summary() -> dict[str, Any]:
    return {
        "outcome": "NO_GO",
        "mode": "run",
        "reason_code": _RUN_REFUSED_REASON,
        "credential_read": False,
        "network_called": False,
        "production_db_touched": False,
        "filesystem_written": False,
    }


def _validate_summary(plan: Any, *, config_path: str, enabled: bool) -> dict[str, Any]:
    passed = plan.outcome is PaperFastLoopOutcome.READY
    return {
        "outcome": "PASS" if passed else "FAIL",
        "mode": "validate-only",
        "config": config_path,
        "enabled": enabled,
        "market": plan.market,
        "symbol": plan.symbol,
        "snapshot_source_id": plan.snapshot_source_id,
        "snapshot_universe": plan.snapshot_universe,
        "snapshot_expires_at": plan.snapshot_expires_at,
        "plan_outcome": plan.outcome.value,
        "reasons": list(plan.reasons),
        "network_called": False,
        "production_db_touched": False,
    }


def _inspect_summary(inspection: Any, *, config_path: str, enabled: bool) -> dict[str, Any]:
    passed = inspection.outcome is InspectionOutcome.OK
    return {
        "outcome": "PASS" if passed else "NO_GO",
        "mode": "inspect-existing",
        "config": config_path,
        "enabled": enabled,
        "market": inspection.market,
        "symbol": inspection.symbol,
        "inspection_outcome": inspection.outcome.value,
        "reasons": list(inspection.reasons),
        "missing_databases": list(inspection.missing_databases),
        "ledger": asdict(inspection.ledger) if inspection.ledger is not None else None,
        "journal": _journal_to_dict(inspection.journal),
        "active_store": asdict(inspection.active_store) if inspection.active_store is not None else None,
        "execution_inputs": (
            asdict(inspection.execution_inputs) if inspection.execution_inputs is not None else None
        ),
        "active_decision": (
            asdict(inspection.active_decision) if inspection.active_decision is not None else None
        ),
        "network_called": False,
    }


def _fingerprint_dict(fp: Any) -> dict[str, Any]:
    return {
        "name": fp.name,
        "present": fp.present,
        "is_regular_file": fp.is_regular_file,
        "size": fp.size,
        "sha256": fp.sha256,
        "user_version": fp.user_version,
        "sidecar_suffixes": list(fp.sidecar_suffixes),
    }


def _receipt_to_dict(receipt: Any) -> dict[str, Any]:
    """Sanitized receipt JSON — 경로/config/secret/DB 내용/traceback 제외."""
    return {
        "schema_version": receipt.schema_version,
        "checked_at": receipt.checked_at,
        "market": receipt.market,
        "symbol": receipt.symbol,
        "enabled": receipt.enabled,
        "machine_outcome": receipt.machine_outcome,
        "inspection_outcome": receipt.inspection_outcome,
        "reasons": list(receipt.reasons),
        "fingerprints_before": [_fingerprint_dict(fp) for fp in receipt.fingerprints_before],
        "fingerprints_after": [_fingerprint_dict(fp) for fp in receipt.fingerprints_after],
        "activation_authorized": receipt.activation_authorized,
        "runtime_activation_outcome": receipt.runtime_activation_outcome,
        "explicit_operator_approval_required": receipt.explicit_operator_approval_required,
        "writers_stopped_manual_confirmation_required": (
            receipt.writers_stopped_manual_confirmation_required
        ),
        "receipt_sha256": receipt.receipt_sha256,
    }


def _precheck_summary(result: Any, *, config_path: str, enabled: bool) -> dict[str, Any]:
    passed = result.machine_outcome is MachineCheckOutcome.PASS
    return {
        # machine PASS ≠ activation authorization — outcome reports only the mechanical verdict.
        "outcome": "PASS" if passed else "NO_GO",
        "mode": "precheck-runtime",
        "config": config_path,
        "enabled": enabled,
        "market": result.market,
        "symbol": result.symbol,
        "machine_check_outcome": result.machine_outcome.value,
        "activation_authorized": result.activation_authorized,
        "runtime_activation_outcome": result.runtime_activation_outcome,
        "explicit_operator_approval_required": result.explicit_operator_approval_required,
        "writers_stopped_manual_confirmation_required": (
            result.writers_stopped_manual_confirmation_required
        ),
        "reasons": list(result.reasons),
        "inspection_outcome": result.inspection.outcome.value,
        "inspection_reasons": list(result.inspection.reasons),
        "missing_databases": list(result.inspection.missing_databases),
        "fingerprints_before": [_fingerprint_dict(fp) for fp in result.fingerprints_before],
        "fingerprints_after": [_fingerprint_dict(fp) for fp in result.fingerprints_after],
        "precheck_receipt": _receipt_to_dict(result.receipt),
        "network_called": False,
        "credential_read": False,
        "broker_called": False,
        "production_db_written": False,
        "runtime_file_created": False,
    }


def _verify_receipt_summary(result: Any) -> dict[str, Any]:
    valid = result.outcome is ReceiptVerificationOutcome.VALID
    return {
        "outcome": "VALID" if valid else "INVALID",
        "mode": "verify-precheck-receipt",
        "schema_version": result.schema_version,
        "receipt_sha256": result.receipt_sha256,
        "reason_codes": list(result.reason_codes),
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
        "credential_read": False,
        "network_called": False,
        "database_opened": False,
        "filesystem_written": False,
    }


def _verify_receipt_input_fail(reason_code: str, *, as_json: bool, out: TextIO) -> int:
    summary = _verify_receipt_summary(
        RuntimePrecheckReceiptVerification(
            outcome=ReceiptVerificationOutcome.INVALID,
            schema_version=None,
            receipt_sha256=None,
            reason_codes=(reason_code,),
        )
    )
    _emit(summary, as_json=as_json, out=out)
    return 1


def _read_verify_stdin_payload() -> tuple[object | None, str | None]:
    """stdin에서 최대 ``_VERIFY_RECEIPT_STDIN_LIMIT + 1`` byte만 읽는다."""
    data = sys.stdin.buffer.read(_VERIFY_RECEIPT_STDIN_LIMIT + 1)
    if not data:
        return None, "receipt_input_empty"
    if len(data) > _VERIFY_RECEIPT_STDIN_LIMIT:
        return None, "receipt_input_too_large"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "receipt_input_not_utf8"
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "receipt_input_not_json"


def _journal_to_dict(journal: Any) -> dict[str, Any] | None:
    if journal is None:
        return None
    data = asdict(journal)
    # state_counts는 dataclass 리스트이므로 asdict가 이미 dict로 변환한다.
    return data


def _replay_summary(result: Any, *, config_path: str) -> dict[str, Any]:
    return {
        "outcome": "PASS",
        "mode": "replay",
        "config": config_path,
        "fixture": result.fixture,
        "market": result.market,
        "symbol": result.symbol,
        "snapshot_loaded": result.snapshot_loaded,
        "snapshot_reason": result.snapshot_reason,
        "event_count": result.event_count,
        "statuses": list(result.statuses),
        "first_status": result.first_status,
        "repeat_status": result.repeat_status,
        "restart_status": result.restart_status,
        "committed_count": result.committed_count,
        "order_result_count": result.order_result_count,
        "filled_result_count": result.filled_result_count,
        "fill_count": result.fill_count,
        "journal_state_counts": [list(item) for item in result.journal_state_counts],
        "journal_terminal_count": result.journal_terminal_count,
        "final_position_quantity": result.final_position_quantity,
        "final_cash_amount": result.final_cash_amount,
        "used_temp_dir": True,
        "runtime_paths_touched": False,
        "network_called": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    as_json = args.json
    out: TextIO = sys.stdout

    try:
        mode = _resolve_mode(args)
    except CliInputError as exc:
        return _fail(str(exc), as_json=as_json, out=out)

    # --run은 어떤 부작용보다 먼저 거부한다(credential/network/DB/fs 접근 전).
    if mode == "run":
        _emit(_run_refused_summary(), as_json=as_json, out=out)
        return 2

    # verify-precheck-receipt: stdin-only — config/env/DB/fs write/network/clock read 없음.
    if mode == "verify-precheck-receipt":
        payload, input_error = _read_verify_stdin_payload()
        if input_error is not None:
            return _verify_receipt_input_fail(input_error, as_json=as_json, out=out)
        try:
            result = verify_runtime_precheck_receipt_payload(payload)
        except Exception as exc:
            return _verify_receipt_input_fail(
                f"verify error: {type(exc).__name__}", as_json=as_json, out=out
            )
        summary = _verify_receipt_summary(result)
        _emit(summary, as_json=as_json, out=out)
        return 0 if summary["outcome"] == "VALID" else 1

    # precheck-runtime은 credential/env read 0을 주장하므로 config 로딩에서도 os.environ을
    # 절대 읽지 않는다: 빈 environ을 주입해 ${ENV} 치환과 live-confirmation/credential
    # 게이트가 모두 sanitized ConfigEnvironmentError/RuntimeGateError로 fail-closed되게 한다.
    # 다른 mode의 동작은 바꾸지 않는다.
    settings_environ: Mapping[str, str] | None = {} if mode == "precheck-runtime" else None
    try:
        settings: AppSettings = load_settings(args.config, environ=settings_environ)
    except (SettingsError, OSError) as exc:
        return _fail(f"config error: {type(exc).__name__}", as_json=as_json, out=out)

    fast_loop = settings.runtime.paper_fast_loop

    if mode == "validate-only":
        plan = build_paper_fast_loop_plan(settings=fast_loop, now=datetime.now(tz=_KST))
        summary = _validate_summary(plan, config_path=args.config, enabled=fast_loop.enabled)
        _emit(summary, as_json=as_json, out=out)
        return 0 if summary["outcome"] == "PASS" else 1

    if mode == "inspect-existing":
        try:
            inspection = inspect_paper_fast_loop(settings=fast_loop, now=datetime.now(tz=_KST))
        except Exception as exc:  # 어떤 sqlite/내부 오류도 traceback 없이 sanitized fail로.
            return _fail(f"inspect error: {type(exc).__name__}", as_json=as_json, out=out)
        summary = _inspect_summary(inspection, config_path=args.config, enabled=fast_loop.enabled)
        _emit(summary, as_json=as_json, out=out)
        return 0 if summary["outcome"] == "PASS" else 1

    if mode == "precheck-runtime":
        try:
            result = precheck_runtime(settings=fast_loop, now=datetime.now(tz=_KST))
        except Exception as exc:  # 어떤 sqlite/내부 오류도 traceback 없이 sanitized fail로.
            return _fail(f"precheck error: {type(exc).__name__}", as_json=as_json, out=out)
        summary = _precheck_summary(result, config_path=args.config, enabled=fast_loop.enabled)
        _emit(summary, as_json=as_json, out=out)
        return 0 if summary["outcome"] == "PASS" else 1

    if mode == "replay":
        fixture = args.replay
        if fixture not in AVAILABLE_REPLAY_FIXTURES:
            return _fail(
                f"unknown replay fixture; choose one of {list(AVAILABLE_REPLAY_FIXTURES)}.",
                as_json=as_json,
                out=out,
            )
        try:
            with tempfile.TemporaryDirectory(prefix="paper_fast_loop_replay_") as tmp:
                result = replay_offline(settings=fast_loop, temp_dir=Path(tmp), fixture=fixture)
        except Exception as exc:  # replay 내부 오류도 traceback 없이 sanitized fail로.
            return _fail(f"replay error: {type(exc).__name__}", as_json=as_json, out=out)
        _emit(_replay_summary(result, config_path=args.config), as_json=as_json, out=out)
        return 0

    return _fail("unsupported mode.", as_json=as_json, out=out)  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
