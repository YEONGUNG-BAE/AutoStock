from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_SMOKE = REPO_ROOT / "ops" / "run_date_md_smoke.py"
EXAMPLE_UNIVERSE = REPO_ROOT / "config" / "universe.paper.toml.example"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data import SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType
from research_source_intake import render_date_md
from run_date_md_smoke import SmokeError, main, parse_date_md_sections, run_date_md_smoke

KST = timezone(timedelta(hours=9))
KST_TS = "2026-05-28T09:00:00+09:00"
KST_CREATED = "2026-05-28T09:05:00+09:00"


def _sample_record(*, symbol: str = "SYNTH-KR-0001", market: str = "KR") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId("260528-1"),
        fact_type=FactType.MANUAL,
        source_name="operator-test",
        source_timestamp=__import__("datetime").datetime.fromisoformat(KST_TS),
        created_at=__import__("datetime").datetime.fromisoformat(KST_CREATED),
        summary="Synthetic manual research source for Foundation 8C test.",
        payload={"note": "synthetic", "score": 1},
        symbol=symbol,
        market=market,
        source_url="https://example.invalid/autostock/synthetic",
    )


def _write_universe(tmp_path: Path) -> Path:
    universe_path = tmp_path / "universe.toml"
    universe_path.write_text(EXAMPLE_UNIVERSE.read_text(encoding="utf-8"), encoding="utf-8")
    return universe_path


def _write_date_md(tmp_path: Path, record: DateIdSourceRecord) -> Path:
    date_md = tmp_path / "Date.md"
    date_md.write_text(render_date_md((record,)), encoding="utf-8")
    return date_md


def _write_store(tmp_path: Path, record: DateIdSourceRecord) -> Path:
    store_path = tmp_path / "date_id_sources.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    with store.transaction():
        store.save_record(record)
    store.close()
    return store_path


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(OPS_SMOKE), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def test_valid_date_md_smoke_passes_with_universe_and_store(tmp_path: Path) -> None:
    record = _sample_record()
    payload = run_date_md_smoke(
        universe_path=_write_universe(tmp_path),
        date_md_path=_write_date_md(tmp_path, record),
        store_path=_write_store(tmp_path, record),
        require_symbol_coverage=True,
        max_date_md_bytes=60_000,
    )

    assert payload["status"] == "ok"
    assert payload["date_ids_count"] == 1
    assert payload["missing_symbols"] == []


def test_valid_date_md_smoke_passes_without_store_using_bold_symbol_market_lines(
    tmp_path: Path,
) -> None:
    record = _sample_record()
    payload = run_date_md_smoke(
        universe_path=_write_universe(tmp_path),
        date_md_path=_write_date_md(tmp_path, record),
        store_path=None,
        require_symbol_coverage=True,
        max_date_md_bytes=60_000,
    )

    assert payload["status"] == "ok"
    assert payload["store_records_count"] is None
    assert payload["missing_symbols"] == []


def test_date_md_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(SmokeError, match="Date.md not found") as exc_info:
        run_date_md_smoke(
            universe_path=_write_universe(tmp_path),
            date_md_path=tmp_path / "missing.md",
            store_path=None,
            require_symbol_coverage=False,
            max_date_md_bytes=60_000,
        )
    assert exc_info.value.stage == "date_md"


def test_date_md_over_max_byte_limit_fails(tmp_path: Path) -> None:
    record = _sample_record()
    date_md = _write_date_md(tmp_path, record)

    with pytest.raises(SmokeError, match="exceeds max size") as exc_info:
        run_date_md_smoke(
            universe_path=_write_universe(tmp_path),
            date_md_path=date_md,
            store_path=None,
            require_symbol_coverage=False,
            max_date_md_bytes=10,
        )
    assert exc_info.value.stage == "date_md"


def test_date_md_invalid_date_id_heading_fails(tmp_path: Path) -> None:
    date_md = tmp_path / "Date.md"
    date_md.write_text("## [bad-id]\n\n- **source_timestamp:** x\n- **summary:** y\n- **payload_hash:** z\n", encoding="utf-8")

    with pytest.raises(SmokeError, match="invalid Date-ID heading"):
        parse_date_md_sections(date_md.read_text(encoding="utf-8"))


def test_date_md_section_missing_payload_hash_fails(tmp_path: Path) -> None:
    date_md = tmp_path / "Date.md"
    date_md.write_text(
        "## [260528-1]\n\n- **source_timestamp:** 2026-05-28T09:00:00+09:00\n- **summary:** test\n",
        encoding="utf-8",
    )

    with pytest.raises(SmokeError, match="missing payload_hash"):
        parse_date_md_sections(date_md.read_text(encoding="utf-8"))


