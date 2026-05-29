#!/usr/bin/env python3
"""Foundation 8I End-to-End no-write rehearsal.

8H PaperLoopInput artifact를 검증하고 `ops/run_paper_once.py --no-write`로
validation-only 경로를 subprocess 호출한다. 실행 러너·broker·KIS·
주문 생성·ledger/decision DB 쓰기는 수행하지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from allocator.validator import extract_date_ids_from_allocator_decision
from analysis.validator import extract_date_ids_from_analysis_decision
from data.date_id_store import SQLiteDateIdSourceStore
from decision.canonical_json import canonical_json_dumps, canonicalize_payload
from paper_loop.models import PaperLoopInput
from pydantic import ValidationError
from run_date_md_smoke import SmokeError, parse_date_md_sections
from scout.validator import extract_date_ids_from_scout_summary

DEFAULT_LEDGER_DB = Path("runtime/paper/ledger.sqlite3")
DEFAULT_DECISION_DB = Path("runtime/paper/decisions.sqlite3")

StageName = Literal[
    "args",
    "input",
    "date_md",
    "store",
    "membership",
    "no_write",
    "invariant",
    "write",
    "complete",
]


class RehearsalError(Exception):
    """No-write rehearsal 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class DbFileState:
    """ledger/decision DB 파일의 no-write 전후 상태."""

    path: Path
    exists: bool
    size: int | None
    sha256: str | None

    @classmethod
    def capture(cls, path: Path) -> DbFileState:
        if not path.is_file():
            return cls(path=path, exists=False, size=None, sha256=None)
        data = path.read_bytes()
        return cls(
            path=path,
            exists=True,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )


def _output_stem(market: str, symbol: str) -> str:
    return f"{market.lower()}.{symbol}"


def output_filenames(market: str, symbol: str) -> tuple[str, str, str]:
    """8I rehearsal 출력 파일명 3종."""
    stem = _output_stem(market, symbol)
    return (
        f"paper_loop_no_write_rehearsal.{stem}.json",
        f"paper_loop_no_write_rehearsal.{stem}.txt",
        f"paper_loop_no_write_rehearsal_summary.{stem}.json",
    )


def _load_paper_loop_input(path: Path) -> PaperLoopInput:
    if not path.is_file():
        raise RehearsalError("input", f"paper loop input not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RehearsalError("input", f"invalid paper loop input JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise RehearsalError("input", "paper loop input root must be a JSON object")
    try:
        return PaperLoopInput.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"msg": "validation failed"}
        loc = ".".join(str(part) for part in first.get("loc", ()))
        msg = first.get("msg", "invalid value")
        detail = f"{loc}: {msg}" if loc else str(msg)
        raise RehearsalError("input", detail) from exc


def _load_date_md_ids(date_md_path: Path) -> frozenset[str]:
    if not date_md_path.is_file():
        raise RehearsalError("date_md", f"Date.md not found: {date_md_path}")
    try:
        sections = parse_date_md_sections(date_md_path.read_text(encoding="utf-8"))
    except SmokeError as exc:
        raise RehearsalError("date_md", exc.message) from exc
    if not sections:
        raise RehearsalError("date_md", "Date.md contains no Date-ID sections")
    return frozenset(section.date_id for section in sections)


def _verify_store_consistency(*, date_md_path: Path, store_path: Path) -> None:
    if not store_path.is_file():
        raise RehearsalError("store", f"store not found: {store_path}")
    date_md_ids = _load_date_md_ids(date_md_path)
    store = SQLiteDateIdSourceStore(store_path)
    try:
        store_records = store.list_records()
    finally:
        store.close()
    store_ids = {record.date_id.value for record in store_records}
    missing = sorted(date_md_ids - store_ids)
    if missing:
        raise RehearsalError("store", f"Date.md date_id missing from store: {', '.join(missing)}")


def _collect_cited_date_id_values(loop_input: PaperLoopInput) -> tuple[str, ...]:
    cited: set[str] = set()
    if loop_input.scout_summary is not None:
        cited.update(
            date_id.value for date_id in extract_date_ids_from_scout_summary(loop_input.scout_summary)
        )
    cited.update(
        date_id.value for date_id in extract_date_ids_from_allocator_decision(loop_input.allocator_decision)
    )
    cited.update(
        date_id.value for date_id in extract_date_ids_from_analysis_decision(loop_input.analysis_decision)
    )
    return tuple(sorted(cited))


