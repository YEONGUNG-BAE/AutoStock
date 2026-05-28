from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SCRIPT = REPO_ROOT / "ops" / "build_scout_manual_packet.py"
EXAMPLE_UNIVERSE = REPO_ROOT / "config" / "universe.paper.toml.example"
ACCEPTANCE_CHECK = REPO_ROOT / "ops" / "acceptance_check.sh"
RUNBOOK = REPO_ROOT / "docs" / "RUNBOOK.md"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType
from research_source_intake import render_date_md
from build_scout_manual_packet import (
    OUTPUT_FILES,
    PacketError,
    main,
    run_build_scout_manual_packet,
)
from scout.models import SUMMARY_ONE_LINER_MAX_LENGTH, ScoutInput

KST = timezone(timedelta(hours=9))
KST_TS = "2026-05-28T09:00:00+09:00"
KST_CREATED = "2026-05-28T09:05:00+09:00"


def _sample_record(
    *,
    date_id: str = "260528-1",
    symbol: str | None = "SYNTH-KR-0001",
    market: str | None = "KR",
    fact_type: FactType = FactType.MANUAL,
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=fact_type,
        source_name="operator-test",
        source_timestamp=__import__("datetime").datetime.fromisoformat(KST_TS),
        created_at=__import__("datetime").datetime.fromisoformat(KST_CREATED),
        summary="Synthetic manual research source for Foundation 8D test.",
        payload={"note": "synthetic", "score": 1},
        symbol=symbol,
        market=market,
        source_url="https://example.invalid/autostock/synthetic",
    )


