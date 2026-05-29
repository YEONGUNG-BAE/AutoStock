from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = REPO_ROOT / "ops" / "rehearse_paper_loop_no_write.py"
RUN_PAPER_ONCE = REPO_ROOT / "ops" / "run_paper_once.py"
EXAMPLE_CONTEXT = REPO_ROOT / "docs" / "examples" / "paper_loop_context.paper.example.json"
ACCEPTANCE_CHECK = REPO_ROOT / "ops" / "acceptance_check.sh"
RUNBOOK = REPO_ROOT / "docs" / "RUNBOOK.md"
VALIDATE_ALLOCATOR = REPO_ROOT / "ops" / "validate_allocator_raw_json.py"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from allocator.models import AllocatorDecision
from analysis.models import AnalysisDecision
from assemble_paper_loop_input import output_filenames as assemble_output_filenames, run_assemble_paper_loop_input
from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType
from paper_loop.models import PaperLoopInput
from rehearse_paper_loop_no_write import (
    RehearsalError,
    invoke_run_paper_once_no_write,
    output_filenames,
    run_rehearse_paper_loop_no_write,
)
from research_source_intake import render_date_md
from scout.models import ScoutSummary

KST = timezone(timedelta(hours=9))
KST_TS = "2026-05-28T09:00:00+09:00"
KST_CREATED = "2026-05-28T09:05:00+09:00"
MARKET = "KR"
SYMBOL = "SYNTH-KR-0001"

MANUAL_SMOKE_SCOUT: dict[str, object] = {
    "summary_id": "scout-kr-260528-1-smoke-test",
    "created_at": "2026-05-28T11:00:19.469156Z",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic manual research source for Foundation 8I rehearsal test.",
    "positive_factors": [],
    "negative_factors": [],
    "neutral_factors": [
        {
            "name": "Synthetic Smoke Test Input",
            "summary": "Synthetic manual research source.",
            "reasons": [
                {
                    "reason": "Synthetic smoke reason.",
                    "date_id": "260528-1",
                    "source_name": "operator-smoke",
                }
            ],
        }
    ],
    "metadata": {"date_ids": ["260528-1"], "foundation": "8I"},
}

VALID_ALLOCATOR_RAW: dict[str, object] = {
    "decision_id": "allocator-260528-1-smoke-test",
    "created_at": "2026-05-28T12:00:00+09:00",
    "schema_name": "allocator_decision.v1",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic allocation decision for Foundation 8I rehearsal.",
    "gold_policy_mode": "normal",
    "signal_summary": {
        "summary": "Risk regime remains balanced.",
        "reasons": [
            {"reason": "Synthetic signal basis.", "date_id": "260528-1", "source_name": "operator-smoke"}
        ],
    },
    "cash_manager": {
        "summary": "Maintain 20% cash.",
        "recommended_cash_percent": "20",
        "reasons": [
            {"reason": "Synthetic cash rationale.", "date_id": "260528-1", "source_name": "operator-smoke"}
        ],
    },
    "asset_allocator": {
        "summary": "Use balanced KR/US/Gold mix.",
        "target_weights": {"kr": "50", "us": "30", "gold": "20"},
        "reasons": [
            {
                "reason": "Synthetic asset allocation rationale.",
                "date_id": "260528-1",
                "source_name": "operator-smoke",
            }
        ],
    },
    "consistency_checker": {
        "passed": True,
        "summary": "All fields are consistent.",
        "issues": [],
        "reasons": [
            {"reason": "Consistency smoke reason.", "date_id": "260528-1", "source_name": "operator-smoke"}
        ],
    },
    "cash_policy": {
        "cash_target_percent": "20",
        "rationale": "Keep liquidity buffer.",
        "reasons": [
            {"reason": "Cash policy smoke reason.", "date_id": "260528-1", "source_name": "operator-smoke"}
        ],
    },
    "target_weights": {"kr": "50", "us": "30", "gold": "20"},
    "reasons": [
        {"reason": "Top-level synthetic rationale.", "date_id": "260528-1", "source_name": "operator-smoke"}
    ],
    "metadata": {"foundation": "8I"},
}

