from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATE_SCRIPT = REPO_ROOT / "ops" / "validate_analysis_raw_json.py"
BUILD_SCRIPT = REPO_ROOT / "ops" / "build_analysis_manual_packet.py"
ACCEPTANCE_CHECK = REPO_ROOT / "ops" / "acceptance_check.sh"
RUNBOOK = REPO_ROOT / "docs" / "RUNBOOK.md"
EXAMPLE_PORTFOLIO = REPO_ROOT / "docs" / "examples" / "portfolio_state.paper.example.json"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from analysis.models import AnalysisDecision
from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType
from research_source_intake import render_date_md
from scout.models import ScoutSummary
from validate_analysis_raw_json import (
    ValidationError,
    main,
    run_validate_analysis_raw_json,
    validation_output_filenames,
)

KST = timezone(timedelta(hours=9))
KST_TS = "2026-05-28T09:00:00+09:00"
KST_CREATED = "2026-05-28T09:05:00+09:00"
NOW = datetime(2026, 5, 28, 0, 0, tzinfo=timezone.utc)
MARKET = "KR"
SYMBOL = "SYNTH-KR-0001"

MANUAL_SMOKE_SCOUT: dict[str, object] = {
    "summary_id": "scout-kr-260528-1-smoke-test",
    "created_at": "2026-05-28T11:00:19.469156Z",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic manual research source for Foundation 8G smoke test on SYNTH-KR-0001.",
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
    "metadata": {"date_ids": ["260528-1"], "foundation": "8G"},
}

VALID_ALLOCATOR_RAW: dict[str, object] = {
    "decision_id": "allocator-260528-1-smoke-test",
    "created_at": "2026-05-28T12:00:00+09:00",
    "schema_name": "allocator_decision.v1",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic allocation decision for Foundation 8G validator smoke.",
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
    "metadata": {"foundation": "8G"},
}

VALID_ANALYSIS_RAW: dict[str, object] = {
    "decision_id": "analysis-260528-1-smoke-test",
    "created_at": "2026-05-28T12:30:00+09:00",
    "schema_name": "analysis_decision.v1",
    "universe": "paper-v0",
    "symbol": SYMBOL,
    "market": MARKET,
    "summary_one_liner": "Synthetic per-symbol analysis hold for Foundation 8G smoke.",
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
    "metadata": {"foundation": "8G", "note": "smoke"},
}