def _write_universe(tmp_path: Path, *, text: str | None = None) -> Path:
    universe_path = tmp_path / "universe.toml"
    universe_path.write_text(
        text if text is not None else EXAMPLE_UNIVERSE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return universe_path


def _write_date_md(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    date_md = tmp_path / "Date.md"
    date_md.write_text(render_date_md(records), encoding="utf-8")
    return date_md


def _write_store(tmp_path: Path, *records: DateIdSourceRecord) -> Path:
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    with store.transaction():
        for record in records:
            store.save_record(record)
    store.close()
    return store_path


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


def _build_packet(tmp_path: Path, **kwargs: object) -> dict[str, object]:
    universe_path = kwargs.pop("universe_path", _write_universe(tmp_path))
    records = kwargs.pop("records", (_sample_record(),))
    date_md_path = kwargs.pop("date_md_path", _write_date_md(tmp_path, *records))
    store_path = kwargs.pop("store_path", _write_store(tmp_path, *records))
    out_dir = kwargs.pop("out_dir", tmp_path / "scout")
    return run_build_scout_manual_packet(
        universe_path=universe_path,
        date_md_path=date_md_path,
        store_path=store_path,
        out_dir=out_dir,
        now=__import__("datetime").datetime.fromisoformat("2026-05-28T00:00:00+00:00"),
        market_scope=kwargs.pop("market_scope", "KR"),
        fact_types=kwargs.pop("fact_types", None),
        max_records=kwargs.pop("max_records", None),
        require_symbol_coverage=kwargs.pop("require_symbol_coverage", True),
        force=kwargs.pop("force", False),
    )


def test_valid_packet_build_writes_expected_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "scout"
    payload = _build_packet(tmp_path, out_dir=out_dir)

    assert payload["status"] == "ok"
    for name in OUTPUT_FILES:
        assert (out_dir / name).is_file()


def test_scout_input_json_round_trips(tmp_path: Path) -> None:
    out_dir = tmp_path / "scout"
    _build_packet(tmp_path, out_dir=out_dir)

    raw = (out_dir / "scout_input.json").read_text(encoding="utf-8")
    scout_input = ScoutInput.model_validate(json.loads(raw))
    assert scout_input.universe == "paper-v0"
    assert len(scout_input.records) == 1


def test_scout_prompt_includes_required_rules(tmp_path: Path) -> None:
    out_dir = tmp_path / "scout"
    _build_packet(tmp_path, out_dir=out_dir)

    prompt = (out_dir / "scout_prompt.md").read_text(encoding="utf-8")
    assert "JSON only" in prompt
    assert "Do **not** wrap JSON in markdown fences" in prompt
    assert "Cite **only** Date-IDs" in prompt
    assert "Do not produce orders" in prompt or "not produce orders" in prompt
    assert str(SUMMARY_ONE_LINER_MAX_LENGTH) in prompt
    assert "SUMMARY_ONE_LINER_MAX_LENGTH" in prompt


def test_existing_output_files_fail_without_force(tmp_path: Path) -> None:
    out_dir = tmp_path / "scout"
    universe_path = _write_universe(tmp_path)
    record = _sample_record()
    date_md_path = _write_date_md(tmp_path, record)
    store_path = _write_store(tmp_path, record)
    now = __import__("datetime").datetime.fromisoformat("2026-05-28T00:00:00+00:00")

    run_build_scout_manual_packet(
        universe_path=universe_path,
        date_md_path=date_md_path,
        store_path=store_path,
        out_dir=out_dir,
        now=now,
        market_scope="KR",
        fact_types=None,
        max_records=None,
        require_symbol_coverage=True,
        force=False,
    )
    (out_dir / "scout_input.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PacketError, match="output files already exist") as exc_info:
        run_build_scout_manual_packet(
            universe_path=universe_path,
            date_md_path=date_md_path,
            store_path=store_path,
            out_dir=out_dir,
            now=now,
            market_scope="KR",
            fact_types=None,
            max_records=None,
            require_symbol_coverage=True,
            force=False,
        )
    assert exc_info.value.stage == "write"


def test_force_overwrites_expected_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "scout"
    universe_path = _write_universe(tmp_path)
    record = _sample_record()
    date_md_path = _write_date_md(tmp_path, record)
    store_path = _write_store(tmp_path, record)
    now = __import__("datetime").datetime.fromisoformat("2026-05-28T00:00:00+00:00")

    run_build_scout_manual_packet(
        universe_path=universe_path,
        date_md_path=date_md_path,
        store_path=store_path,
        out_dir=out_dir,
        now=now,
        market_scope="KR",
        fact_types=None,
        max_records=None,
        require_symbol_coverage=True,
        force=False,
    )
    (out_dir / "scout_input.json").write_text("{}", encoding="utf-8")

    payload = run_build_scout_manual_packet(
        universe_path=universe_path,
        date_md_path=date_md_path,
        store_path=store_path,
        out_dir=out_dir,
        now=now,
        market_scope="KR",
        fact_types=None,
        max_records=None,
        require_symbol_coverage=True,
        force=True,
    )
    assert payload["status"] == "ok"
    scout_input = json.loads((out_dir / "scout_input.json").read_text(encoding="utf-8"))
    assert "records" in scout_input


def test_json_output_is_parseable_and_sanitized(tmp_path: Path) -> None:
    out_dir = tmp_path / "scout"
    exit_code = main(
        [
            "--universe",
            str(_write_universe(tmp_path)),
            "--date-md",
            str(_write_date_md(tmp_path, _sample_record())),
            "--store",
            str(_write_store(tmp_path, _sample_record())),
            "--out-dir",
            str(out_dir),
            "--require-symbol-coverage",
            "--json",
        ]
    )
    assert exit_code == 0


def test_json_and_verbose_keeps_stdout_pure_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out_dir = tmp_path / "scout"
    exit_code = main(
        [
            "--universe",
            str(_write_universe(tmp_path)),
            "--date-md",
            str(_write_date_md(tmp_path, _sample_record())),
            "--store",
            str(_write_store(tmp_path, _sample_record())),
            "--out-dir",
            str(out_dir),
            "--json",
            "--verbose",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out.strip())
    assert payload["status"] == "ok"
    assert "verbose:" in captured.err


def test_invalid_now_timezone_naive_fails_closed(tmp_path: Path) -> None:
    result = _run_cli(
        "--universe",
        str(_write_universe(tmp_path)),
        "--date-md",
        str(_write_date_md(tmp_path, _sample_record())),
        "--store",
        str(_write_store(tmp_path, _sample_record())),
        "--out-dir",
        str(tmp_path / "scout"),
        "--now",
        "2026-05-28T00:00:00",
        "--json",
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout.strip())
    assert payload["status"] == "error"
    assert payload["stage"] == "args"


def test_invalid_fact_type_fails_closed(tmp_path: Path) -> None:
    result = _run_cli(
        "--universe",
        str(_write_universe(tmp_path)),
        "--date-md",
        str(_write_date_md(tmp_path, _sample_record())),
        "--store",
        str(_write_store(tmp_path, _sample_record())),
        "--out-dir",
        str(tmp_path / "scout"),
        "--fact-type",
        "not-a-fact",
    )
    assert result.returncode != 0


def test_max_records_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(PacketError, match="max_records must be a positive integer"):
        run_build_scout_manual_packet(
            universe_path=_write_universe(tmp_path),
            date_md_path=_write_date_md(tmp_path, _sample_record()),
            store_path=_write_store(tmp_path, _sample_record()),
            out_dir=tmp_path / "scout",
            now=__import__("datetime").datetime.fromisoformat("2026-05-28T00:00:00+00:00"),
            market_scope="KR",
            fact_types=None,
            max_records=0,
            require_symbol_coverage=True,
            force=False,
        )


def test_universe_enabled_symbols_filter_exact_market_symbol_pairs(tmp_path: Path) -> None:
    matching = _sample_record(symbol="SYNTH-KR-0001", market="KR")
    wrong_symbol = _sample_record(
        date_id="260528-2",
        symbol="SYNTH-KR-9999",
        market="KR",
    )
    out_dir = tmp_path / "scout"
    payload = _build_packet(
        tmp_path,
        records=(matching, wrong_symbol),
        out_dir=out_dir,
        require_symbol_coverage=False,
    )
    assert payload["records_count"] == 1


def test_disabled_universe_symbols_are_excluded(tmp_path: Path) -> None:
    disabled_us = _sample_record(
        date_id="260528-2",
        symbol="SYNTH-US-0001",
        market="US",
    )
    out_dir = tmp_path / "scout"
    payload = _build_packet(
        tmp_path,
        records=(_sample_record(), disabled_us),
        out_dir=out_dir,
        market_scope="BOTH",
        require_symbol_coverage=False,
    )
    assert payload["records_count"] == 1


def test_global_records_are_included_by_default(tmp_path: Path) -> None:
    global_record = _sample_record(date_id="260528-2", symbol=None, market=None)
    out_dir = tmp_path / "scout"
    payload = _build_packet(
        tmp_path,
        records=(_sample_record(), global_record),
        out_dir=out_dir,
        require_symbol_coverage=False,
    )
    assert payload["records_count"] == 2


def test_partial_symbol_market_records_are_excluded(tmp_path: Path) -> None:
    partial_symbol = _sample_record(date_id="260528-2", symbol="SYNTH-KR-0001", market=None)
    partial_market = _sample_record(date_id="260528-3", symbol=None, market="KR")
    out_dir = tmp_path / "scout"
    payload = _build_packet(
        tmp_path,
        records=(_sample_record(), partial_symbol, partial_market),
        out_dir=out_dir,
        require_symbol_coverage=False,
    )
    assert payload["records_count"] == 1


def test_date_md_smoke_failure_prevents_output_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "scout"
    with pytest.raises(PacketError):
        run_build_scout_manual_packet(
            universe_path=_write_universe(tmp_path),
            date_md_path=tmp_path / "missing.md",
            store_path=_write_store(tmp_path, _sample_record()),
            out_dir=out_dir,
            now=__import__("datetime").datetime.fromisoformat("2026-05-28T00:00:00+00:00"),
            market_scope="KR",
            fact_types=None,
            max_records=None,
            require_symbol_coverage=True,
            force=False,
        )
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_require_symbol_coverage_failure_prevents_output_files(tmp_path: Path) -> None:
    # universe enabled symbol과 store record symbol이 불일치 → coverage fail
    record = _sample_record(symbol="SYNTH-KR-9999", market="KR")
    out_dir = tmp_path / "scout"
    with pytest.raises(PacketError, match="missing symbol coverage"):
        run_build_scout_manual_packet(
            universe_path=_write_universe(tmp_path),
            date_md_path=_write_date_md(tmp_path, record),
            store_path=_write_store(tmp_path, record),
            out_dir=out_dir,
            now=__import__("datetime").datetime.fromisoformat("2026-05-28T00:00:00+00:00"),
            market_scope="KR",
            fact_types=None,
            max_records=None,
            require_symbol_coverage=True,
            force=False,
        )
    assert not any((out_dir / name).exists() for name in OUTPUT_FILES)


def test_market_scope_kr_excludes_enabled_us_symbol_records(tmp_path: Path) -> None:
    mixed_universe = """
version = 1
name = "mixed-v0"
description = "Mixed market test universe."
base_market = "BOTH"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true

[[symbols]]
symbol = "SYNTH-US-0001"
market = "US"
enabled = true
"""
    kr_record = _sample_record(date_id="260528-1", symbol="SYNTH-KR-0001", market="KR")
    us_record = _sample_record(date_id="260528-2", symbol="SYNTH-US-0001", market="US")
    out_dir = tmp_path / "scout"
    payload = _build_packet(
        tmp_path,
        universe_path=_write_universe(tmp_path, text=mixed_universe),
        records=(kr_record, us_record),
        out_dir=out_dir,
        market_scope="KR",
        require_symbol_coverage=False,
    )
    assert payload["records_count"] == 1
    scout_input = ScoutInput.model_validate(
        json.loads((out_dir / "scout_input.json").read_text(encoding="utf-8"))
    )
    assert all(record.market == "KR" for record in scout_input.records if record.market is not None)


def test_script_help_exits_zero() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0


def test_script_does_not_create_raw_output_file(tmp_path: Path) -> None:
    out_dir = tmp_path / "scout"
    _build_packet(tmp_path, out_dir=out_dir)
    raw_files = list(out_dir.glob("scout_output.*.raw.json"))
    validated_files = list(out_dir.glob("*.validated.json"))
    assert raw_files == []
    assert validated_files == []


def test_pytest_baseline_synchronized_between_runbook_and_acceptance_check() -> None:
    acceptance_text = ACCEPTANCE_CHECK.read_text(encoding="utf-8")
    runbook_text = RUNBOOK.read_text(encoding="utf-8")

    acceptance_match = re.search(r'grep -q "(\d+) passed"', acceptance_text)
    assert acceptance_match is not None, "acceptance_check.sh missing pytest baseline grep pattern"

    baseline = acceptance_match.group(1)
    assert f"pytest: {baseline} passed" in acceptance_text
    assert f"pytest baseline mismatch(`{baseline} passed`" in runbook_text
    assert f"**pytest baseline:** `{baseline} passed`" in runbook_text

    runbook_counts = re.findall(r"(\d+) passed", runbook_text)
    acceptance_counts = re.findall(r"(\d+) passed", acceptance_text)
    assert len(set(runbook_counts)) == 1
    assert len(set(acceptance_counts)) == 1
    assert runbook_counts[0] == acceptance_counts[0] == baseline