VALID_ANALYSIS_RAW: dict[str, object] = {
    "decision_id": "analysis-260528-1-smoke-test",
    "created_at": "2026-05-28T12:30:00+09:00",
    "schema_name": "analysis_decision.v1",
    "universe": "paper-v0",
    "symbol": SYMBOL,
    "market": MARKET,
    "summary_one_liner": "Synthetic per-symbol analysis hold for Foundation 8I rehearsal.",
    "bear": {
        "summary": "Synthetic bear view.",
        "risks": ["Synthetic demand risk"],
        "reasons": [{"reason": "Synthetic bear reason.", "date_id": "260528-1", "source_name": "operator-smoke"}],
    },
    "bull": {
        "summary": "Synthetic bull view.",
        "catalysts": ["Synthetic catalyst"],
        "reasons": [{"reason": "Synthetic bull reason.", "date_id": "260528-1", "source_name": "operator-smoke"}],
    },
    "risk_manager": {
        "summary": "Synthetic risk manager view.",
        "risk_flags": ["Synthetic flag"],
        "reasons": [{"reason": "Synthetic risk reason.", "date_id": "260528-1", "source_name": "operator-smoke"}],
    },
    "fund_manager": {
        "action": "hold",
        "target_weight_percent": "5",
        "rationale": "Maintain synthetic weight.",
        "reasons": [{"reason": "Synthetic fund reason.", "date_id": "260528-1", "source_name": "operator-smoke"}],
    },
    "reasons": [{"reason": "Synthetic top-level reason.", "date_id": "260528-1", "source_name": "operator-smoke"}],
    "metadata": {"foundation": "8I"},
}


def _sample_record(*, date_id: str = "260528-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.MANUAL,
        source_name="operator-smoke",
        source_timestamp=datetime.fromisoformat(KST_TS),
        created_at=datetime.fromisoformat(KST_CREATED),
        summary="Synthetic manual research source for Foundation 8I test.",
        payload={"note": "synthetic", "score": 1},
        symbol=SYMBOL,
        market=MARKET,
        source_url="https://example.invalid/autostock/synthetic",
    )