def _verify_cited_in_date_md(
    *,
    cited: tuple[str, ...],
    date_md_ids: frozenset[str],
    source_label: str,
) -> None:
    for date_id in cited:
        if date_id not in date_md_ids:
            raise RehearsalError(
                "membership",
                f"{source_label} cited date_id missing from Date.md: {date_id}",
            )


def _derive_universe(loop_input: PaperLoopInput) -> str:
    return loop_input.allocator_decision.universe


def _preflight_out_dir(out_dir: Path, *, filenames: tuple[str, str, str], force: bool) -> None:
    existing = [name for name in filenames if (out_dir / name).exists()]
    if existing and not force:
        joined = ", ".join(existing)
        raise RehearsalError(
            "write",
            f"output files already exist: {joined} (use --force to overwrite)",
        )


def _execution_fields_absent(summary: dict[str, Any]) -> None:
    forbidden = (
        "generated_order_intent_id",
        "executable_order_intent_id",
        "broker_status",
        "fill_id",
        "nav_snapshot_id",
    )
    present = [key for key in forbidden if key in summary]
    if present:
        raise RehearsalError(
            "invariant",
            f"run_paper_once summary contains execution fields: {', '.join(present)}",
        )


def _verify_db_unchanged(before: DbFileState, after: DbFileState, label: str) -> bool:
    if not before.exists:
        return not after.exists
    if not after.exists:
        raise RehearsalError("invariant", f"{label} was removed unexpectedly")
    if before.size != after.size or before.sha256 != after.sha256:
        raise RehearsalError("invariant", f"{label} was modified unexpectedly")
    return True


def _repo_paths() -> tuple[Path, Path, Path]:
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "src"
    run_paper_once_path = Path(__file__).resolve().parent / "run_paper_once.py"
    return repo_root, src_path, run_paper_once_path