def _sample_record(*, date_id: str = "260528-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.MANUAL,
        source_name="operator-smoke",
        source_timestamp=datetime.fromisoformat(KST_TS),
        created_at=datetime.fromisoformat(KST_CREATED),
        summary="Synthetic manual research source for Foundation 8G test.",
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


def _write_analysis_input(
    tmp_path: Path,
    *,
    universe: str = "paper-v0",
    market: str = MARKET,
    symbol: str = SYMBOL,
    allowed_date_ids: list[str] | None = None,
    allocator_tolerance_context: dict[str, str] | None = None,
) -> Path:
    from allocator.models import AllocatorDecision

    path = tmp_path / "analysis_input.kr.SYNTH-KR-0001.json"
    scout_summary = ScoutSummary.model_validate(MANUAL_SMOKE_SCOUT).model_dump(mode="json")
    allocator_decision = AllocatorDecision.model_validate(VALID_ALLOCATOR_RAW).model_dump(mode="json")
    portfolio_state = json.loads(EXAMPLE_PORTFOLIO.read_text(encoding="utf-8"))
    payload: dict[str, object] = {
        "created_at": "2026-05-28T00:00:00+00:00",
        "universe": universe,
        "market": market,
        "symbol": symbol,
        "scout_summary": scout_summary,
        "allocator_decision": allocator_decision,
        "portfolio_state": portfolio_state,
        "allowed_date_ids": allowed_date_ids if allowed_date_ids is not None else ["260528-1"],
        "analysis_schema_summary": {"schema_name": "analysis_decision.v1"},
        "metadata": {"foundation": "8G"},
    }
    if allocator_tolerance_context is not None:
        payload["allocator_tolerance_context"] = allocator_tolerance_context
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_raw_json(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "analysis_output.kr.SYNTH-KR-0001.raw.json"
    path.write_text(json.dumps(payload if payload is not None else VALID_ANALYSIS_RAW, indent=2) + "\n", encoding="utf-8")
    return path


def _validation_files() -> tuple[str, str, str]:
    return validation_output_filenames(MARKET, SYMBOL)


def _validate(
    tmp_path: Path,
    *,
    raw_json_path: Path | None = None,
    raw_payload: dict[str, object] | None = None,
    out_dir: Path | None = None,
    analysis_input_path: Path | None = None,
    date_md_path: Path | None = None,
    store_path: Path | None = None,
    force: bool = False,
    now: datetime | None = None,
    allocator_target_weight_percent: str | None = None,
    tolerance_percent: str | None = None,
) -> dict[str, object]:
    record = _sample_record()
    target_out = out_dir if out_dir is not None else tmp_path / "out"
    resolved_raw = raw_json_path if raw_json_path is not None else _write_raw_json(tmp_path, raw_payload)
    return run_validate_analysis_raw_json(
        raw_json_path=resolved_raw,
        analysis_input_path=analysis_input_path or _write_analysis_input(tmp_path),
        date_md_path=date_md_path or _write_date_md(tmp_path, record),
        out_dir=target_out,
        store_path=store_path if store_path is not None else _write_store(tmp_path, record),
        now=now or NOW,
        cli_allocator_target_weight_percent=allocator_target_weight_percent,
        cli_tolerance_percent=tolerance_percent,
        force=force,
    )


def test_19_valid_analysis_raw_json_produces_expected_output_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    payload = _validate(tmp_path, out_dir=out_dir)
    assert payload["status"] == "ok"
    for name in _validation_files():
        assert (out_dir / name).is_file()


def test_20_validated_json_round_trips_through_analysis_decision_schema(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    _validate(tmp_path, out_dir=out_dir)
    validated_name, _, _ = _validation_files()
    validated = json.loads((out_dir / validated_name).read_text(encoding="utf-8"))
    AnalysisDecision.model_validate(validated)
    assert validated["decision_id"] == "analysis-260528-1-smoke-test"


def test_21_validation_summary_includes_expected_fields(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    _validate(tmp_path, out_dir=out_dir)
    _, _, summary_name = _validation_files()
    summary = json.loads((out_dir / summary_name).read_text(encoding="utf-8"))
    assert summary["cited_date_ids"] == ["260528-1"]
    assert summary["action"] == "hold"
    assert summary["target_weight_percent"] == "5"
    assert summary["created_at_freshness_checked"] is False


def test_22_validation_txt_includes_pass_line_without_raw_json_body(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    raw_path = _write_raw_json(tmp_path)
    _validate(tmp_path, out_dir=out_dir)
    _, txt_name, _ = _validation_files()
    txt = (out_dir / txt_name).read_text(encoding="utf-8")
    assert "AnalysisDecision schema: PASS" in txt
    assert "AnalysisDecisionValidator: PASS" in txt
    assert "created_at freshness ordering: NOT CHECKED" in txt
    assert raw_path.read_text(encoding="utf-8") not in txt


def test_23_invalid_raw_json_parse_fails_closed_without_outputs(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    raw_path = tmp_path / "analysis_output.kr.SYNTH-KR-0001.raw.json"
    raw_path.write_text('{"broken": }', encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid JSON"):
        _validate(
            tmp_path,
            raw_json_path=raw_path,
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_24_markdown_fenced_json_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    raw_path = tmp_path / "analysis_output.kr.SYNTH-KR-0001.raw.json"
    raw_path.write_text("```json\n" + json.dumps(VALID_ANALYSIS_RAW) + "\n```", encoding="utf-8")
    with pytest.raises(ValidationError, match="markdown fences"):
        _validate(tmp_path, raw_json_path=raw_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_25_prose_before_after_json_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    raw_path = tmp_path / "analysis_output.kr.SYNTH-KR-0001.raw.json"
    raw_path.write_text("note\n" + json.dumps(VALID_ANALYSIS_RAW), encoding="utf-8")
    with pytest.raises(ValidationError, match="without prose"):
        _validate(tmp_path, raw_json_path=raw_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_26_raw_json_array_root_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    raw_path = tmp_path / "analysis_output.kr.SYNTH-KR-0001.raw.json"
    raw_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValidationError, match="single JSON object"):
        _validate(tmp_path, raw_json_path=raw_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_27_analysis_decision_schema_error_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ANALYSIS_RAW)
    bad.pop("summary_one_liner")
    with pytest.raises(ValidationError, match="summary_one_liner"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_28_summary_one_liner_over_200_chars_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ANALYSIS_RAW)
    bad["summary_one_liner"] = "x" * 201
    with pytest.raises(ValidationError, match="summary_one_liner"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_29_missing_required_role_view_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ANALYSIS_RAW)
    bad.pop("bear")
    with pytest.raises(ValidationError):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_30_empty_reasons_fail_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ANALYSIS_RAW)
    bad["reasons"] = []
    with pytest.raises(ValidationError, match="reasons"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_31_cited_date_id_missing_from_allowed_date_ids_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ANALYSIS_RAW)
    bad["reasons"] = [
        {"reason": "Other id.", "date_id": "260528-9", "source_name": "operator-smoke"}
    ]
    with pytest.raises(ValidationError, match="missing from analysis_input.allowed_date_ids"):
        _validate(
            tmp_path,
            raw_payload=bad,
            analysis_input_path=_write_analysis_input(tmp_path, allowed_date_ids=["260528-1"]),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_32_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    record = _sample_record()
    bad = dict(VALID_ANALYSIS_RAW)
    bad["reasons"] = [
        {"reason": "Missing from Date.md.", "date_id": "260528-9", "source_name": "operator-smoke"}
    ]
    with pytest.raises(ValidationError, match="missing from Date.md"):
        _validate(
            tmp_path,
            raw_payload=bad,
            analysis_input_path=_write_analysis_input(
                tmp_path,
                allowed_date_ids=["260528-1", "260528-9"],
            ),
            date_md_path=_write_date_md(tmp_path, record),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_33_bracketed_date_id_in_raw_json_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ANALYSIS_RAW)
    bad["reasons"] = [
        {"reason": "Bracketed.", "date_id": "[260528-1]", "source_name": "operator-smoke"}
    ]
    with pytest.raises(ValidationError, match="brackets"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_34_universe_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ANALYSIS_RAW)
    bad["universe"] = "other-universe"
    with pytest.raises(ValidationError, match="universe mismatch"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_35_market_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ANALYSIS_RAW)
    bad["market"] = "US"
    with pytest.raises(ValidationError, match="market mismatch"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_36_symbol_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ANALYSIS_RAW)
    bad["symbol"] = "OTHER"
    with pytest.raises(ValidationError, match="symbol mismatch"):
        _validate(tmp_path, raw_payload=bad, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_37_store_argument_is_required_at_cli_level() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([
            "--raw-json", "raw.json",
            "--analysis-input", "input.json",
            "--date-md", "Date.md",
            "--out-dir", "out",
        ])
    assert exc_info.value.code != 0


def test_38_now_timezone_naive_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        run_validate_analysis_raw_json(
            raw_json_path=_write_raw_json(tmp_path),
            analysis_input_path=_write_analysis_input(tmp_path),
            date_md_path=_write_date_md(tmp_path, _sample_record()),
            out_dir=out_dir,
            store_path=_write_store(tmp_path, _sample_record()),
            now=datetime(2026, 5, 28, 12, 0),
            cli_allocator_target_weight_percent=None,
            cli_tolerance_percent=None,
            force=False,
        )
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_39_allocator_tolerance_violation_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    bad = dict(VALID_ANALYSIS_RAW)
    bad["fund_manager"] = {
        **VALID_ANALYSIS_RAW["fund_manager"],  # type: ignore[arg-type]
        "target_weight_percent": "20",
    }
    with pytest.raises(ValidationError, match="allocator_target_weight"):
        _validate(
            tmp_path,
            raw_payload=bad,
            out_dir=out_dir,
            allocator_target_weight_percent="5",
            tolerance_percent="1",
        )
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_40_incomplete_tolerance_context_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    with pytest.raises(ValidationError, match="incomplete tolerance context"):
        _validate(
            tmp_path,
            out_dir=out_dir,
            analysis_input_path=_write_analysis_input(
                tmp_path,
                allocator_tolerance_context={"allocator_target_weight_percent": "5"},
            ),
        )
    assert not any((out_dir / name).exists() for name in _validation_files())


def test_41_existing_validation_output_files_fail_without_force(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    _validate(tmp_path, out_dir=out_dir)
    validated_name, _, _ = _validation_files()
    (out_dir / validated_name).write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="output files already exist") as exc_info:
        _validate(tmp_path, out_dir=out_dir, force=False)
    assert exc_info.value.stage == "write"


def test_42_force_overwrites_expected_validation_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    _validate(tmp_path, out_dir=out_dir)
    validated_name, _, _ = _validation_files()
    (out_dir / validated_name).write_text("{}", encoding="utf-8")
    payload = _validate(tmp_path, out_dir=out_dir, force=True)
    assert payload["status"] == "ok"
    validated = json.loads((out_dir / validated_name).read_text(encoding="utf-8"))
    assert validated["decision_id"] == "analysis-260528-1-smoke-test"


def test_43_json_output_is_parseable_and_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _sample_record()
    out_dir = tmp_path / "out"
    argv = [
        "--raw-json", str(_write_raw_json(tmp_path)),
        "--analysis-input", str(_write_analysis_input(tmp_path)),
        "--date-md", str(_write_date_md(tmp_path, record)),
        "--store", str(_write_store(tmp_path, record)),
        "--out-dir", str(out_dir),
        "--json",
    ]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "ok"
    assert "bear" not in payload


def test_44_json_verbose_keeps_stdout_pure_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _sample_record()
    out_dir = tmp_path / "out"
    argv = [
        "--raw-json", str(_write_raw_json(tmp_path)),
        "--analysis-input", str(_write_analysis_input(tmp_path)),
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


def test_45_created_at_equal_upstream_timestamps_is_accepted(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    same_as_scout = dict(VALID_ANALYSIS_RAW)
    same_as_scout["created_at"] = MANUAL_SMOKE_SCOUT["created_at"]
    payload = _validate(tmp_path, raw_payload=same_as_scout, out_dir=out_dir)
    assert payload["status"] == "ok"


def test_46_validation_summary_created_at_freshness_checked_false(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    _validate(tmp_path, out_dir=out_dir)
    _, _, summary_name = _validation_files()
    summary = json.loads((out_dir / summary_name).read_text(encoding="utf-8"))
    assert summary["created_at_freshness_checked"] is False


def test_47_validate_script_help_exits_zero() -> None:
    import subprocess

    env = {"PYTHONPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(
        [sys.executable, str(VALIDATE_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0


def test_48_analysis_ops_scripts_do_not_import_forbidden_modules() -> None:
    source_build = BUILD_SCRIPT.read_text(encoding="utf-8").lower()
    source_validate = VALIDATE_SCRIPT.read_text(encoding="utf-8").lower()
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
    )
    for token in forbidden:
        assert token not in source_build
        assert token not in source_validate


def test_49_pytest_baseline_synchronized_between_runbook_and_acceptance_check() -> None:
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


def test_50_manual_smoke_shape_raw_json_validates_successfully(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    payload = _validate(tmp_path, out_dir=out_dir)
    assert payload["decision_id"] == "analysis-260528-1-smoke-test"
    assert payload["cited_date_ids_count"] == 1