def _write_date_md(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    path = tmp_path / "Date.md"
    path.write_text(render_date_md(records), encoding="utf-8")
    return path


def _write_store(tmp_path: Path, *records: DateIdSourceRecord, name: str = "date_id_sources.sqlite3") -> Path:
    store_path = tmp_path / name
    if store_path.exists():
        return store_path
    store = SQLiteDateIdSourceStore(store_path)
    with store.transaction():
        for record in records:
            store.save_record(record)
    store.close()
    return store_path


def _write_context(tmp_path: Path) -> Path:
    path = tmp_path / "paper_loop_context.json"
    path.write_text(EXAMPLE_CONTEXT.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _write_scout(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "scout_output.validated.json"
    model = ScoutSummary.model_validate(payload if payload is not None else MANUAL_SMOKE_SCOUT)
    path.write_text(json.dumps(model.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def _write_allocator(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "allocator_output.validated.json"
    model = AllocatorDecision.model_validate(payload if payload is not None else VALID_ALLOCATOR_RAW)
    path.write_text(json.dumps(model.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def _write_analysis(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "analysis_output.validated.json"
    model = AnalysisDecision.model_validate(payload if payload is not None else VALID_ANALYSIS_RAW)
    path.write_text(json.dumps(model.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def _assemble_paper_loop_input(tmp_path: Path) -> tuple[Path, Path, Path]:
    """8H assembler로 valid PaperLoopInput fixture를 생성한다."""
    record = _sample_record()
    out_dir = tmp_path / "paper_loop"
    run_assemble_paper_loop_input(
        validated_scout_path=_write_scout(tmp_path),
        validated_allocator_path=_write_allocator(tmp_path),
        validated_analysis_path=_write_analysis(tmp_path),
        portfolio_state_path=None,
        paper_loop_context_path=_write_context(tmp_path),
        date_md_path=_write_date_md(tmp_path, record),
        store_path=_write_store(tmp_path, record),
        out_dir=out_dir,
        now=None,
        force=True,
    )
    input_name, _, _ = assemble_output_filenames(MARKET, SYMBOL)
    return (
        out_dir / input_name,
        _write_date_md(tmp_path, record),
        _write_store(tmp_path, record),
    )


def _rehearsal_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    paper_loop_input, date_md, store = _assemble_paper_loop_input(tmp_path)
    out_dir = tmp_path / "rehearsal"
    ledger_db = tmp_path / "ledger.sqlite3"
    decision_db = tmp_path / "decisions.sqlite3"
    return paper_loop_input, date_md, store, ledger_db, decision_db, out_dir


def _run_rehearsal(
    tmp_path: Path,
    *,
    paper_loop_input: Path | None = None,
    date_md: Path | None = None,
    store: Path | None = None,
    ledger_db: Path | None = None,
    decision_db: Path | None = None,
    out_dir: Path | None = None,
    force: bool = False,
) -> dict[str, object]:
    if paper_loop_input is None:
        pli, dm, st, ldb, ddb, od = _rehearsal_paths(tmp_path)
    else:
        pli = paper_loop_input
        dm = date_md if date_md is not None else _write_date_md(tmp_path, _sample_record())
        st = store if store is not None else _write_store(tmp_path, _sample_record())
        ldb = ledger_db if ledger_db is not None else tmp_path / "ledger.sqlite3"
        ddb = decision_db if decision_db is not None else tmp_path / "decisions.sqlite3"
        od = out_dir if out_dir is not None else tmp_path / "rehearsal"
    return run_rehearse_paper_loop_no_write(
        paper_loop_input_path=pli,
        date_md_path=dm,
        store_path=st,
        ledger_db=ldb,
        decision_db=ddb,
        out_dir=od,
        force=force,
        created_at=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
    )


def _output_files() -> tuple[str, str, str]:
    return output_filenames(MARKET, SYMBOL)


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(OPS_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def test_01_valid_rehearsal_writes_exactly_three_output_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    payload = _run_rehearsal(tmp_path, out_dir=out_dir)
    assert payload["status"] == "ok"
    assert set(out_dir.iterdir()) == {out_dir / name for name in _output_files()}


def test_02_rehearsal_machine_json_includes_no_write_invariants(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    _run_rehearsal(tmp_path, out_dir=out_dir)
    json_name, _, _ = _output_files()
    record = json.loads((out_dir / json_name).read_text(encoding="utf-8"))
    invariants = record["no_write_invariants"]
    assert invariants["no_write_required"] is True
    assert invariants["paper_loop_runner_called"] is False
    assert invariants["broker_called"] is False
    assert invariants["kis_called"] is False
    assert invariants["order_generation_run"] is False
    assert invariants["ledger_db_unchanged"] is True
    assert invariants["decision_db_unchanged"] is True
    assert invariants["execution_artifacts_created"] is False


def test_03_rehearsal_txt_includes_guard_lines(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    _run_rehearsal(tmp_path, out_dir=out_dir)
    _, txt_name, _ = _output_files()
    txt = (out_dir / txt_name).read_text(encoding="utf-8")
    assert "rehearsal_mode: no_write" in txt
    assert "PaperLoopRunner.run: NOT CALLED" in txt
    assert "PaperBroker: NOT CALLED" in txt
    assert "KIS: NOT CALLED" in txt
    assert "Order generation: NOT RUN" in txt
    assert "Ledger writes: NOT RUN" in txt
    assert "Decision snapshot writes: NOT RUN" in txt
    assert "Execution artifacts: NOT CREATED" in txt


def test_04_compact_summary_includes_validation_only_status(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    _run_rehearsal(tmp_path, out_dir=out_dir)
    _, _, summary_name = _output_files()
    summary = json.loads((out_dir / summary_name).read_text(encoding="utf-8"))
    assert summary["run_paper_once_status"] == "VALIDATION_ONLY"
    assert summary["run_paper_once_outcome"] == "PASS"


def test_05_paper_loop_input_round_trips_through_model_validate(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    _run_rehearsal(tmp_path, out_dir=out_dir)
    json_name, _, _ = _output_files()
    record = json.loads((out_dir / json_name).read_text(encoding="utf-8"))
    pli_path = Path(record["paper_loop_input"])
    raw = json.loads(pli_path.read_text(encoding="utf-8"))
    PaperLoopInput.model_validate(raw)


def test_06_no_write_missing_fails_at_args_stage() -> None:
    from rehearse_paper_loop_no_write import main

    assert main([
        "--paper-loop-input", "input.json",
        "--date-md", "Date.md",
        "--store", "store.sqlite3",
        "--out-dir", "out",
    ]) == 1


def test_07_invalid_paper_loop_input_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    pli, date_md, store, ledger_db, decision_db, _ = _rehearsal_paths(tmp_path)
    pli.write_text('{"run_id": "bad"}', encoding="utf-8")
    with pytest.raises(RehearsalError) as exc_info:
        _run_rehearsal(
            tmp_path,
            paper_loop_input=pli,
            date_md=date_md,
            store=store,
            ledger_db=ledger_db,
            decision_db=decision_db,
            out_dir=out_dir,
        )
    assert exc_info.value.stage == "input"
    assert not any((out_dir / name).exists() for name in _output_files())


def test_08_invalid_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    pli, _, store, ledger_db, decision_db, _ = _rehearsal_paths(tmp_path)
    bad_date_md = tmp_path / "Date.md"
    bad_date_md.write_text("not a valid date md", encoding="utf-8")
    with pytest.raises(RehearsalError) as exc_info:
        _run_rehearsal(
            tmp_path,
            paper_loop_input=pli,
            date_md=bad_date_md,
            store=store,
            ledger_db=ledger_db,
            decision_db=decision_db,
            out_dir=out_dir,
        )
    assert exc_info.value.stage == "date_md"
    assert not any((out_dir / name).exists() for name in _output_files())


def test_09_date_md_store_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    pli, _, _, ledger_db, decision_db, _ = _rehearsal_paths(tmp_path)
    record = _sample_record()
    extra = _sample_record(date_id="260528-2")
    with pytest.raises(RehearsalError, match="Date.md date_id missing from store"):
        _run_rehearsal(
            tmp_path,
            paper_loop_input=pli,
            date_md=_write_date_md(tmp_path, record, extra),
            store=_write_store(tmp_path, record),
            ledger_db=ledger_db,
            decision_db=decision_db,
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_10_scout_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    record = _sample_record()
    only_other = _sample_record(date_id="260528-9")
    assemble_out = tmp_path / "paper_loop"
    run_assemble_paper_loop_input(
        validated_scout_path=_write_scout(tmp_path),
        validated_allocator_path=_write_allocator(tmp_path),
        validated_analysis_path=_write_analysis(tmp_path),
        portfolio_state_path=None,
        paper_loop_context_path=_write_context(tmp_path),
        date_md_path=_write_date_md(tmp_path, record),
        store_path=_write_store(tmp_path, record),
        out_dir=assemble_out,
        now=None,
        force=False,
    )
    input_name, _, _ = assemble_output_filenames(MARKET, SYMBOL)
    with pytest.raises(RehearsalError, match="ScoutSummary cited date_id missing from Date.md"):
        _run_rehearsal(
            tmp_path,
            paper_loop_input=assemble_out / input_name,
            date_md=_write_date_md(tmp_path, only_other),
            store=_write_store(tmp_path, only_other, name="store_only.sqlite3"),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_11_allocator_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    record = _sample_record()
    assemble_out = tmp_path / "paper_loop"
    run_assemble_paper_loop_input(
        validated_scout_path=_write_scout(tmp_path),
        validated_allocator_path=_write_allocator(tmp_path),
        validated_analysis_path=_write_analysis(tmp_path),
        portfolio_state_path=None,
        paper_loop_context_path=_write_context(tmp_path),
        date_md_path=_write_date_md(tmp_path, record),
        store_path=_write_store(tmp_path, record),
        out_dir=assemble_out,
        now=None,
        force=True,
    )
    input_name, _, _ = assemble_output_filenames(MARKET, SYMBOL)
    input_path = assemble_out / input_name
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    raw["allocator_decision"]["reasons"] = [
        {"reason": "Other id.", "date_id": "260528-9", "source_name": "operator-smoke"}
    ]
    input_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RehearsalError, match="AllocatorDecision cited date_id missing from Date.md"):
        _run_rehearsal(
            tmp_path,
            paper_loop_input=input_path,
            date_md=_write_date_md(tmp_path, record),
            store=_write_store(tmp_path, record, name="store_alloc.sqlite3"),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_12_analysis_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    record = _sample_record()
    assemble_out = tmp_path / "paper_loop"
    run_assemble_paper_loop_input(
        validated_scout_path=_write_scout(tmp_path),
        validated_allocator_path=_write_allocator(tmp_path),
        validated_analysis_path=_write_analysis(tmp_path),
        portfolio_state_path=None,
        paper_loop_context_path=_write_context(tmp_path),
        date_md_path=_write_date_md(tmp_path, record),
        store_path=_write_store(tmp_path, record),
        out_dir=assemble_out,
        now=None,
        force=True,
    )
    input_name, _, _ = assemble_output_filenames(MARKET, SYMBOL)
    input_path = assemble_out / input_name
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    raw["analysis_decision"]["reasons"] = [
        {"reason": "Other id.", "date_id": "260528-9", "source_name": "operator-smoke"}
    ]
    input_path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(RehearsalError, match="AnalysisDecision cited date_id missing from Date.md"):
        _run_rehearsal(
            tmp_path,
            paper_loop_input=input_path,
            date_md=_write_date_md(tmp_path, record),
            store=_write_store(tmp_path, record, name="store_analysis.sqlite3"),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_13_run_paper_once_non_zero_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    pli, date_md, store, ledger_db, decision_db, _ = _rehearsal_paths(tmp_path)

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")

    with patch("rehearse_paper_loop_no_write.subprocess.run", side_effect=_fake_run):
        with pytest.raises(RehearsalError, match="run_paper_once --no-write failed"):
            run_rehearse_paper_loop_no_write(
                paper_loop_input_path=pli,
                date_md_path=date_md,
                store_path=store,
                ledger_db=ledger_db,
                decision_db=decision_db,
                out_dir=out_dir,
                force=False,
            )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_14_run_paper_once_malformed_json_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    pli, date_md, store, ledger_db, decision_db, _ = _rehearsal_paths(tmp_path)

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="not-json", stderr="")

    with patch("rehearse_paper_loop_no_write.subprocess.run", side_effect=_fake_run):
        with pytest.raises(RehearsalError, match="not valid JSON"):
            run_rehearse_paper_loop_no_write(
                paper_loop_input_path=pli,
                date_md_path=date_md,
                store_path=store,
                ledger_db=ledger_db,
                decision_db=decision_db,
                out_dir=out_dir,
                force=False,
            )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_15_run_paper_once_missing_validation_only_status_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    pli, date_md, store, ledger_db, decision_db, _ = _rehearsal_paths(tmp_path)
    bad_summary = json.dumps({"outcome": "PASS", "status": "EXECUTED"})

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=bad_summary, stderr="")

    with patch("rehearse_paper_loop_no_write.subprocess.run", side_effect=_fake_run):
        with pytest.raises(RehearsalError, match="status is not VALIDATION_ONLY"):
            run_rehearse_paper_loop_no_write(
                paper_loop_input_path=pli,
                date_md_path=date_md,
                store_path=store,
                ledger_db=ledger_db,
                decision_db=decision_db,
                out_dir=out_dir,
                force=False,
            )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_16_ledger_db_absent_before_remains_absent_after(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    ledger_db = tmp_path / "missing_ledger.sqlite3"
    assert not ledger_db.exists()
    _run_rehearsal(tmp_path, out_dir=out_dir, ledger_db=ledger_db)
    assert not ledger_db.exists()


def test_17_decision_db_absent_before_remains_absent_after(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    decision_db = tmp_path / "missing_decisions.sqlite3"
    assert not decision_db.exists()
    _run_rehearsal(tmp_path, out_dir=out_dir, decision_db=decision_db)
    assert not decision_db.exists()


def test_18_existing_ledger_db_hash_unchanged_after_rehearsal(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    ledger_db = tmp_path / "ledger.sqlite3"
    ledger_db.write_bytes(b"ledger-bytes")
    before = hashlib.sha256(ledger_db.read_bytes()).hexdigest()
    _run_rehearsal(tmp_path, out_dir=out_dir, ledger_db=ledger_db)
    after = hashlib.sha256(ledger_db.read_bytes()).hexdigest()
    assert before == after


def test_19_existing_decision_db_hash_unchanged_after_rehearsal(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    decision_db = tmp_path / "decisions.sqlite3"
    decision_db.write_bytes(b"decision-bytes")
    before = hashlib.sha256(decision_db.read_bytes()).hexdigest()
    _run_rehearsal(tmp_path, out_dir=out_dir, decision_db=decision_db)
    after = hashlib.sha256(decision_db.read_bytes()).hexdigest()
    assert before == after


def test_20_existing_output_files_fail_without_force_before_run_paper_once(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    pli, date_md, store, ledger_db, decision_db, _ = _rehearsal_paths(tmp_path)
    json_name, _, _ = _output_files()
    out_dir.mkdir(parents=True)
    (out_dir / json_name).write_text("{}", encoding="utf-8")

    call_count = {"n": 0}
    original = subprocess.run

    def _counting_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        call_count["n"] += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    with patch("rehearse_paper_loop_no_write.subprocess.run", side_effect=_counting_run):
        with pytest.raises(RehearsalError, match="output files already exist") as exc_info:
            run_rehearse_paper_loop_no_write(
                paper_loop_input_path=pli,
                date_md_path=date_md,
                store_path=store,
                ledger_db=ledger_db,
                decision_db=decision_db,
                out_dir=out_dir,
                force=False,
            )
    assert exc_info.value.stage == "write"
    assert call_count["n"] == 0


def test_21_force_overwrites_expected_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    json_name, _, _ = _output_files()
    _run_rehearsal(tmp_path, out_dir=out_dir)
    (out_dir / json_name).write_text("{}", encoding="utf-8")
    payload = _run_rehearsal(tmp_path, out_dir=out_dir, force=True)
    assert payload["status"] == "ok"
    record = json.loads((out_dir / json_name).read_text(encoding="utf-8"))
    assert record["status"] == "ok"


def test_22_json_output_is_parseable_and_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pli, date_md, store, ledger_db, decision_db, out_dir = _rehearsal_paths(tmp_path)
    from rehearse_paper_loop_no_write import main

    argv = [
        "--paper-loop-input", str(pli),
        "--date-md", str(date_md),
        "--store", str(store),
        "--ledger-db", str(ledger_db),
        "--decision-db", str(decision_db),
        "--out-dir", str(out_dir),
        "--no-write",
        "--json",
    ]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "ok"
    assert "run_paper_once_summary" not in payload


def test_23_json_verbose_keeps_stdout_pure_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pli, date_md, store, ledger_db, decision_db, out_dir = _rehearsal_paths(tmp_path)
    from rehearse_paper_loop_no_write import main

    argv = [
        "--paper-loop-input", str(pli),
        "--date-md", str(date_md),
        "--store", str(store),
        "--ledger-db", str(ledger_db),
        "--decision-db", str(decision_db),
        "--out-dir", str(out_dir),
        "--no-write",
        "--json",
        "--verbose",
    ]
    assert main(argv) == 0
    captured = capsys.readouterr()
    json.loads(captured.out.strip())
    assert "verbose:" in captured.err


def test_24_script_help_exits_zero() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0


def test_25_script_does_not_import_forbidden_modules() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    forbidden = (
        "ollama",
        "openai",
        "httpx",
        "requests",
        "aiohttp",
        "yfinance",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
        "orderintentgenerator",
        "quantityresolver",
    )
    for token in forbidden:
        assert token not in source


def test_26_script_does_not_create_execution_artifacts(tmp_path: Path) -> None:
    out_dir = tmp_path / "rehearsal"
    _run_rehearsal(tmp_path, out_dir=out_dir)
    names = {path.name for path in out_dir.iterdir()}
    assert names == set(_output_files())


def test_27_subprocess_invokes_run_paper_once_with_required_args(tmp_path: Path) -> None:
    pli, date_md, store, ledger_db, decision_db, out_dir = _rehearsal_paths(tmp_path)
    captured: dict[str, object] = {}

    def _capture_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"outcome": "PASS", "status": "VALIDATION_ONLY"}),
            stderr="",
        )

    with patch("rehearse_paper_loop_no_write.subprocess.run", side_effect=_capture_run):
        run_rehearse_paper_loop_no_write(
            paper_loop_input_path=pli,
            date_md_path=date_md,
            store_path=store,
            ledger_db=ledger_db,
            decision_db=decision_db,
            out_dir=out_dir,
            force=False,
        )

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert str(RUN_PAPER_ONCE) in cmd
    assert "--no-write" in cmd
    assert "--json" in cmd
    assert "--validated-input" in cmd
    assert captured["cwd"] == REPO_ROOT
    env = captured["env"]
    assert isinstance(env, dict)
    pythonpath = env.get("PYTHONPATH", "")
    assert str(REPO_ROOT / "src") in pythonpath.split(os.pathsep)


def test_28_happy_path_exercises_actual_subprocess_to_run_paper_once(tmp_path: Path) -> None:
    """실제 subprocess로 run_paper_once --no-write --json 경로를 검증한다."""
    pli, date_md, store, ledger_db, decision_db, out_dir = _rehearsal_paths(tmp_path)
    summary = invoke_run_paper_once_no_write(
        paper_loop_input=pli,
        ledger_db=ledger_db,
        decision_db=decision_db,
        repo_root=REPO_ROOT,
        src_path=REPO_ROOT / "src",
        run_paper_once_path=RUN_PAPER_ONCE,
    )
    assert summary["outcome"] == "PASS"
    assert summary["status"] == "VALIDATION_ONLY"
    payload = run_rehearse_paper_loop_no_write(
        paper_loop_input_path=pli,
        date_md_path=date_md,
        store_path=store,
        ledger_db=ledger_db,
        decision_db=decision_db,
        out_dir=out_dir,
        force=False,
    )
    assert payload["run_paper_once_status"] == "VALIDATION_ONLY"


def test_29_static_guard_only_inspects_rehearse_script_not_run_paper_once() -> None:
    rehearse_source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    run_once_source = RUN_PAPER_ONCE.read_text(encoding="utf-8").lower()
    assert "paperlooprunner" not in rehearse_source
    assert "paperbroker" not in rehearse_source
    assert "paperlooprunner" in run_once_source
    assert "paperbroker" in run_once_source


def test_30_pytest_baseline_synchronized_between_runbook_and_acceptance_check() -> None:
    acceptance_text = ACCEPTANCE_CHECK.read_text(encoding="utf-8")
    runbook_text = RUNBOOK.read_text(encoding="utf-8")

    acceptance_match = re.search(r'grep -q "(\d+) passed"', acceptance_text)
    assert acceptance_match is not None

    baseline = acceptance_match.group(1)
    assert f"pytest: {baseline} passed" in acceptance_text
    assert f"pytest baseline mismatch(`{baseline} passed`" in runbook_text
    assert f"**pytest baseline:** `{baseline} passed`" in runbook_text

    runbook_counts = re.findall(r"(\d+) passed", runbook_text)
    acceptance_counts = re.findall(r"(\d+) passed", acceptance_text)
    assert len(set(runbook_counts)) == 1
    assert len(set(acceptance_counts)) == 1
    assert runbook_counts[0] == acceptance_counts[0] == baseline


def test_31_8f_p1_preflight_store_required_and_finally_close() -> None:
    source = VALIDATE_ALLOCATOR.read_text(encoding="utf-8")
    assert 'parser.add_argument("--store", required=True' in source
    assert "AllocatorDecisionValidator" in source
    assert "finally:" in source
    assert "store.close()" in source
