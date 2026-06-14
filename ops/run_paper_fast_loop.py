#!/usr/bin/env python3
"""RTM-7c.4a operator CLI for the offline paper fast-loop composition.

Nine mutually-exclusive modes:

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
* ``--revalidate-activation-candidate``: stdin receipt + config; read-only revalidation of
  current artifact state against a verified machine PASS receipt (RTM-7c.4g). Config loads
  with empty ``environ``; no clock read, no DB connection, no filesystem write, no network.
  Mechanical PASS is NOT activation authorization.
* ``--final-preflight-activation-candidate``: stdin receipt + config; 4g byte-state
  revalidation followed by a fresh current-time machine precheck (snapshot / active-decision
  validity windows) bound back to the receipt's post-inspection state (RTM-7c.4h). Config loads
  with empty ``environ``; current-time validity IS evaluated, receipt age and freshness policy
  are NOT; the composed precheck opens the configured DBs read-only (no write). Mechanical PASS
  is NOT activation authorization.
* ``--freshness-preflight-activation-candidate``: stdin receipt + config + required explicit
  ``--max-age-microseconds``; composes verified final preflight with explicit freshness policy
  (RTM-7c.4l). Config loads with empty ``environ``; ``now=datetime.now(tz=KST)``; no default
  threshold; mechanical FRESH PASS is NOT activation authorization.
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
from composition.precheck_receipt_stdin_json import ReceiptStdinJsonError, parse_receipt_stdin_json
from composition.activation_candidate_revalidation import (
    ActivationCandidateRevalidationOutcome,
    revalidate_activation_candidate,
)
from composition.activation_candidate_final_preflight import (
    ActivationCandidateFinalPreflightOutcome,
    final_preflight_activation_candidate,
)
from composition.activation_candidate_freshness_preflight import (
    ActivationCandidateFreshnessPreflightOutcome,
    freshness_qualify_activation_candidate,
)
from composition.freshness_policy_cli_input import parse_max_age_microseconds_cli_input
from composition.receipt_freshness_policy import ReceiptFreshnessPolicy
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
        "--revalidate-activation-candidate",
        action="store_true",
        help=(
            "stdin receipt + config: read-only approval-time state revalidation against a "
            "verified machine PASS receipt; config loads with empty environ; mechanical PASS "
            "is NOT activation authorization"
        ),
    )
    parser.add_argument(
        "--final-preflight-activation-candidate",
        action="store_true",
        help=(
            "stdin receipt + config: 4g byte-state revalidation + policy-neutral receipt time "
            "observation (exact receipt_age_microseconds; future checked_at fail-closed) + fresh "
            "current-time machine precheck (snapshot/active-decision validity) bound back to the "
            "receipt's post-inspection state; fresh_precheck_executed reports whether that "
            "precheck ran, no TTL/max-age/freshness policy; mechanical PASS is NOT activation "
            "authorization"
        ),
    )
    parser.add_argument(
        "--freshness-preflight-activation-candidate",
        action="store_true",
        help=(
            "stdin receipt + config + required --max-age-microseconds: verified final preflight "
            "with explicit freshness policy evaluation; config loads with empty environ; no "
            "default threshold; mechanical FRESH PASS is NOT activation authorization"
        ),
    )
    parser.add_argument(
        "--max-age-microseconds",
        default=None,
        metavar="MICROSECONDS",
        help=(
            "required explicit ASCII decimal max receipt age in microseconds; only valid with "
            "--freshness-preflight-activation-candidate"
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
            ("revalidate-activation-candidate", args.revalidate_activation_candidate),
            ("final-preflight-activation-candidate", args.final_preflight_activation_candidate),
            ("freshness-preflight-activation-candidate", args.freshness_preflight_activation_candidate),
            ("replay", args.replay is not None),
            ("validate-only", args.validate_only),
        )
        if on
    ]
    if len(selected) > 1:
        raise CliInputError(
            "modes --validate-only / --inspect-existing / --precheck-runtime / "
            "--verify-precheck-receipt / --revalidate-activation-candidate / "
            "--final-preflight-activation-candidate / "
            "--freshness-preflight-activation-candidate / --replay / --run are mutually exclusive."
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
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
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


def _revalidate_summary(result: Any, *, config_path: str) -> dict[str, Any]:
    passed = result.outcome is ActivationCandidateRevalidationOutcome.PASS
    return {
        "outcome": "PASS" if passed else "NO_GO",
        "mode": "revalidate-activation-candidate",
        "config": config_path,
        "receipt_sha256": result.receipt_sha256,
        "market": result.market,
        "symbol": result.symbol,
        "reasons": list(result.reasons),
        "activation_authorized": result.activation_authorized,
        "runtime_activation_outcome": result.runtime_activation_outcome,
        "explicit_operator_approval_required": result.explicit_operator_approval_required,
        "writers_stopped_manual_confirmation_required": (
            result.writers_stopped_manual_confirmation_required
        ),
        "freshness_evaluated": result.freshness_evaluated,
        "credential_read": False,
        "network_called": False,
        "database_opened": False,
        "filesystem_written": False,
    }


def _revalidate_input_fail(reason_code: str, *, as_json: bool, out: TextIO) -> int:
    summary = {
        "outcome": "NO_GO",
        "mode": "revalidate-activation-candidate",
        "receipt_sha256": None,
        "market": None,
        "symbol": None,
        "reasons": [reason_code],
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
        "explicit_operator_approval_required": True,
        "writers_stopped_manual_confirmation_required": True,
        "freshness_evaluated": False,
        "credential_read": False,
        "network_called": False,
        "database_opened": False,
        "filesystem_written": False,
    }
    _emit(summary, as_json=as_json, out=out)
    return 1


def _final_preflight_summary(result: Any) -> dict[str, Any]:
    passed = result.outcome is ActivationCandidateFinalPreflightOutcome.PASS
    precheck = result.current_precheck_result
    return {
        # mechanical PASS ≠ activation authorization — outcome reports only the time-aware verdict.
        # config path는 출력하지 않는다(로컬 디렉터리/사용자명 노출 방지). path-free summary 유지.
        "outcome": "PASS" if passed else "NO_GO",
        "mode": "final-preflight-activation-candidate",
        "receipt_sha256": result.receipt_sha256,
        "market": result.market,
        "symbol": result.symbol,
        "reasons": list(result.reasons),
        "current_precheck_outcome": precheck.machine_outcome.value if precheck is not None else None,
        "current_precheck_reasons": list(precheck.reasons) if precheck is not None else [],
        # per-call 실행 사실: fresh precheck가 실제로 돌았는지. read-only DB inspection이
        # 이 안에서 일어날 수 있으나, connection 개수를 주장하지 않는다.
        "fresh_precheck_executed": result.fresh_precheck_executed,
        # RTM-7c.4i policy-neutral receipt time observation: exact age in microseconds (or
        # null for future/pre-comparison). NOT a TTL/max-age/freshness verdict.
        "receipt_age_evaluated": result.receipt_age_evaluated,
        "receipt_age_microseconds": result.receipt_age_microseconds,
        "freshness_policy_evaluated": result.freshness_policy_evaluated,
        "activation_authorized": result.activation_authorized,
        "runtime_activation_outcome": result.runtime_activation_outcome,
        "explicit_operator_approval_required": result.explicit_operator_approval_required,
        "writers_stopped_manual_confirmation_required": (
            result.writers_stopped_manual_confirmation_required
        ),
        "credential_read": False,
        "network_called": False,
        "broker_called": False,
        "operational_db_written": False,
        "filesystem_written": False,
        "runtime_file_created": False,
    }


def _final_preflight_input_fail(reason_code: str, *, as_json: bool, out: TextIO) -> int:
    summary = {
        "outcome": "NO_GO",
        "mode": "final-preflight-activation-candidate",
        "receipt_sha256": None,
        "market": None,
        "symbol": None,
        "reasons": [reason_code],
        "current_precheck_outcome": None,
        "current_precheck_reasons": [],
        "fresh_precheck_executed": False,
        "receipt_age_evaluated": False,
        "receipt_age_microseconds": None,
        "freshness_policy_evaluated": False,
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
        "explicit_operator_approval_required": True,
        "writers_stopped_manual_confirmation_required": True,
        "credential_read": False,
        "network_called": False,
        "broker_called": False,
        "operational_db_written": False,
        "filesystem_written": False,
        "runtime_file_created": False,
    }
    _emit(summary, as_json=as_json, out=out)
    return 1


def _freshness_preflight_summary(
    result: Any, *, parsed_max_age: int | None
) -> dict[str, Any]:
    passed = result.outcome is ActivationCandidateFreshnessPreflightOutcome.PASS
    final_result = result.final_preflight_result
    freshness_eval = result.freshness_evaluation

    if freshness_eval is not None:
        receipt_age = freshness_eval.receipt_age_microseconds
        max_age = freshness_eval.max_age_microseconds
    elif final_result is not None:
        receipt_age = (
            final_result.receipt_age_microseconds
            if final_result.receipt_age_evaluated
            else None
        )
        max_age = parsed_max_age
    else:
        receipt_age = None
        max_age = parsed_max_age

    final_outcome: str | None = None
    final_reasons: list[str] = []
    if final_result is not None:
        final_outcome = final_result.outcome.value
        final_reasons = list(final_result.reasons)

    return {
        "outcome": "PASS" if passed else "NO_GO",
        "mode": "freshness_preflight_activation_candidate",
        "receipt_sha256": result.receipt_sha256,
        "market": result.market,
        "symbol": result.symbol,
        "reasons": list(result.reasons),
        "freshness_policy_evaluated": result.freshness_policy_evaluated,
        "receipt_age_microseconds": receipt_age,
        "max_age_microseconds": max_age,
        "final_preflight_outcome": final_outcome,
        "final_preflight_reasons": final_reasons,
        "activation_authorized": result.activation_authorized,
        "runtime_activation_outcome": result.runtime_activation_outcome,
        "explicit_operator_approval_required": result.explicit_operator_approval_required,
        "writers_stopped_manual_confirmation_required": (
            result.writers_stopped_manual_confirmation_required
        ),
        "credential_read": False,
        "network_called": False,
        "broker_called": False,
        "operational_db_written": False,
        "filesystem_written": False,
        "runtime_file_created": False,
    }


def _freshness_preflight_input_fail(reason_code: str, *, as_json: bool, out: TextIO) -> int:
    summary = {
        "outcome": "NO_GO",
        "mode": "freshness_preflight_activation_candidate",
        "receipt_sha256": None,
        "market": None,
        "symbol": None,
        "reasons": [reason_code],
        "freshness_policy_evaluated": False,
        "receipt_age_microseconds": None,
        "max_age_microseconds": None,
        "final_preflight_outcome": None,
        "final_preflight_reasons": [],
        "activation_authorized": False,
        "runtime_activation_outcome": "no_go",
        "explicit_operator_approval_required": True,
        "writers_stopped_manual_confirmation_required": True,
        "credential_read": False,
        "network_called": False,
        "broker_called": False,
        "operational_db_written": False,
        "filesystem_written": False,
        "runtime_file_created": False,
    }
    _emit(summary, as_json=as_json, out=out)
    return 1


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
    try:
        data = sys.stdin.buffer.read(_VERIFY_RECEIPT_STDIN_LIMIT + 1)
    except (OSError, ValueError):
        # ValueError: closed stdin buffer ("read of closed file") 등 — traceback 없이 fail-closed.
        return None, "receipt_input_read_error"
    if not data:
        return None, "receipt_input_empty"
    if len(data) > _VERIFY_RECEIPT_STDIN_LIMIT:
        return None, "receipt_input_too_large"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "receipt_input_not_utf8"
    try:
        return parse_receipt_stdin_json(text), None
    except ReceiptStdinJsonError as exc:
        return None, exc.reason_code


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

    # --max-age-microseconds는 freshness-qualified mode에서만 허용한다.
    if args.max_age_microseconds is not None and mode != "freshness-preflight-activation-candidate":
        return _fail("freshness_policy_argument_not_applicable", as_json=as_json, out=out)

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

    # revalidate-activation-candidate: stdin receipt + config(environ={}) — clock/DB/fs write 없음.
    if mode == "revalidate-activation-candidate":
        payload, input_error = _read_verify_stdin_payload()
        if input_error is not None:
            return _revalidate_input_fail(input_error, as_json=as_json, out=out)
        try:
            settings: AppSettings = load_settings(args.config, environ={})
        except (SettingsError, OSError) as exc:
            return _revalidate_input_fail(
                f"config error: {type(exc).__name__}", as_json=as_json, out=out
            )
        fast_loop = settings.runtime.paper_fast_loop
        try:
            result = revalidate_activation_candidate(
                settings=fast_loop,
                receipt_payload=payload,
            )
        except Exception as exc:
            return _revalidate_input_fail(
                f"revalidate error: {type(exc).__name__}", as_json=as_json, out=out
            )
        summary = _revalidate_summary(result, config_path=args.config)
        _emit(summary, as_json=as_json, out=out)
        return 0 if summary["outcome"] == "PASS" else 1

    # final-preflight-activation-candidate: stdin receipt + config(environ={}) + KST now.
    # 4g revalidation 후 현재 시각 기준 fresh precheck. DB는 read-only로만 열린다(쓰기/네트워크 없음).
    if mode == "final-preflight-activation-candidate":
        payload, input_error = _read_verify_stdin_payload()
        if input_error is not None:
            return _final_preflight_input_fail(input_error, as_json=as_json, out=out)
        try:
            settings: AppSettings = load_settings(args.config, environ={})
        except (SettingsError, OSError) as exc:
            return _final_preflight_input_fail(
                f"config error: {type(exc).__name__}", as_json=as_json, out=out
            )
        fast_loop = settings.runtime.paper_fast_loop
        try:
            result = final_preflight_activation_candidate(
                settings=fast_loop,
                receipt_payload=payload,
                now=datetime.now(tz=_KST),
            )
        except Exception as exc:
            return _final_preflight_input_fail(
                f"final-preflight error: {type(exc).__name__}", as_json=as_json, out=out
            )
        summary = _final_preflight_summary(result)
        _emit(summary, as_json=as_json, out=out)
        return 0 if summary["outcome"] == "PASS" else 1

    # freshness-preflight-activation-candidate: max-age parse → stdin → config(environ={}) → KST now.
    if mode == "freshness-preflight-activation-candidate":
        parsed_max_age, max_age_error = parse_max_age_microseconds_cli_input(
            args.max_age_microseconds
        )
        if max_age_error is not None:
            return _freshness_preflight_input_fail(max_age_error, as_json=as_json, out=out)
        assert parsed_max_age is not None
        payload, input_error = _read_verify_stdin_payload()
        if input_error is not None:
            return _freshness_preflight_input_fail(input_error, as_json=as_json, out=out)
        try:
            settings: AppSettings = load_settings(args.config, environ={})
        except (SettingsError, OSError) as exc:
            return _freshness_preflight_input_fail(
                f"config error: {type(exc).__name__}", as_json=as_json, out=out
            )
        fast_loop = settings.runtime.paper_fast_loop
        policy = ReceiptFreshnessPolicy(max_age_microseconds=parsed_max_age)
        try:
            result = freshness_qualify_activation_candidate(
                settings=fast_loop,
                receipt_payload=payload,
                now=datetime.now(tz=_KST),
                policy=policy,
            )
        except Exception as exc:
            return _freshness_preflight_input_fail(
                f"freshness-preflight error: {type(exc).__name__}", as_json=as_json, out=out
            )
        summary = _freshness_preflight_summary(result, parsed_max_age=parsed_max_age)
        _emit(summary, as_json=as_json, out=out)
        return 0 if summary["outcome"] == "PASS" else 1

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
