"""Real Intake 3E4 — combined FRED+PRICE+DART context budget cap tests (fixtures only)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
INTAKE_SCRIPT = REPO_ROOT / "ops" / "research_source_intake.py"
SMOKE_SCRIPT = REPO_ROOT / "ops" / "run_date_md_smoke.py"
SCOUT_SCRIPT = REPO_ROOT / "ops" / "build_scout_manual_packet.py"
COMBINED_SCRIPT = REPO_ROOT / "ops" / "build_kr_real_combined_context_smoke.py"

KST = timezone(timedelta(hours=9))
AS_OF = datetime(2026, 5, 30, 13, 0, 0, tzinfo=KST)
DISCLOSURES_PER_SYMBOL = 80
DISCLOSURE_CAP = 5
PRICE_CAP = 1

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from data import SQLiteDateIdSourceStore
from data.source_record_context_selector import (
    ContextBudgetCaps,
    KR_REAL_SMOKE_CONTEXT_BUDGET,
    select_context_records,
)
from domain import DateId, DateIdSourceRecord, FactType
from research_source_intake import export_date_md, render_date_md, run_export_only, run_normal
from run_date_md_smoke import SmokeError, run_date_md_smoke


def _macro_record(*, date_id: str, source_timestamp: datetime) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.MACRO,
        source_name="fred",
        source_timestamp=source_timestamp,
        created_at=AS_OF,
        summary="US 10Y Treasury yield observation",
        payload={"series_id": "DGS10", "value": "4.25"},
        symbol=None,
        market=None,
        source_url=None,
    )


def _price_record(
    *,
    date_id: str,
    symbol: str,
    source_timestamp: datetime,
    price: float,
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.PRICE,
        source_name="yfinance",
        source_timestamp=source_timestamp,
        created_at=AS_OF,
        summary=f"{symbol} close {price:.0f} KRW",
        payload={"symbol": symbol, "market": "KR", "price": str(price), "currency": "KRW"},
        symbol=symbol,
        market="KR",
        source_url=None,
    )


def _disclosure_record(
    *,
    date_id: str,
    symbol: str,
    source_timestamp: datetime,
    index: int,
) -> DateIdSourceRecord:
    title = (
        f"Synthetic DART disclosure #{index} for {symbol} with extended summary padding "
        f"to inflate Date.md export size when uncapped for budget regression testing."
    )
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.DISCLOSURE,
        source_name="dart",
        source_timestamp=source_timestamp,
        created_at=AS_OF,
        summary=title,
        payload={"title": title, "receipt_no": f"20260530{index:04d}"},
        symbol=symbol,
        market=None,
        source_url=f"https://dart.fss.or.kr/example/{symbol}/{index}",
    )


def _build_combined_records() -> tuple[DateIdSourceRecord, ...]:
    records: list[DateIdSourceRecord] = []
    seq = 1

    def next_date_id() -> str:
        nonlocal seq
        token = f"260530-{seq}"
        seq += 1
        return token

    records.append(
        _macro_record(
            date_id=next_date_id(),
            source_timestamp=datetime(2026, 5, 30, 8, 0, 0, tzinfo=KST),
        )
    )
    records.append(
        _price_record(
            date_id=next_date_id(),
            symbol="005930",
            source_timestamp=datetime(2026, 5, 30, 9, 0, 0, tzinfo=KST),
            price=70000.0,
        )
    )
    records.append(
        _price_record(
            date_id=next_date_id(),
            symbol="000660",
            source_timestamp=datetime(2026, 5, 30, 9, 5, 0, tzinfo=KST),
            price=130000.0,
        )
    )

    for symbol in ("005930", "000660"):
        for index in range(DISCLOSURES_PER_SYMBOL):
            ts = datetime(2026, 5, 30, 10, 0, 0, tzinfo=KST) + timedelta(minutes=index)
            records.append(
                _disclosure_record(
                    date_id=next_date_id(),
                    symbol=symbol,
                    source_timestamp=ts,
                    index=index + 1,
                )
            )
    return tuple(records)


def _write_jsonl(path: Path, records: tuple[DateIdSourceRecord, ...]) -> None:
    lines = [json.dumps(record.model_dump(mode="json"), ensure_ascii=False) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_store(path: Path, records: tuple[DateIdSourceRecord, ...]) -> None:
    store = SQLiteDateIdSourceStore(path)
    with store.transaction():
        for record in records:
            store.save_record(record)
    store.close()


def _run_intake_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(INTAKE_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _run_date_md_smoke_cli(
    *,
    date_md: Path,
    store: Path,
    require_symbol_coverage: bool,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    argv = [
        sys.executable,
        str(SMOKE_SCRIPT),
        "--universe",
        str(KR_REAL_UNIVERSE),
        "--date-md",
        str(date_md),
        "--store",
        str(store),
        "--json",
    ]
    if require_symbol_coverage:
        argv.append("--require-symbol-coverage")
    return subprocess.run(argv, capture_output=True, text=True, cwd=REPO_ROOT, env=env)


def _run_scout_cli(*, date_md: Path, store: Path, out_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            str(SCOUT_SCRIPT),
            "--universe",
            str(KR_REAL_UNIVERSE),
            "--date-md",
            str(date_md),
            "--store",
            str(store),
            "--out-dir",
            str(out_dir),
            "--market-scope",
            "KR",
            "--require-symbol-coverage",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def test_selector_keeps_latest_price_per_symbol() -> None:
    older = _price_record(
        date_id="260530-10",
        symbol="005930",
        source_timestamp=datetime(2026, 5, 29, 9, 0, 0, tzinfo=KST),
        price=69000.0,
    )
    newer = _price_record(
        date_id="260530-11",
        symbol="005930",
        source_timestamp=datetime(2026, 5, 30, 9, 0, 0, tzinfo=KST),
        price=70000.0,
    )
    caps = ContextBudgetCaps(max_price_per_symbol_source=1)
    selected = select_context_records((older, newer), caps=caps)
    assert [record.date_id.value for record in selected] == ["260530-11"]


def test_selector_keeps_latest_n_disclosures_per_symbol() -> None:
    records = tuple(
        _disclosure_record(
            date_id=f"260530-{index + 1}",
            symbol="005930",
            source_timestamp=datetime(2026, 5, 30, 10, index, 0, tzinfo=KST),
            index=index + 1,
        )
        for index in range(8)
    )
    caps = ContextBudgetCaps(max_disclosure_per_symbol_source=3)
    selected = select_context_records(records, caps=caps)
    assert len(selected) == 3
    assert [record.date_id.value for record in selected] == ["260530-6", "260530-7", "260530-8"]


def test_selector_keeps_latest_global_macro_records() -> None:
    records = tuple(
        _macro_record(
            date_id=f"260530-{index + 1}",
            source_timestamp=datetime(2026, 5, 30, 7, index, 0, tzinfo=KST),
        )
        for index in range(4)
    )
    caps = ContextBudgetCaps(max_global_per_fact_type_source=2)
    selected = select_context_records(records, caps=caps)
    assert [record.date_id.value for record in selected] == ["260530-3", "260530-4"]


def test_selector_tie_break_is_deterministic_by_date_id() -> None:
    same_ts = datetime(2026, 5, 30, 10, 0, 0, tzinfo=KST)
    first = _disclosure_record(date_id="260530-2", symbol="005930", source_timestamp=same_ts, index=1)
    second = _disclosure_record(date_id="260530-3", symbol="005930", source_timestamp=same_ts, index=2)
    caps = ContextBudgetCaps(max_disclosure_per_symbol_source=1)
    selected = select_context_records((first, second), caps=caps)
    assert [record.date_id.value for record in selected] == ["260530-2"]


def test_uncapped_date_md_exceeds_budget_for_large_disclosure_store(tmp_path: Path) -> None:
    records = _build_combined_records()
    date_md = tmp_path / "Date.md"
    export_date_md(records, date_md)
    assert len(date_md.read_bytes()) > 60_000
    with pytest.raises(SmokeError, match="exceeds max size"):
        run_date_md_smoke(
            universe_path=KR_REAL_UNIVERSE,
            date_md_path=date_md,
            store_path=None,
            require_symbol_coverage=False,
            max_date_md_bytes=60_000,
        )


def test_capped_date_md_stays_below_budget_and_passes_smoke(tmp_path: Path) -> None:
    records = _build_combined_records()
    store_path = tmp_path / "date_id_sources.sqlite3"
    date_md = tmp_path / "Date.md"
    _write_store(store_path, records)
    run_export_only(
        store_path=store_path,
        date_md_out=date_md,
        force_date_md=False,
        context_budget_caps=KR_REAL_SMOKE_CONTEXT_BUDGET,
        context_budget_profile="kr-real-smoke",
    )
    assert len(date_md.read_bytes()) <= 60_000
    payload = run_date_md_smoke(
        universe_path=KR_REAL_UNIVERSE,
        date_md_path=date_md,
        store_path=store_path,
        require_symbol_coverage=True,
        max_date_md_bytes=60_000,
    )
    assert payload["status"] == "ok"
    assert payload["missing_symbols"] == []


def test_normal_mode_without_caps_exports_all_records_unchanged(tmp_path: Path) -> None:
    records = _build_combined_records()[:5]
    source = tmp_path / "combined.jsonl"
    store = tmp_path / "store.sqlite3"
    date_md = tmp_path / "Date.md"
    _write_jsonl(source, records)
    payload = run_normal(
        source_jsonl=source,
        store_path=store,
        date_md_out=date_md,
        force_date_md=False,
        context_budget_caps=None,
    )
    assert payload["records_saved"] == len(records)
    assert payload["records_exported"] == len(records)
    assert date_md.read_text(encoding="utf-8") == render_date_md(records)


def test_combined_8b_8c_scout_flow_with_context_profile(tmp_path: Path) -> None:
    records = _build_combined_records()
    source = tmp_path / "combined.jsonl"
    store = tmp_path / "store.sqlite3"
    date_md = tmp_path / "Date.md"
    scout_out = tmp_path / "scout"
    _write_jsonl(source, records)

    intake = run_normal(
        source_jsonl=source,
        store_path=store,
        date_md_out=date_md,
        force_date_md=False,
        context_budget_caps=KR_REAL_SMOKE_CONTEXT_BUDGET,
        context_budget_profile="kr-real-smoke",
    )
    assert intake["records_saved"] == len(records)
    assert intake["records_exported"] < intake["records_saved"]
    assert len(date_md.read_bytes()) <= 60_000

    smoke = run_date_md_smoke(
        universe_path=KR_REAL_UNIVERSE,
        date_md_path=date_md,
        store_path=store,
        require_symbol_coverage=True,
        max_date_md_bytes=60_000,
    )
    assert smoke["missing_symbols"] == []

    from build_scout_manual_packet import run_build_scout_manual_packet

    scout = run_build_scout_manual_packet(
        universe_path=KR_REAL_UNIVERSE,
        date_md_path=date_md,
        store_path=store,
        out_dir=scout_out,
        now=AS_OF,
        market_scope="KR",
        fact_types=None,
        max_records=None,
        require_symbol_coverage=True,
        force=False,
    )
    assert scout["records_count"] > 0

    scout_input = json.loads((scout_out / "scout_input.json").read_text(encoding="utf-8"))
    by_type = {}
    for record in scout_input["records"]:
        by_type.setdefault(record["fact_type"], []).append(record)

    assert "macro" in by_type
    assert len(by_type["price"]) == 2
    assert {record["symbol"] for record in by_type["price"]} == {"005930", "000660"}
    assert len(by_type["disclosure"]) == DISCLOSURE_CAP * 2
    assert {record["symbol"] for record in by_type["disclosure"]} == {"005930", "000660"}
    assert all(record["market"] is None for record in by_type["disclosure"])


def test_combined_helper_orchestrates_end_to_end(tmp_path: Path) -> None:
    from build_kr_real_combined_context_smoke import run_kr_real_combined_context_smoke

    records = _build_combined_records()
    source = tmp_path / "combined.jsonl"
    store = tmp_path / "store.sqlite3"
    date_md = tmp_path / "Date.md"
    scout_out = tmp_path / "scout"
    _write_jsonl(source, records)

    payload = run_kr_real_combined_context_smoke(
        universe_path=KR_REAL_UNIVERSE,
        source_jsonl=source,
        store_path=store,
        date_md_out=date_md,
        scout_out_dir=scout_out,
        now=AS_OF,
    )
    assert payload["status"] == "ok"
    assert payload["missing_symbols"] == []
    assert payload["date_md_bytes"] <= 60_000
    assert payload["records_saved"] == len(records)
    assert payload["records_exported"] < payload["records_saved"]
    assert (scout_out / "scout_input.json").is_file()


def test_dart_only_store_still_fails_symbol_coverage(tmp_path: Path) -> None:
    records = tuple(
        _disclosure_record(
            date_id=f"260530-{index + 1}",
            symbol=symbol,
            source_timestamp=datetime(2026, 5, 30, 10, 0, 0, tzinfo=KST),
            index=1,
        )
        for index, symbol in enumerate(("005930", "000660"))
    )
    store = tmp_path / "store.sqlite3"
    date_md = tmp_path / "Date.md"
    _write_store(store, records)
    run_export_only(
        store_path=store,
        date_md_out=date_md,
        force_date_md=False,
        context_budget_caps=KR_REAL_SMOKE_CONTEXT_BUDGET,
    )
    with pytest.raises(SmokeError, match="missing symbol coverage"):
        run_date_md_smoke(
            universe_path=KR_REAL_UNIVERSE,
            date_md_path=date_md,
            store_path=store,
            require_symbol_coverage=True,
            max_date_md_bytes=60_000,
        )


def test_intake_cli_context_profile_flag(tmp_path: Path) -> None:
    records = _build_combined_records()
    source = tmp_path / "combined.jsonl"
    store = tmp_path / "store.sqlite3"
    date_md = tmp_path / "Date.md"
    _write_jsonl(source, records)

    result = _run_intake_cli(
        "--source-jsonl",
        str(source),
        "--store",
        str(store),
        "--date-md-out",
        str(date_md),
        "--context-budget-profile",
        "kr-real-smoke",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["records_exported"] < payload["records_saved"]
    assert payload["context_budget_profile"] == "kr-real-smoke"


def test_combined_script_has_no_forbidden_tokens() -> None:
    source = COMBINED_SCRIPT.read_text(encoding="utf-8").lower()
    forbidden = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
        "import yfinance",
        "from yfinance",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
        "fred_api_key",
        "dart_api_key",
        "os.environ",
        "getenv",
    )
    for token in forbidden:
        assert token not in source, f"combined smoke script must not reference {token!r}"


def test_no_runtime_files_tracked_in_repo() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runtime"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert tracked.returncode == 0
    assert tracked.stdout.strip() == ""


def test_existing_3e2_and_3e3_smoke_tests_remain_importable() -> None:
    import test_kr_real_dart_smoke  # noqa: F401
    import test_kr_real_price_smoke  # noqa: F401
