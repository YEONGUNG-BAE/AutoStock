from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = REPO_ROOT / "ops" / "build_analysis_manual_packet.py"
EXAMPLE_PORTFOLIO = REPO_ROOT / "docs" / "examples" / "portfolio_state.paper.example.json"
EXAMPLE_UNIVERSE = REPO_ROOT / "config" / "universe.paper.toml.example"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from allocator.models import AllocatorDecision
from build_analysis_manual_packet import (
    PacketError,
    output_filenames,
    run_build_analysis_manual_packet,
)
from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType
from research_source_intake import render_date_md
from scout.models import ScoutSummary

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
            "summary": "The input data is explicitly identified as a synthetic manual research source.",
            "reasons": [
                {
                    "reason": "The payload note indicates the data is synthetic and intended for a smoke test.",
                    "date_id": "260528-1",
                    "source_name": "operator-smoke",
                    "quote": "synthetic",
                }
            ],
        }
    ],
    "metadata": {"date_ids": ["260528-1"], "foundation": "8G", "market_scope": "KR"},
}

VALID_ALLOCATOR_RAW: dict[str, object] = {
    "decision_id": "allocator-260528-1-smoke-test",
    "created_at": "2026-05-28T12:00:00+09:00",
    "schema_name": "allocator_decision.v1",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic allocation decision for Foundation 8G smoke.",
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
    "metadata": {"foundation": "8G", "note": "manual smoke"},
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


def _write_portfolio_state(tmp_path: Path) -> Path:
    path = tmp_path / "portfolio_state.json"
    path.write_text(EXAMPLE_PORTFOLIO.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _write_validated_scout(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "scout_output.validated.json"
    model = ScoutSummary.model_validate(payload if payload is not None else MANUAL_SMOKE_SCOUT)
    path.write_text(json.dumps(model.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def _write_validated_allocator(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "allocator_output.validated.json"
    model = AllocatorDecision.model_validate(payload if payload is not None else VALID_ALLOCATOR_RAW)
    path.write_text(json.dumps(model.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def _write_allocator_validation_summary(
    tmp_path: Path,
    *,
    status: str = "ok",
    decision_id: str = "allocator-260528-1-smoke-test",
) -> Path:
    path = tmp_path / "allocator_validation_summary.json"
    path.write_text(
        json.dumps(
            {
                "status": status,
                "decision_id": decision_id,
                "created_at_freshness_checked": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_date_md(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    path = tmp_path / "Date.md"
    path.write_text(render_date_md(records), encoding="utf-8")
    return path


def _write_store(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    with store.transaction():
        for record in records:
            store.save_record(record)
    store.close()
    return store_path


def _write_universe(tmp_path: Path, *, name: str = "paper-v0") -> Path:
    path = tmp_path / "universe.toml"
    text = EXAMPLE_UNIVERSE.read_text(encoding="utf-8")
    path.write_text(text.replace('name = "paper-v0"', f'name = "{name}"'), encoding="utf-8")
    return path


def _packet_files() -> tuple[str, str, str]:
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


def _build_packet(
    tmp_path: Path,
    *,
    validated_scout_path: Path | None = None,
    validated_allocator_path: Path | None = None,
    allocator_validation_summary_path: Path | None = None,
    portfolio_state_path: Path | None = None,
    date_md_path: Path | None = None,
    store_path: Path | None = None,
    universe_path: Path | None = None,
    market: str = MARKET,
    symbol: str = SYMBOL,
    out_dir: Path | None = None,
    force: bool = False,
    allocator_target_weight_percent: str | None = None,
    tolerance_percent: str | None = None,
) -> dict[str, object]:
    record = _sample_record()
    kwargs: dict[str, object] = {
        "validated_scout_path": validated_scout_path or _write_validated_scout(tmp_path),
        "validated_allocator_path": validated_allocator_path or _write_validated_allocator(tmp_path),
        "allocator_validation_summary_path": (
            allocator_validation_summary_path
            if allocator_validation_summary_path is not None
            else _write_allocator_validation_summary(tmp_path)
        ),
        "portfolio_state_path": portfolio_state_path or _write_portfolio_state(tmp_path),
        "date_md_path": date_md_path or _write_date_md(tmp_path, record),
        "store_path": store_path or _write_store(tmp_path, record),
        "universe_path": universe_path if universe_path is not None else _write_universe(tmp_path),
        "market": market,
        "symbol": symbol,
        "out_dir": out_dir or (tmp_path / "analysis"),
        "now": NOW,
        "tolerance": None,
        "force": force,
    }
    if allocator_target_weight_percent is not None or tolerance_percent is not None:
        from build_analysis_manual_packet import ToleranceContext
        from domain.identifiers import Percent

        if (allocator_target_weight_percent is None) != (tolerance_percent is None):
            raise PacketError(
                "args",
                "allocator-target-weight-percent and tolerance-percent must both be provided or both omitted",
            )
        kwargs["tolerance"] = ToleranceContext(
            allocator_target_weight_percent=Percent(allocator_target_weight_percent),
            tolerance_percent=Percent(tolerance_percent),
        )
    return run_build_analysis_manual_packet(**kwargs)  # type: ignore[arg-type]


def test_01_analysis_packet_build_writes_expected_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    payload = _build_packet(tmp_path, out_dir=out_dir)
    assert payload["status"] == "ok"
    for name in _packet_files():
        assert (out_dir / name).is_file()


def test_02_analysis_prompt_contains_required_guardrails(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    _build_packet(tmp_path, out_dir=out_dir)
    _, prompt_name, _ = _packet_files()
    prompt = (out_dir / prompt_name).read_text(encoding="utf-8")
    assert "JSON only" in prompt
    assert "Do **not** wrap JSON in markdown fences" in prompt
    assert "schema_name must be 'analysis_decision.v1'" in prompt
    assert "buy, sell, hold" in prompt
    assert "bear must include" in prompt
    assert "Do not produce orders" in prompt
    assert "260528-1" in prompt


def test_03_analysis_input_contains_expected_core_fields(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    _build_packet(tmp_path, out_dir=out_dir)
    input_name, _, _ = _packet_files()
    analysis_input = json.loads((out_dir / input_name).read_text(encoding="utf-8"))
    assert analysis_input["universe"] == "paper-v0"
    assert analysis_input["market"] == MARKET
    assert analysis_input["symbol"] == SYMBOL
    assert analysis_input["allowed_date_ids"] == ["260528-1"]
    assert analysis_input["scout_summary"]["summary_id"] == "scout-kr-260528-1-smoke-test"
    assert analysis_input["allocator_decision"]["decision_id"] == "allocator-260528-1-smoke-test"
    assert analysis_input["portfolio_state"]["version"] == 1


def test_04_existing_output_files_fail_without_force(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    record = _sample_record()
    paths = {
        "validated_scout_path": _write_validated_scout(tmp_path),
        "validated_allocator_path": _write_validated_allocator(tmp_path),
        "allocator_validation_summary_path": _write_allocator_validation_summary(tmp_path),
        "portfolio_state_path": _write_portfolio_state(tmp_path),
        "date_md_path": _write_date_md(tmp_path, record),
        "store_path": _write_store(tmp_path, record),
        "universe_path": _write_universe(tmp_path),
    }
    _build_packet(tmp_path, out_dir=out_dir, **paths)
    input_name, _, _ = _packet_files()
    (out_dir / input_name).write_text("{}", encoding="utf-8")
    with pytest.raises(PacketError, match="output files already exist") as exc_info:
        _build_packet(tmp_path, out_dir=out_dir, force=False, **paths)
    assert exc_info.value.stage == "write"


def test_05_force_overwrites_expected_packet_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    record = _sample_record()
    paths = {
        "validated_scout_path": _write_validated_scout(tmp_path),
        "validated_allocator_path": _write_validated_allocator(tmp_path),
        "allocator_validation_summary_path": _write_allocator_validation_summary(tmp_path),
        "portfolio_state_path": _write_portfolio_state(tmp_path),
        "date_md_path": _write_date_md(tmp_path, record),
        "store_path": _write_store(tmp_path, record),
        "universe_path": _write_universe(tmp_path),
    }
    _build_packet(tmp_path, out_dir=out_dir, **paths)
    input_name, _, _ = _packet_files()
    (out_dir / input_name).write_text("{}", encoding="utf-8")
    payload = _build_packet(tmp_path, out_dir=out_dir, force=True, **paths)
    assert payload["status"] == "ok"
    rewritten = json.loads((out_dir / input_name).read_text(encoding="utf-8"))
    assert "allowed_date_ids" in rewritten


def test_06_json_output_is_parseable_and_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _sample_record()
    out_dir = tmp_path / "analysis"
    argv = [
        "--validated-scout", str(_write_validated_scout(tmp_path)),
        "--validated-allocator", str(_write_validated_allocator(tmp_path)),
        "--allocator-validation-summary", str(_write_allocator_validation_summary(tmp_path)),
        "--portfolio-state", str(_write_portfolio_state(tmp_path)),
        "--date-md", str(_write_date_md(tmp_path, record)),
        "--store", str(_write_store(tmp_path, record)),
        "--universe", str(_write_universe(tmp_path)),
        "--market", MARKET,
        "--symbol", SYMBOL,
        "--out-dir", str(out_dir),
        "--json",
    ]
    from build_analysis_manual_packet import main

    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["status"] == "ok"
    assert "scout_summary" not in payload


def test_07_json_verbose_keeps_stdout_pure_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _sample_record()
    out_dir = tmp_path / "analysis"
    argv = [
        "--validated-scout", str(_write_validated_scout(tmp_path)),
        "--validated-allocator", str(_write_validated_allocator(tmp_path)),
        "--portfolio-state", str(_write_portfolio_state(tmp_path)),
        "--date-md", str(_write_date_md(tmp_path, record)),
        "--store", str(_write_store(tmp_path, record)),
        "--market", MARKET,
        "--symbol", SYMBOL,
        "--out-dir", str(out_dir),
        "--json",
        "--verbose",
    ]
    from build_analysis_manual_packet import main

    assert main(argv) == 0
    captured = capsys.readouterr()
    json.loads(captured.out.strip())
    assert "verbose:" in captured.err


def test_08_invalid_validated_scout_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    bad = dict(MANUAL_SMOKE_SCOUT)
    bad.pop("summary_one_liner")
    bad_path = tmp_path / "scout_output.validated.json"
    bad_path.write_text(json.dumps(bad, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(PacketError, match="summary_one_liner"):
        _build_packet(tmp_path, validated_scout_path=bad_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _packet_files())


def test_09_invalid_validated_allocator_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    bad = dict(VALID_ALLOCATOR_RAW)
    bad.pop("summary_one_liner")
    bad_path = tmp_path / "allocator_output.validated.json"
    bad_path.write_text(json.dumps(bad, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(PacketError, match="summary_one_liner"):
        _build_packet(tmp_path, validated_allocator_path=bad_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _packet_files())


def test_10_scout_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    only_other = _sample_record(date_id="260528-9")
    with pytest.raises(PacketError, match="ScoutSummary cited date_id missing from Date.md"):
        _build_packet(
            tmp_path,
            date_md_path=_write_date_md(tmp_path, only_other),
            store_path=_write_store(tmp_path, only_other),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _packet_files())


def test_11_allocator_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    scout_only = _sample_record(date_id="260528-1")
    alloc_only = _sample_record(date_id="260528-2")
    bad_alloc = dict(VALID_ALLOCATOR_RAW)
    bad_alloc["reasons"] = [
        {"reason": "Other id.", "date_id": "260528-2", "source_name": "operator-smoke"}
    ]
    with pytest.raises(PacketError, match="AllocatorDecision cited date_id missing from Date.md"):
        _build_packet(
            tmp_path,
            validated_allocator_path=_write_validated_allocator(tmp_path, bad_alloc),
            date_md_path=_write_date_md(tmp_path, scout_only),
            store_path=_write_store(tmp_path, scout_only, alloc_only),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in _packet_files())


def test_12_allocator_validation_summary_status_not_ok_fails(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    bad_summary = _write_allocator_validation_summary(tmp_path, status="error")
    with pytest.raises(PacketError, match="status must be ok"):
        _build_packet(tmp_path, allocator_validation_summary_path=bad_summary, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _packet_files())


def test_13_universe_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    mismatch_universe = _write_universe(tmp_path, name="other-paper-v0")
    with pytest.raises(PacketError, match="universe mismatch"):
        _build_packet(tmp_path, universe_path=mismatch_universe, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _packet_files())


def test_14_universe_missing_enabled_symbol_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    with pytest.raises(PacketError, match="universe missing enabled symbol"):
        _build_packet(tmp_path, market="US", symbol="SYNTH-US-0001", out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in _packet_files())


def test_15_one_tolerance_value_without_other_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    with pytest.raises(PacketError, match="must both be provided or both omitted"):
        _build_packet(
            tmp_path,
            out_dir=out_dir,
            allocator_target_weight_percent="5",
            tolerance_percent=None,
        )


def test_16_both_tolerance_values_included_in_analysis_input(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    _build_packet(
        tmp_path,
        out_dir=out_dir,
        allocator_target_weight_percent="5",
        tolerance_percent="1",
    )
    input_name, _, _ = _packet_files()
    analysis_input = json.loads((out_dir / input_name).read_text(encoding="utf-8"))
    assert analysis_input["allocator_tolerance_context"] == {
        "allocator_target_weight_percent": "5",
        "tolerance_percent": "1",
    }


def test_17_script_help_exits_zero() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0


def test_18_packet_builder_does_not_create_raw_analysis_output(tmp_path: Path) -> None:
    out_dir = tmp_path / "analysis"
    _build_packet(tmp_path, out_dir=out_dir)
    assert not (out_dir / f"analysis_output.kr.{SYMBOL}.raw.json").exists()
    assert list(out_dir.glob("analysis_output*.raw.json")) == []