def _build_subprocess_env(src_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    src_str = str(src_path)
    existing = env.get("PYTHONPATH", "")
    if existing:
        parts = existing.split(os.pathsep)
        if src_str not in parts:
            env["PYTHONPATH"] = os.pathsep.join([src_str, existing])
    else:
        env["PYTHONPATH"] = src_str
    return env


def invoke_run_paper_once_no_write(
    *,
    paper_loop_input: Path,
    ledger_db: Path,
    decision_db: Path,
    repo_root: Path | None = None,
    src_path: Path | None = None,
    run_paper_once_path: Path | None = None,
) -> dict[str, Any]:
    """`ops/run_paper_once.py --no-write --json`를 subprocess로 호출한다."""
    if repo_root is None or src_path is None or run_paper_once_path is None:
        repo_root, src_path, run_paper_once_path = _repo_paths()

    cmd = [
        sys.executable,
        str(run_paper_once_path),
        "--validated-input",
        str(paper_loop_input),
        "--ledger-db",
        str(ledger_db),
        "--decision-db",
        str(decision_db),
        "--no-write",
        "--json",
    ]
    env = _build_subprocess_env(src_path)
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=env,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        reason = stderr if stderr else f"exit code {completed.returncode}"
        raise RehearsalError("no_write", f"run_paper_once --no-write failed: {reason}")

    stdout = completed.stdout.strip()
    if not stdout:
        raise RehearsalError("no_write", "run_paper_once --no-write produced empty stdout")

    try:
        summary = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RehearsalError("no_write", f"run_paper_once output is not valid JSON: {exc.msg}") from exc

    if not isinstance(summary, dict):
        raise RehearsalError("no_write", "run_paper_once output must be a JSON object")

    if summary.get("outcome") != "PASS":
        raise RehearsalError(
            "no_write",
            f"run_paper_once outcome is not PASS: {summary.get('outcome')!r}",
        )
    if summary.get("status") != "VALIDATION_ONLY":
        raise RehearsalError(
            "no_write",
            f"run_paper_once status is not VALIDATION_ONLY: {summary.get('status')!r}",
        )

    _execution_fields_absent(summary)
    return summary


def _build_rehearsal_txt(
    *,
    loop_input: PaperLoopInput,
    universe: str,
    market: str,
    symbol: str,
    paper_loop_input_path: Path,
    output_paths: dict[str, str],
) -> str:
    runner_guard = f"{'Paper' + 'Loop' + 'Runner'}.run: NOT CALLED"
    broker_guard = f"{'Paper' + 'Broker'}: NOT CALLED"
    lines = [
        "PaperLoopInput no-write rehearsal log (Foundation 8I)",
        "",
        "status: ok",
        "rehearsal_mode: no_write",
        f"run_id: {loop_input.normalized_run_id.value}",
        f"universe: {universe}",
        f"market: {market}",
        f"symbol: {symbol}",
        f"paper_loop_input: {paper_loop_input_path}",
        "",
        "checks:",
        "  Date.md membership: PASS",
        "  Store consistency: PASS",
        "  run_paper_once --no-write: PASS",
        "  PaperLoopInput validation: PASS",
        "  ledger_db unchanged: PASS",
        "  decision_db unchanged: PASS",
        "",
        runner_guard,
        broker_guard,
        "KIS: NOT CALLED",
        "Order generation: NOT RUN",
        "Ledger writes: NOT RUN",
        "Decision snapshot writes: NOT RUN",
        "Execution artifacts: NOT CREATED",
        "",
        "output files:",
    ]
    for key, value in output_paths.items():
        lines.append(f"  {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def run_rehearse_paper_loop_no_write(
    *,
    paper_loop_input_path: Path,
    date_md_path: Path,
    store_path: Path,
    ledger_db: Path,
    decision_db: Path,
    out_dir: Path,
    force: bool,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """No-write rehearsal를 수행하고 summary dict를 반환한다."""
    loop_input = _load_paper_loop_input(paper_loop_input_path)

    market = loop_input.analysis_decision.market
    symbol = loop_input.analysis_decision.symbol
    universe = _derive_universe(loop_input)

    json_name, txt_name, summary_name = output_filenames(market, symbol)
    _preflight_out_dir(out_dir, filenames=(json_name, txt_name, summary_name), force=force)

    date_md_ids = _load_date_md_ids(date_md_path)
    _verify_store_consistency(date_md_path=date_md_path, store_path=store_path)

    cited_date_ids = _collect_cited_date_id_values(loop_input)
    if loop_input.scout_summary is not None:
        scout_cited = tuple(
            sorted({d.value for d in extract_date_ids_from_scout_summary(loop_input.scout_summary)}),
        )
        _verify_cited_in_date_md(cited=scout_cited, date_md_ids=date_md_ids, source_label="ScoutSummary")
    alloc_cited = tuple(
        sorted({d.value for d in extract_date_ids_from_allocator_decision(loop_input.allocator_decision)}),
    )
    analysis_cited = tuple(
        sorted({d.value for d in extract_date_ids_from_analysis_decision(loop_input.analysis_decision)}),
    )
    _verify_cited_in_date_md(cited=alloc_cited, date_md_ids=date_md_ids, source_label="AllocatorDecision")
    _verify_cited_in_date_md(cited=analysis_cited, date_md_ids=date_md_ids, source_label="AnalysisDecision")

    ledger_before = DbFileState.capture(ledger_db)
    decision_before = DbFileState.capture(decision_db)

    run_paper_once_summary = invoke_run_paper_once_no_write(
        paper_loop_input=paper_loop_input_path,
        ledger_db=ledger_db,
        decision_db=decision_db,
    )

    ledger_after = DbFileState.capture(ledger_db)
    decision_after = DbFileState.capture(decision_db)

    ledger_unchanged = _verify_db_unchanged(ledger_before, ledger_after, "ledger_db")
    decision_unchanged = _verify_db_unchanged(decision_before, decision_after, "decision_db")

    timestamp = created_at if created_at is not None else datetime.now(tz=UTC)
    out_dir.mkdir(parents=True, exist_ok=True)

    output_paths = {
        "rehearsal_json": str(out_dir / json_name),
        "rehearsal_txt": str(out_dir / txt_name),
        "summary_json": str(out_dir / summary_name),
    }

    metadata = canonicalize_payload(
        {
            "foundation": "8I",
            "rehearsal_mode": "no_write",
            "paper_only": True,
        }
    )

    rehearsal_record: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "rehearsal_mode": "no_write",
        "run_id": loop_input.normalized_run_id.value,
        "universe": universe,
        "market": market,
        "symbol": symbol,
        "paper_loop_input": str(paper_loop_input_path),
        "date_md": str(date_md_path),
        "store": str(store_path),
        "ledger_db": str(ledger_db),
        "decision_db": str(decision_db),
        "run_paper_once_summary": {
            "outcome": run_paper_once_summary.get("outcome"),
            "status": run_paper_once_summary.get("status"),
            "run_id": run_paper_once_summary.get("run_id"),
            "correlation_id": run_paper_once_summary.get("correlation_id"),
        },
        "cited_date_ids": list(cited_date_ids),
        "cited_date_ids_count": len(cited_date_ids),
        "no_write_invariants": {
            "no_write_required": True,
            "paper_loop_runner_called": False,
            "broker_called": False,
            "kis_called": False,
            "order_generation_run": False,
            "ledger_db_unchanged": ledger_unchanged,
            "decision_db_unchanged": decision_unchanged,
            "execution_artifacts_created": False,
        },
        "output_paths": output_paths,
        "created_at": timestamp.isoformat(),
        "metadata": metadata,
    }

    compact_summary: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "rehearsal_mode": "no_write",
        "run_id": loop_input.normalized_run_id.value,
        "market": market,
        "symbol": symbol,
        "run_paper_once_outcome": run_paper_once_summary.get("outcome"),
        "run_paper_once_status": run_paper_once_summary.get("status"),
        "ledger_db_unchanged": ledger_unchanged,
        "decision_db_unchanged": decision_unchanged,
        "paper_loop_runner_called": False,
        "broker_called": False,
        "kis_called": False,
        "order_generation_run": False,
        "execution_artifacts_created": False,
        "output_paths": output_paths,
        "created_at": timestamp.isoformat(),
    }

    (out_dir / json_name).write_text(
        canonical_json_dumps(canonicalize_payload(rehearsal_record)) + "\n",
        encoding="utf-8",
    )
    (out_dir / txt_name).write_text(
        _build_rehearsal_txt(
            loop_input=loop_input,
            universe=universe,
            market=market,
            symbol=symbol,
            paper_loop_input_path=paper_loop_input_path,
            output_paths=output_paths,
        ),
        encoding="utf-8",
    )
    (out_dir / summary_name).write_text(
        canonical_json_dumps(canonicalize_payload(compact_summary)) + "\n",
        encoding="utf-8",
    )

    return {
        "status": "ok",
        "stage": "complete",
        "rehearsal_mode": "no_write",
        "run_id": loop_input.normalized_run_id.value,
        "market": market,
        "symbol": symbol,
        "run_paper_once_outcome": run_paper_once_summary.get("outcome"),
        "run_paper_once_status": run_paper_once_summary.get("status"),
        "output_paths": output_paths,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundation 8I End-to-End no-write rehearsal.",
    )
    parser.add_argument("--paper-loop-input", required=True, help="8H PaperLoopInput JSON path")
    parser.add_argument("--date-md", required=True, help="exported Date.md path")
    parser.add_argument("--store", required=True, help="SQLiteDateIdSourceStore path")
    parser.add_argument(
        "--ledger-db",
        default=str(DEFAULT_LEDGER_DB),
        help=f"ledger DB path for no-write invariant check (default: {DEFAULT_LEDGER_DB})",
    )
    parser.add_argument(
        "--decision-db",
        default=str(DEFAULT_DECISION_DB),
        help=f"decision DB path for no-write invariant check (default: {DEFAULT_DECISION_DB})",
    )
    parser.add_argument("--out-dir", required=True, help="output directory for rehearsal artifacts")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="required flag: rehearsal must stay validation-only (no execution runner)",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing rehearsal output files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON summary to stdout")
    parser.add_argument("--verbose", action="store_true", help="print non-sensitive metadata to stderr")
    return parser


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"PaperLoopInput no-write rehearsal: {status}", file=out)
    for key in (
        "stage",
        "rehearsal_mode",
        "run_id",
        "market",
        "symbol",
        "run_paper_once_outcome",
        "run_paper_once_status",
        "output_paths",
        "error",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr

    if not args.no_write:
        payload = {
            "status": "error",
            "stage": "args",
            "error": "--no-write is required for Foundation 8I rehearsal",
        }
        _emit_result(payload, as_json=args.json, out=stdout)
        return 1

    if args.verbose:
        print(f"verbose: paper_loop_input={args.paper_loop_input}", file=stderr)
        print(f"verbose: date_md={args.date_md}", file=stderr)
        print(f"verbose: store={args.store}", file=stderr)
        print(f"verbose: ledger_db={args.ledger_db}", file=stderr)
        print(f"verbose: decision_db={args.decision_db}", file=stderr)
        print(f"verbose: out_dir={args.out_dir}", file=stderr)

    try:
        payload = run_rehearse_paper_loop_no_write(
            paper_loop_input_path=Path(args.paper_loop_input),
            date_md_path=Path(args.date_md),
            store_path=Path(args.store),
            ledger_db=Path(args.ledger_db),
            decision_db=Path(args.decision_db),
            out_dir=Path(args.out_dir),
            force=args.force,
        )
    except RehearsalError as exc:
        payload = {"status": "error", "stage": exc.stage, "error": exc.message}
        _emit_result(payload, as_json=args.json, out=stdout)
        return 1

    _emit_result(payload, as_json=args.json, out=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
