from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = REPO_ROOT / "ops" / "build_allocator_manual_packet.py"
EXAMPLE_PORTFOLIO = REPO_ROOT / "docs" / "examples" / "portfolio_state.paper.example.json"
EXAMPLE_UNIVERSE = REPO_ROOT / "config" / "universe.paper.toml.example"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from build_allocator_manual_packet import (
    OUTPUT_FILES as PACKET_FILES,
    PacketError,
    load_portfolio_state,
    run_build_allocator_manual_packet,
)
from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType
from research_source_intake import render_date_md
from scout.models import ScoutSummary

KST = timezone(timedelta(hours=9))
KST_TS = "2026-05-28T09:00:00+09:00"
KST_CREATED = "2026-05-28T09:05:00+09:00"
NOW = datetime(2026, 5, 28, 0, 0, tzinfo=timezone.utc)

MANUAL_SMOKE_RAW: dict[str, object] = {
    "summary_id": "scout-kr-260528-1-smoke-test",
    "created_at": "2026-05-28T11:00:19.469156Z",
    "universe": "paper-v0",
    "summary_one_liner": "Synthetic manual research source for Foundation 8F smoke test on SYNTH-KR-0001.",
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
    "metadata": {"date_ids": ["260528-1"], "foundation": "8F", "market_scope": "KR"},
}


def _sample_record(*, date_id: str = "260528-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.MANUAL,
        source_name="operator-smoke",
        source_timestamp=datetime.fromisoformat(KST_TS),
        created_at=datetime.fromisoformat(KST_CREATED),
        summary="Synthetic manual research source for Foundation 8F test.",
        payload={"note": "synthetic", "score": 1},
        symbol="SYNTH-KR-0001",
        market="KR",
        source_url="https://example.invalid/autostock/synthetic",
    )


