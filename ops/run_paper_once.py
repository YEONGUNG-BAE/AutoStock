#!/usr/bin/env python3
"""validated PaperLoopInput JSON으로 PaperLoopRunner를 수동 1회 실행한다.

Layer B paper execution only. LLM/Ollama/Scout/Allocator/Analysis/KIS 호출 없음.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from broker.paper_broker import PaperBrokerAdapter
from decision.sqlite_decision_store import SQLiteDecisionStore
from domain.enums import AccountRole, Currency
from domain.position import CashSnapshot
from ledger.sqlite_ledger import SQLiteLedger
from paper_loop import PaperLoopInput, PaperLoopRunner, PaperLoopStatus

DEFAULT_LEDGER_DB = Path("runtime/paper/ledger.sqlite3")
DEFAULT_DECISION_DB = Path("runtime/paper/decisions.sqlite3")
DEFAULT_INITIAL_CASH_KRW = "100000000"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run PaperLoopRunner once from validated PaperLoopInput JSON (Layer B only).",
    )
    parser.add_argument(
        "--validated-input",
        required=True,
        help="PaperLoopInput-compatible JSON file path",
    )
    parser.add_argument(
        "--ledger-db",
        default=str(DEFAULT_LEDGER_DB),
        help=f"SQLite ledger path (default: {DEFAULT_LEDGER_DB})",
    )
    parser.add_argument(
        "--decision-db",
        default=str(DEFAULT_DECISION_DB),
        help=f"SQLite decision store path (default: {DEFAULT_DECISION_DB})",
    )
    parser.add_argument(
        "--initial-cash-krw",
        default=DEFAULT_INITIAL_CASH_KRW,
        help=f"initial PAPER/KRW cash as Decimal string (default: {DEFAULT_INITIAL_CASH_KRW})",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="validate input only; do not open DB or call PaperLoopRunner.run()",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON summary",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print non-sensitive metadata (no raw input payload)",
    )
    return parser


def _fail(stage: str, reason: str, *, as_json: bool = False) -> int:
    payload = {
        "outcome": "FAIL",
        "stage": stage,
        "reason": reason,
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("Paper once: FAIL")
        print(f"stage: {stage}")
        print(f"reason: {reason}")
    return 1


def _input_fingerprint(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return f"len={path.stat().st_size} sha256={digest}"


def _summarize_validation_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "Pydantic validation failed"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    msg = first.get("msg", "invalid value")
    if len(errors) == 1:
        return f"{loc}: {msg}" if loc else str(msg)
    return f"{len(errors)} validation errors (first: {loc}: {msg})"


def _parse_initial_cash_krw(value: str) -> Decimal | None:
    """Decimal string → finite non-negative amount. float 변환 금지."""
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    if amount < Decimal("0"):
        return None
    return amount


def _cash_missing(ledger: SQLiteLedger) -> bool:
    return ledger.get_cash(Currency.KRW, AccountRole.PAPER) is None


def _build_summary(
    *,
    outcome: str,
    validated_input: Path,
    ledger_db: Path,
    decision_db: Path,
    initial_cash_seeded: bool,
    loop_input: PaperLoopInput | None = None,
    result_status: str | None = None,
    correlation_id: str | None = None,
    generated_order_intent_id: str | None = None,
    executable_order_intent_id: str | None = None,
    broker_status: str | None = None,
    fill_id: str | None = None,
    nav_snapshot_id: str | None = None,
    decision_snapshot_ids: list[str] | None = None,
    verbose_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "outcome": outcome,
        "validated_input": str(validated_input),
        "ledger_db": str(ledger_db),
        "decision_db": str(decision_db),
        "initial_cash_seeded": "yes" if initial_cash_seeded else "no",
    }
    if loop_input is not None:
        summary["run_id"] = loop_input.normalized_run_id.value
    if result_status is not None:
        summary["status"] = result_status
    if correlation_id is not None:
        summary["correlation_id"] = correlation_id
    if generated_order_intent_id is not None:
        summary["generated_order_intent_id"] = generated_order_intent_id
    if executable_order_intent_id is not None:
        summary["executable_order_intent_id"] = executable_order_intent_id
    if broker_status is not None:
        summary["broker_status"] = broker_status
    if fill_id is not None:
        summary["fill_id"] = fill_id
    if nav_snapshot_id is not None:
        summary["nav_snapshot_id"] = nav_snapshot_id
    if decision_snapshot_ids is not None:
        summary["decision_snapshot_ids"] = decision_snapshot_ids
    if verbose_meta:
        summary.update(verbose_meta)
    return summary


def _print_text_summary(summary: dict[str, Any]) -> None:
    outcome = summary["outcome"]
    print(f"Paper once: {outcome}")
    for key in (
        "validated_input",
        "ledger_db",
        "decision_db",
        "initial_cash_seeded",
        "run_id",
        "status",
        "correlation_id",
        "generated_order_intent_id",
        "executable_order_intent_id",
        "broker_status",
        "fill_id",
        "nav_snapshot_id",
        "decision_snapshot_ids",
    ):
        if key not in summary:
            continue
        value = summary[key]
        if key == "decision_snapshot_ids":
            print(f"{key}: {value}")
        else:
            print(f"{key}: {value}")


def _emit_summary(summary: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        _print_text_summary(summary)


def _result_summary_fields(result: Any) -> dict[str, Any]:
    generated_id = (
        result.generated_order_intent.order_id
        if result.generated_order_intent is not None
        else None
    )
    executable_id = (
        result.executable_order_intent.order_id
        if result.executable_order_intent is not None
        else None
    )
    broker_status = (
        result.broker_order_result.status.value
        if result.broker_order_result is not None
        else None
    )
    fill_id = result.fill.fill_id if result.fill is not None else None
    nav_snapshot_id = (
        result.nav_snapshot.snapshot_id
        if result.nav_snapshot is not None
        else None
    )
    decision_snapshot_ids = [item.value for item in result.decision_snapshot_ids]
    return {
        "result_status": result.status.value,
        "correlation_id": result.correlation_id,
        "generated_order_intent_id": generated_id,
        "executable_order_intent_id": executable_id,
        "broker_status": broker_status,
        "fill_id": fill_id,
        "nav_snapshot_id": nav_snapshot_id,
        "decision_snapshot_ids": decision_snapshot_ids,
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    input_path = Path(args.validated_input)
    ledger_db = Path(args.ledger_db)
    decision_db = Path(args.decision_db)
    as_json = args.json

    if args.verbose and not as_json:
        print(f"verbose: validated_input={input_path}")
        print(f"verbose: ledger_db={ledger_db}")
        print(f"verbose: decision_db={decision_db}")
        print(f"verbose: no_write={'yes' if args.no_write else 'no'}")

    if not input_path.is_file():
        return _fail("input", f"input file not found: {input_path}", as_json=as_json)

    if args.verbose and not as_json:
        print(f"verbose: input {_input_fingerprint(input_path)}")

    try:
        raw_payload = json.loads(input_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return _fail("input", f"unable to read input file: {exc}", as_json=as_json)
    except json.JSONDecodeError as exc:
        return _fail("input", f"JSON parse failure: {exc.msg}", as_json=as_json)

    try:
        loop_input = PaperLoopInput.model_validate(raw_payload)
    except ValidationError as exc:
        return _fail("validation", _summarize_validation_error(exc), as_json=as_json)

    initial_cash_amount = _parse_initial_cash_krw(args.initial_cash_krw)
    if initial_cash_amount is None:
        return _fail(
            "validation",
            f"invalid --initial-cash-krw: {args.initial_cash_krw!r}",
            as_json=as_json,
        )

    if args.no_write:
        summary = _build_summary(
            outcome="PASS",
            validated_input=input_path,
            ledger_db=ledger_db,
            decision_db=decision_db,
            initial_cash_seeded=False,
            loop_input=loop_input,
            result_status="VALIDATION_ONLY",
            correlation_id=loop_input.correlation_id,
        )
        _emit_summary(summary, as_json=as_json)
        return 0

    ledger_db.parent.mkdir(parents=True, exist_ok=True)
    decision_db.parent.mkdir(parents=True, exist_ok=True)

    ledger: SQLiteLedger | None = None
    decision_store: SQLiteDecisionStore | None = None

    try:
        try:
            ledger = SQLiteLedger(ledger_db)
            decision_store = SQLiteDecisionStore(decision_db)
        except (OSError, sqlite3.Error) as exc:
            return _fail("db", f"unable to open sqlite database: {exc}", as_json=as_json)

        existing = decision_store.get_decision_snapshot(loop_input.normalized_run_id)
        if existing is not None:
            return _fail(
                "runner",
                f"decision_id already exists: {loop_input.normalized_run_id.value}",
                as_json=as_json,
            )

        cash_missing = _cash_missing(ledger)
        initial_cash_seeded = False

        initial_cash = None
        if cash_missing:
            initial_cash = CashSnapshot(
                currency=Currency.KRW,
                amount=initial_cash_amount,
                account_role=AccountRole.PAPER,
                as_of=loop_input.created_at,
            )
            initial_cash_seeded = True

        broker = PaperBrokerAdapter(ledger, initial_cash=initial_cash)
        runner = PaperLoopRunner(
            ledger=ledger,
            decision_store=decision_store,
            broker=broker,
        )

        result = runner.run(loop_input)
        fields = _result_summary_fields(result)

        if result.status == PaperLoopStatus.VALIDATION_FAILED:
            first_issue = (
                result.validation_result.issues[0].message
                if result.validation_result.issues
                else "validation_failed"
            )
            return _fail("runner", first_issue, as_json=as_json)

        summary = _build_summary(
            outcome="PASS",
            validated_input=input_path,
            ledger_db=ledger_db,
            decision_db=decision_db,
            initial_cash_seeded=initial_cash_seeded,
            loop_input=loop_input,
            **fields,
        )
        _emit_summary(summary, as_json=as_json)
        return 0

    except Exception as exc:
        return _fail("unexpected", str(exc), as_json=as_json)

    finally:
        if ledger is not None:
            ledger.close()
        if decision_store is not None:
            decision_store.close()


if __name__ == "__main__":
    sys.exit(main())
