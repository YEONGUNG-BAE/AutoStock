from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = REPO_ROOT / "ops" / "assemble_paper_loop_input.py"
EXAMPLE_CONTEXT = REPO_ROOT / "docs" / "examples" / "paper_loop_context.paper.example.json"
EXAMPLE_PORTFOLIO = REPO_ROOT / "docs" / "examples" / "portfolio_state.paper.example.json"
ACCEPTANCE_CHECK = REPO_ROOT / "ops" / "acceptance_check.sh"
RUNBOOK = REPO_ROOT / "docs" / "RUNBOOK.md"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from allocator.models import AllocatorDecision
from analysis.models import AnalysisDecision
from assemble_paper_loop_input import (
    AssemblyError,
    load_paper_loop_context,
    output_filenames,
    run_assemble_paper_loop_input,
)
from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType
from domain.enums import AccountRole
from domain.market import MarketPrice
from paper_loop.models import PaperLoopInput
from research_source_intake import render_date_md
from risk.models import RiskFilterContext
from scout.models import ScoutSummary

KST = timezone(timedelta(hours=9))
KST_TS = "2026-05-28T09:00:00+09:00"
KST_CREATED = "2026-05-28T09:05:00+09:00"
NOW = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
MARKET = "KR"
SYMBOL = "SYNTH-KR-0001"

MANUAL_SMOKE_SCOUT: dict[str, object] = {
    "summary_id": "scout-kr-260528-1-smoke-test",
    "created_at": "2026-05-28T11:00:19.469156Z",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic manual research source for Foundation 8H smoke test.",
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
    "metadata": {"date_ids": ["260528-1"], "foundation": "8H"},
}

VALID_ALLOCATOR_RAW: dict[str, object] = {
    "decision_id": "allocator-260528-1-smoke-test",
    "created_at": "2026-05-28T12:00:00+09:00",
    "schema_name": "allocator_decision.v1",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic allocation decision for Foundation 8H assembler smoke.",
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
    "metadata": {"foundation": "8H"},
}

VALID_ANALYSIS_RAW: dict[str, object] = {
    "decision_id": "analysis-260528-1-smoke-test",
    "created_at": "2026-05-28T12:30:00+09:00",
    "schema_name": "analysis_decision.v1",
    "universe": "paper-v0",
    "symbol": SYMBOL,
    "market": MARKET,
    "summary_one_liner": "Synthetic per-symbol analysis hold for Foundation 8H smoke.",
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
    "metadata": {"foundation": "8H"},
}


def _sample_record(*, date_id: str = "260528-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.MANUAL,
        source_name="operator-smoke",
        source_timestamp=datetime.fromisoformat(KST_TS),
        created_at=datetime.fromisoformat(KST_CREATED),
        summary="Synthetic manual research source for Foundation 8H test.",
        payload={"note": "synthetic", "score": 1},
        symbol=SYMBOL,
        market=MARKET,
        source_url="https://example.invalid/autostock/synthetic",
    )