def test_date_md_date_id_missing_from_store_fails_when_store_provided(tmp_path: Path) -> None:
    record = _sample_record()
    date_md = _write_date_md(tmp_path, record)
    store_path = tmp_path / "empty.sqlite3"
    SQLiteDateIdSourceStore(store_path).close()

    with pytest.raises(SmokeError, match="missing from store") as exc_info:
        run_date_md_smoke(
            universe_path=_write_universe(tmp_path),
            date_md_path=date_md,
            store_path=store_path,
            require_symbol_coverage=False,
            max_date_md_bytes=60_000,
        )
    assert exc_info.value.stage == "store"


def test_store_records_without_symbol_or_market_excluded_from_coverage(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.toml"
    universe_path.write_text(
        """
version = 1
name = "single"
description = "desc"
base_market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true
""",
        encoding="utf-8",
    )
    record_with_symbol = _sample_record()
    record_without_symbol = DateIdSourceRecord(
        date_id=DateId("260528-2"),
        fact_type=FactType.MANUAL,
        source_name="operator-test",
        source_timestamp=record_with_symbol.source_timestamp,
        created_at=record_with_symbol.created_at,
        summary="No symbol record.",
        payload={"note": "synthetic"},
    )
    date_md = tmp_path / "Date.md"
    date_md.write_text(render_date_md((record_with_symbol, record_without_symbol)), encoding="utf-8")
    store_path = tmp_path / "store.sqlite3"
    store = SQLiteDateIdSourceStore(store_path)
    with store.transaction():
        store.save_record(record_with_symbol)
        store.save_record(record_without_symbol)
    store.close()

    payload = run_date_md_smoke(
        universe_path=universe_path,
        date_md_path=date_md,
        store_path=store_path,
        require_symbol_coverage=True,
        max_date_md_bytes=60_000,
    )

    assert payload["status"] == "ok"
    assert payload["missing_symbols"] == []


def test_require_symbol_coverage_fails_when_enabled_universe_symbol_missing(tmp_path: Path) -> None:
    universe_path = tmp_path / "universe.toml"
    universe_path.write_text(
        """
version = 1
name = "needs-coverage"
description = "desc"
base_market = "KR"

[[symbols]]
symbol = "SYNTH-KR-0001"
market = "KR"
enabled = true

[[symbols]]
symbol = "SYNTH-KR-0002"
market = "KR"
enabled = true
""",
        encoding="utf-8",
    )
    record = _sample_record(symbol="SYNTH-KR-0001")
    date_md = _write_date_md(tmp_path, record)

    with pytest.raises(SmokeError, match="missing symbol coverage") as exc_info:
        run_date_md_smoke(
            universe_path=universe_path,
            date_md_path=date_md,
            store_path=None,
            require_symbol_coverage=True,
            max_date_md_bytes=60_000,
        )
    assert exc_info.value.stage == "coverage"


def test_without_require_symbol_coverage_missing_symbols_reported_but_exit_ok(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    universe_path = tmp_path / "universe.toml"
    universe_path.write_text(EXAMPLE_UNIVERSE.read_text(encoding="utf-8"), encoding="utf-8")
    record = _sample_record(symbol="OTHER-SYMBOL", market="KR")
    date_md = _write_date_md(tmp_path, record)

    exit_code = main(
        [
            "--universe",
            str(universe_path),
            "--date-md",
            str(date_md),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["missing_symbols"] != []


def test_json_output_is_parseable_and_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _sample_record()
    exit_code = main(
        [
            "--universe",
            str(_write_universe(tmp_path)),
            "--date-md",
            str(_write_date_md(tmp_path, record)),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())

    assert exit_code == 0
    assert payload["status"] == "ok"
    assert "app_key" not in captured.out.lower()
    assert "payload" not in payload


def test_json_and_verbose_keeps_stdout_pure_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    record = _sample_record()
    exit_code = main(
        [
            "--universe",
            str(_write_universe(tmp_path)),
            "--date-md",
            str(_write_date_md(tmp_path, record)),
            "--json",
            "--verbose",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    json.loads(captured.out.strip())
    assert "verbose:" in captured.err


def test_script_help_exits_zero() -> None:
    result = _run_cli("--help")
    assert result.returncode == 0


def test_smoke_script_creates_no_output_files(tmp_path: Path) -> None:
    record = _sample_record()
    universe_path = _write_universe(tmp_path)
    date_md_path = _write_date_md(tmp_path, record)
    store_path = _write_store(tmp_path, record)
    before = {path for path in tmp_path.rglob("*")}

    run_date_md_smoke(
        universe_path=universe_path,
        date_md_path=date_md_path,
        store_path=store_path,
        require_symbol_coverage=True,
        max_date_md_bytes=60_000,
    )

    after = {path for path in tmp_path.rglob("*")}
    assert before == after