def _write_portfolio_state(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "portfolio_state.json"
    if payload is None:
        path.write_text(EXAMPLE_PORTFOLIO.read_text(encoding="utf-8"), encoding="utf-8")
        return path
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _write_validated_scout(tmp_path: Path, payload: dict[str, object] | None = None) -> Path:
    path = tmp_path / "scout_output.validated.json"
    model = ScoutSummary.model_validate(payload if payload is not None else MANUAL_SMOKE_RAW)
    path.write_text(json.dumps(model.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def _write_scout_validation_summary(
    tmp_path: Path,
    *,
    status: str = "ok",
    summary_id: str = "scout-kr-260528-1-smoke-test",
) -> Path:
    path = tmp_path / "scout_validation_summary.json"
    path.write_text(
        json.dumps(
            {
                "status": status,
                "summary_id": summary_id,
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
    scout_validation_summary_path: Path | None = None,
    portfolio_state_path: Path | None = None,
    date_md_path: Path | None = None,
    store_path: Path | None = None,
    universe_path: Path | None = None,
    out_dir: Path | None = None,
    force: bool = False,
) -> dict[str, object]:
    record = _sample_record()
    return run_build_allocator_manual_packet(
        validated_scout_path=validated_scout_path or _write_validated_scout(tmp_path),
        scout_validation_summary_path=(
            scout_validation_summary_path
            if scout_validation_summary_path is not None
            else _write_scout_validation_summary(tmp_path)
        ),
        portfolio_state_path=portfolio_state_path or _write_portfolio_state(tmp_path),
        date_md_path=date_md_path or _write_date_md(tmp_path, record),
        store_path=store_path or _write_store(tmp_path, record),
        universe_path=universe_path if universe_path is not None else _write_universe(tmp_path),
        out_dir=out_dir or (tmp_path / "allocator"),
        now=NOW,
        force=force,
    )


def test_01_load_portfolio_state_accepts_example_json(tmp_path: Path) -> None:
    bundle = load_portfolio_state(_write_portfolio_state(tmp_path))
    assert bundle.version == 1
    assert str(bundle.portfolio_snapshot.total_nav_krw) == "100000000"
    assert str(bundle.nav_snapshot.cash_krw) == "20000000"
    assert bundle.constraints["allowed_markets"] == ["KR", "US"]
    assert bundle.metadata["paper_only"] is True


def test_02_load_portfolio_state_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PacketError, match="portfolio state not found"):
        load_portfolio_state(tmp_path / "missing.json")


def test_03_load_portfolio_state_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_state.json"
    path.write_text('{"broken": }', encoding="utf-8")
    with pytest.raises(PacketError, match="invalid portfolio state JSON"):
        load_portfolio_state(path)


def test_04_load_portfolio_state_rejects_non_object_root(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_state.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(PacketError, match="root must be a JSON object"):
        load_portfolio_state(path)


def test_05_load_portfolio_state_requires_version_1(tmp_path: Path) -> None:
    payload = json.loads(EXAMPLE_PORTFOLIO.read_text(encoding="utf-8"))
    payload["version"] = 2
    with pytest.raises(PacketError, match="version must be exactly 1"):
        load_portfolio_state(_write_portfolio_state(tmp_path, payload))


def test_06_load_portfolio_state_rejects_total_nav_mismatch(tmp_path: Path) -> None:
    payload = json.loads(EXAMPLE_PORTFOLIO.read_text(encoding="utf-8"))
    payload["portfolio_snapshot"]["total_nav_krw"] = "90000000"
    with pytest.raises(PacketError, match="total_nav_krw must equal"):
        load_portfolio_state(_write_portfolio_state(tmp_path, payload))


def test_07_load_portfolio_state_rejects_cash_mismatch(tmp_path: Path) -> None:
    payload = json.loads(EXAMPLE_PORTFOLIO.read_text(encoding="utf-8"))
    payload["portfolio_snapshot"]["cash_krw"] = "19000000"
    with pytest.raises(PacketError, match="cash_krw must equal"):
        load_portfolio_state(_write_portfolio_state(tmp_path, payload))


def test_08_load_portfolio_state_requires_metadata_paper_only_true(tmp_path: Path) -> None:
    payload = json.loads(EXAMPLE_PORTFOLIO.read_text(encoding="utf-8"))
    payload["metadata"]["paper_only"] = False
    with pytest.raises(PacketError, match="metadata.paper_only must be true"):
        load_portfolio_state(_write_portfolio_state(tmp_path, payload))


def test_09_allocator_packet_build_writes_expected_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    payload = _build_packet(tmp_path, out_dir=out_dir)
    assert payload["status"] == "ok"
    for name in PACKET_FILES:
        assert (out_dir / name).is_file()


def test_10_allocator_input_contains_expected_core_fields(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    _build_packet(tmp_path, out_dir=out_dir)
    allocator_input = json.loads((out_dir / "allocator_input.json").read_text(encoding="utf-8"))
    assert allocator_input["universe"] == "paper-v0"
    assert allocator_input["allowed_date_ids"] == ["260528-1"]
    assert allocator_input["metadata"]["foundation"] == "8F"
    assert allocator_input["portfolio_state"]["version"] == 1


def test_11_allocator_prompt_contains_required_guardrails(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    _build_packet(tmp_path, out_dir=out_dir)
    prompt = (out_dir / "allocator_prompt.md").read_text(encoding="utf-8")
    assert "JSON only" in prompt
    assert "Do **not** wrap JSON in markdown fences" in prompt
    assert "schema_name must be 'allocator_decision.v1'" in prompt
    assert "consistency_checker.passed must be true" in prompt
    assert "allocator_output.raw.json" in prompt


def test_12_existing_output_files_fail_without_force(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    record = _sample_record()
    validated_scout_path = _write_validated_scout(tmp_path)
    scout_validation_summary_path = _write_scout_validation_summary(tmp_path)
    portfolio_state_path = _write_portfolio_state(tmp_path)
    date_md_path = _write_date_md(tmp_path, record)
    store_path = _write_store(tmp_path, record)
    universe_path = _write_universe(tmp_path)
    _build_packet(
        tmp_path,
        validated_scout_path=validated_scout_path,
        scout_validation_summary_path=scout_validation_summary_path,
        portfolio_state_path=portfolio_state_path,
        date_md_path=date_md_path,
        store_path=store_path,
        universe_path=universe_path,
        out_dir=out_dir,
    )
    (out_dir / "allocator_input.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PacketError, match="output files already exist") as exc_info:
        _build_packet(
            tmp_path,
            validated_scout_path=validated_scout_path,
            scout_validation_summary_path=scout_validation_summary_path,
            portfolio_state_path=portfolio_state_path,
            date_md_path=date_md_path,
            store_path=store_path,
            universe_path=universe_path,
            out_dir=out_dir,
            force=False,
        )
    assert exc_info.value.stage == "write"


def test_13_force_overwrites_existing_output_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    record = _sample_record()
    validated_scout_path = _write_validated_scout(tmp_path)
    scout_validation_summary_path = _write_scout_validation_summary(tmp_path)
    portfolio_state_path = _write_portfolio_state(tmp_path)
    date_md_path = _write_date_md(tmp_path, record)
    store_path = _write_store(tmp_path, record)
    universe_path = _write_universe(tmp_path)
    _build_packet(
        tmp_path,
        validated_scout_path=validated_scout_path,
        scout_validation_summary_path=scout_validation_summary_path,
        portfolio_state_path=portfolio_state_path,
        date_md_path=date_md_path,
        store_path=store_path,
        universe_path=universe_path,
        out_dir=out_dir,
    )
    (out_dir / "allocator_input.json").write_text("{}", encoding="utf-8")
    payload = _build_packet(
        tmp_path,
        validated_scout_path=validated_scout_path,
        scout_validation_summary_path=scout_validation_summary_path,
        portfolio_state_path=portfolio_state_path,
        date_md_path=date_md_path,
        store_path=store_path,
        universe_path=universe_path,
        out_dir=out_dir,
        force=True,
    )
    assert payload["status"] == "ok"
    rewritten = json.loads((out_dir / "allocator_input.json").read_text(encoding="utf-8"))
    assert "allowed_date_ids" in rewritten


def test_14_missing_date_md_fails_closed_without_outputs(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    record = _sample_record()
    with pytest.raises(PacketError, match="Date.md not found"):
        run_build_allocator_manual_packet(
            validated_scout_path=_write_validated_scout(tmp_path),
            scout_validation_summary_path=_write_scout_validation_summary(tmp_path),
            portfolio_state_path=_write_portfolio_state(tmp_path),
            date_md_path=tmp_path / "missing.md",
            store_path=_write_store(tmp_path, record),
            universe_path=_write_universe(tmp_path),
            out_dir=out_dir,
            now=NOW,
            force=False,
        )
    assert not any((out_dir / name).exists() for name in PACKET_FILES)


def test_15_store_date_md_mismatch_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    record = _sample_record()
    extra = _sample_record(date_id="260528-2")
    with pytest.raises(PacketError, match="Date.md date_id missing from store"):
        run_build_allocator_manual_packet(
            validated_scout_path=_write_validated_scout(tmp_path),
            scout_validation_summary_path=_write_scout_validation_summary(tmp_path),
            portfolio_state_path=_write_portfolio_state(tmp_path),
            date_md_path=_write_date_md(tmp_path, record, extra),
            store_path=_write_store(tmp_path, record),
            universe_path=_write_universe(tmp_path),
            out_dir=out_dir,
            now=NOW,
            force=False,
        )
    assert not any((out_dir / name).exists() for name in PACKET_FILES)


def test_16_corrupt_validated_scout_json_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    bad_scout = tmp_path / "scout_output.validated.json"
    bad_scout.write_text('{"broken": }', encoding="utf-8")
    with pytest.raises(PacketError, match="invalid validated scout JSON"):
        _build_packet(tmp_path, validated_scout_path=bad_scout, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in PACKET_FILES)


def test_17_invalid_validated_scout_schema_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    bad = dict(MANUAL_SMOKE_RAW)
    bad.pop("summary_one_liner")
    bad_path = tmp_path / "scout_output.validated.json"
    bad_path.write_text(json.dumps(bad, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(PacketError, match="summary_one_liner"):
        _build_packet(tmp_path, validated_scout_path=bad_path, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in PACKET_FILES)


def test_18_scout_validation_summary_status_not_ok_fails(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    bad_summary = _write_scout_validation_summary(tmp_path, status="error")
    with pytest.raises(PacketError, match="status must be ok"):
        _build_packet(tmp_path, scout_validation_summary_path=bad_summary, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in PACKET_FILES)


def test_19_universe_mismatch_fails_with_different_universe_toml_name(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    mismatch_universe = _write_universe(tmp_path, name="other-paper-v0")
    with pytest.raises(PacketError, match="universe mismatch"):
        _build_packet(tmp_path, universe_path=mismatch_universe, out_dir=out_dir)
    assert not any((out_dir / name).exists() for name in PACKET_FILES)


def test_20_cited_date_id_missing_from_date_md_fails_closed(tmp_path: Path) -> None:
    out_dir = tmp_path / "allocator"
    only_other = _sample_record(date_id="260528-9")
    with pytest.raises(PacketError, match="cited date_id missing from Date.md"):
        _build_packet(
            tmp_path,
            date_md_path=_write_date_md(tmp_path, only_other),
            store_path=_write_store(tmp_path, only_other),
            out_dir=out_dir,
        )
    assert not any((out_dir / name).exists() for name in PACKET_FILES)


def test_21_script_help_exits_zero() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0