def _write_date_md(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    path = tmp_path / "Date.md"
    path.write_text(render_date_md(records), encoding="utf-8")
    return path


def _write_store(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    store_path = tmp_path / "date_id_sources.sqlite3"
    if store_path.exists():
        return store_path
    store = SQLiteDateIdSourceStore(store_path)
    with store.transaction():
        for record in records:
            store.save_record(record)
    store.close()
    return store_path


def _write_context(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "paper_loop_context.json"
    if payload is None:
        path.write_text(EXAMPLE_CONTEXT.read_text(encoding="utf-8"), encoding="utf-8")
        return path
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
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


def _output_files() -> tuple[str, str, str]:
    return output_filenames(MARKET, SYMBOL)


def _assemble(
    tmp_path: Path,
    *,
    validated_scout_path: Path | None = None,
    validated_allocator_path: Path | None = None,
    validated_analysis_path: Path | None = None,
    paper_loop_context_path: Path | None = None,
    portfolio_state_path: Path | None = None,
    date_md_path: Path | None = None,
    store_path: Path | None = None,
    out_dir: Path | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    record = _sample_record()
    return run_assemble_paper_loop_input(
        validated_scout_path=validated_scout_path if validated_scout_path is not None else _write_scout(tmp_path),
        validated_allocator_path=validated_allocator_path or _write_allocator(tmp_path),
        validated_analysis_path=validated_analysis_path or _write_analysis(tmp_path),
        portfolio_state_path=portfolio_state_path,
        paper_loop_context_path=paper_loop_context_path or _write_context(tmp_path),
        date_md_path=date_md_path or _write_date_md(tmp_path, record),
        store_path=store_path or _write_store(tmp_path, record),
        out_dir=out_dir or (tmp_path / "paper_loop"),
        now=now,
        force=force,
    )


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


def test_01_example_paper_loop_context_validates_successfully() -> None:
    bundle = load_paper_loop_context(EXAMPLE_CONTEXT)
    assert bundle.version == 1
    assert bundle.metadata["paper_only"] is True
    MarketPrice.model_validate(bundle.market_price.model_dump(mode="json"))
    RiskFilterContext.model_validate(bundle.risk_context.model_dump(mode="json"))


def test_02_valid_assembly_writes_expected_output_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    payload = _assemble(tmp_path, out_dir=out_dir)
    assert payload["status"] == "ok"
    for name in _output_files():
        assert (out_dir / name).is_file()


def test_03_paper_loop_input_round_trips_through_model_validate(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    _assemble(tmp_path, out_dir=out_dir)
    input_name, _, _ = _output_files()
    raw = json.loads((out_dir / input_name).read_text(encoding="utf-8"))
    PaperLoopInput.model_validate(raw)


def test_04_output_uses_broker_account_role_paper(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    _assemble(tmp_path, out_dir=out_dir)
    input_name, _, _ = _output_files()
    raw = json.loads((out_dir / input_name).read_text(encoding="utf-8"))
    assert raw["broker_account_role"] == AccountRole.PAPER.value


def test_05_assembly_txt_includes_execution_guard_lines(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    _assemble(tmp_path, out_dir=out_dir)
    _, txt_name, _ = _output_files()
    txt = (out_dir / txt_name).read_text(encoding="utf-8")
    assert "execution: NOT RUN" in txt
    assert "order generation: NOT RUN" in txt
    assert "broker: NOT CALLED" in txt
    assert "KIS: NOT CALLED" in txt
    assert "PaperLoopInput model validation: PASS" in txt


def test_06_summary_includes_execution_flags_false(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    _assemble(tmp_path, out_dir=out_dir)
    _, _, summary_name = _output_files()
    summary = json.loads((out_dir / summary_name).read_text(encoding="utf-8"))
    assert summary["execution_run"] is False
    assert summary["order_generation_run"] is False
    assert summary["broker_called"] is False
    assert summary["kis_called"] is False


def test_07_json_output_is_parseable_and_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _sample_record()
    out_dir = tmp_path / "paper_loop"
    from assemble_paper_loop_input import main

    argv = [
        "--validated-scout", str(_write_scout(tmp_path)),
        "--validated-allocator", str(_write_allocator(tmp_path)),
        "--validated-analysis", str(_write_analysis(tmp_path)),
        "--paper-loop-context", str(_write_context(tmp_path)),
        "--date-md", str(_write_date_md(tmp_path, record)),
        "--store", str(_write_store(tmp_path, record)),
        "--out-dir", str(out_dir),
        "--json",
    ]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "ok"
    assert "allocator_decision" not in payload


def test_08_json_verbose_keeps_stdout_pure_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _sample_record()
    out_dir = tmp_path / "paper_loop"
    from assemble_paper_loop_input import main

    argv = [
        "--validated-allocator", str(_write_allocator(tmp_path)),
        "--validated-analysis", str(_write_analysis(tmp_path)),
        "--paper-loop-context", str(_write_context(tmp_path)),
        "--date-md", str(_write_date_md(tmp_path, record)),
        "--store", str(_write_store(tmp_path, record)),
        "--out-dir", str(out_dir),
        "--json",
        "--verbose",
    ]
    assert main(argv) == 0
    captured = capsys.readouterr()
    json.loads(captured.out.strip())
    assert "verbose:" in captured.err


def test_09_invalid_scout_fails_closed_when_provided(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    bad = dict(MANUAL_SMOKE_SCOUT)
    bad.pop("summary_one_liner")
    bad_path = tmp_path / "scout_output.validated.json"
    bad_path.write_text(json.dumps(bad, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(AssemblyError, match="summary_one_liner"):
        _assemble(tmp_path, validated_scout_path=bad_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _output_files())


def test_10_invalid_allocator_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    bad = dict(VALID_ALLOCATOR_RAW)
    bad.pop("summary_one_liner")
    bad_path = tmp_path / "allocator_output.validated.json"
    bad_path.write_text(json.dumps(bad, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(AssemblyError, match="summary_one_liner"):
        _assemble(tmp_path, validated_allocator_path=bad_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _output_files())


def test_11_invalid_analysis_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    bad = dict(VALID_ANALYSIS_RAW)
    bad.pop("summary_one_liner")
    bad_path = tmp_path / "analysis_output.validated.json"
    bad_path.write_text(json.dumps(bad, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(AssemblyError, match="summary_one_liner"):
        _assemble(tmp_path, validated_analysis_path=bad_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _output_files())


def test_12_invalid_paper_loop_context_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    bad = json.loads(EXAMPLE_CONTEXT.read_text(encoding="utf-8"))
    bad["version"] = 2
    with pytest.raises(AssemblyError, match="version must be exactly 1"):
        _assemble(tmp_path, paper_loop_context_path=_write_context(tmp_path, bad), out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _output_files())


def test_13_metadata_paper_only_not_true_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    bad = json.loads(EXAMPLE_CONTEXT.read_text(encoding="utf-8"))
    bad["metadata"]["paper_only"] = False
    with pytest.raises(AssemblyError, match="metadata.paper_only must be true"):
        _assemble(tmp_path, paper_loop_context_path=_write_context(tmp_path, bad), out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _output_files())


def test_14_market_price_symbol_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    bad = json.loads(EXAMPLE_CONTEXT.read_text(encoding="utf-8"))
    bad["market_price"]["symbol"] = "OTHER-SYMBOL"
    with pytest.raises(AssemblyError, match="market_price.symbol must match"):
        _assemble(tmp_path, paper_loop_context_path=_write_context(tmp_path, bad), out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _output_files())


def test_15_market_price_market_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    bad = json.loads(EXAMPLE_CONTEXT.read_text(encoding="utf-8"))
    bad["market_price"]["market"] = "US"
    with pytest.raises(AssemblyError, match="market_price.market must match"):
        _assemble(tmp_path, paper_loop_context_path=_write_context(tmp_path, bad), out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _output_files())


def test_16_market_price_currency_invalid_for_kr_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    bad = json.loads(EXAMPLE_CONTEXT.read_text(encoding="utf-8"))
    bad["market_price"]["currency"] = "USD"
    bad["risk_context"]["currency"] = None
    with pytest.raises(AssemblyError, match="market_price.currency must be KRW"):
        _assemble(tmp_path, paper_loop_context_path=_write_context(tmp_path, bad), out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _output_files())


def test_17_risk_context_currency_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    bad = json.loads(EXAMPLE_CONTEXT.read_text(encoding="utf-8"))
    bad["risk_context"]["currency"] = "USD"
    with pytest.raises(AssemblyError, match="risk_context.currency must match market_price.currency"):
        _assemble(tmp_path, paper_loop_context_path=_write_context(tmp_path, bad), out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _output_files())


def test_18_scout_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    only_other = _sample_record(date_id="260528-9")
    with pytest.raises(AssemblyError, match="ScoutSummary cited date_id missing from Date.md"):
        _assemble(
            tmp_path,
            date_md_path=_write_date_md(tmp_path, only_other),
            store_path=_write_store(tmp_path, only_other),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_19_allocator_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    record = _sample_record()
    bad_alloc = dict(VALID_ALLOCATOR_RAW)
    bad_alloc["reasons"] = [
        {"reason": "Other id.", "date_id": "260528-9", "source_name": "operator-smoke"}
    ]
    with pytest.raises(AssemblyError, match="AllocatorDecision cited date_id missing from Date.md"):
        _assemble(
            tmp_path,
            validated_allocator_path=_write_allocator(tmp_path, bad_alloc),
            date_md_path=_write_date_md(tmp_path, record),
            store_path=_write_store(tmp_path, record),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_20_analysis_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    record = _sample_record()
    bad_analysis = dict(VALID_ANALYSIS_RAW)
    bad_analysis["reasons"] = [
        {"reason": "Other id.", "date_id": "260528-9", "source_name": "operator-smoke"}
    ]
    with pytest.raises(AssemblyError, match="AnalysisDecision cited date_id missing from Date.md"):
        _assemble(
            tmp_path,
            validated_analysis_path=_write_analysis(tmp_path, bad_analysis),
            date_md_path=_write_date_md(tmp_path, record),
            store_path=_write_store(tmp_path, record),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_21_universe_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    bad_analysis = dict(VALID_ANALYSIS_RAW)
    bad_analysis["universe"] = "other-universe"
    with pytest.raises(AssemblyError, match="universe mismatch"):
        _assemble(tmp_path, validated_analysis_path=_write_analysis(tmp_path, bad_analysis), out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _output_files())


def test_22_date_md_store_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    record = _sample_record()
    extra = _sample_record(date_id="260528-2")
    with pytest.raises(AssemblyError, match="Date.md date_id missing from store"):
        _assemble(
            tmp_path,
            date_md_path=_write_date_md(tmp_path, record, extra),
            store_path=_write_store(tmp_path, record),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_23_store_argument_is_required_at_cli_level() -> None:
    from assemble_paper_loop_input import main

    with pytest.raises(SystemExit) as exc_info:
        main([
            "--validated-allocator", "allocator.json",
            "--validated-analysis", "analysis.json",
            "--paper-loop-context", "context.json",
            "--date-md", "Date.md",
            "--out-dir", "out",
        ])
    assert exc_info.value.code != 0


def test_24_now_timezone_naive_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    record = _sample_record()
    with pytest.raises(AssemblyError, match="timezone-aware datetime"):
        run_assemble_paper_loop_input(
            validated_scout_path=_write_scout(tmp_path),
            validated_allocator_path=_write_allocator(tmp_path),
            validated_analysis_path=_write_analysis(tmp_path),
            portfolio_state_path=None,
            paper_loop_context_path=_write_context(tmp_path),
            date_md_path=_write_date_md(tmp_path, record),
            store_path=_write_store(tmp_path, record),
            out_dir=out_dir,
            now=datetime(2026, 5, 28, 14, 0),
            force=False,
        )
    assert not any((out_dir / name).exists() for name in _output_files())


def test_25_existing_output_files_fail_without_force(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    record = _sample_record()
    paths = {
        "validated_scout_path": _write_scout(tmp_path),
        "validated_allocator_path": _write_allocator(tmp_path),
        "validated_analysis_path": _write_analysis(tmp_path),
        "paper_loop_context_path": _write_context(tmp_path),
        "date_md_path": _write_date_md(tmp_path, record),
        "store_path": _write_store(tmp_path, record),
    }
    _assemble(tmp_path, out_dir=out_dir, **paths)
    input_name, _, _ = _output_files()
    (out_dir / input_name).write_text("{}", encoding="utf-8")
    with pytest.raises(AssemblyError, match="output files already exist") as exc_info:
        _assemble(tmp_path, out_dir=out_dir, force=False, **paths)
    assert exc_info.value.stage == "write"


def test_26_force_overwrites_expected_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    record = _sample_record()
    paths = {
        "validated_scout_path": _write_scout(tmp_path),
        "validated_allocator_path": _write_allocator(tmp_path),
        "validated_analysis_path": _write_analysis(tmp_path),
        "paper_loop_context_path": _write_context(tmp_path),
        "date_md_path": _write_date_md(tmp_path, record),
        "store_path": _write_store(tmp_path, record),
    }
    _assemble(tmp_path, out_dir=out_dir, **paths)
    input_name, _, _ = _output_files()
    (out_dir / input_name).write_text("{}", encoding="utf-8")
    payload = _assemble(tmp_path, out_dir=out_dir, force=True, **paths)
    assert payload["status"] == "ok"
    raw = json.loads((out_dir / input_name).read_text(encoding="utf-8"))
    assert raw["run_id"] == "paper-run-260528-1-smoke"


def test_27_script_help_exits_zero() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0


def test_28_script_does_not_import_forbidden_modules() -> None:
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


def test_29_script_does_not_create_execution_artifacts(tmp_path: Path) -> None:
    out_dir = tmp_path / "paper_loop"
    _assemble(tmp_path, out_dir=out_dir)
    names = {path.name for path in out_dir.iterdir()}
    assert names == set(_output_files())


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
