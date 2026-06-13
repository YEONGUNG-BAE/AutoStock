from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.sqlite_inspector import (
    SqliteInspectionError,
    inspect_sqlite_file,
    open_read_only,
    summarize_active_store,
    summarize_journal,
    summarize_ledger,
)

LEDGER_SCHEMA = """
CREATE TABLE order_intents (order_id TEXT PRIMARY KEY, symbol TEXT, market TEXT);
CREATE TABLE order_results (order_id TEXT PRIMARY KEY, status TEXT);
CREATE TABLE fills (fill_id TEXT PRIMARY KEY, order_id TEXT);
CREATE TABLE current_cash (currency TEXT, account_role TEXT, amount TEXT, PRIMARY KEY (currency, account_role));
CREATE TABLE current_positions (
    symbol TEXT, market TEXT, account_role TEXT, currency TEXT, quantity TEXT,
    PRIMARY KEY (symbol, market, account_role)
);
"""

JOURNAL_SCHEMA = """
CREATE TABLE trigger_fire_journal (
    idempotency_key TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('reserved','dispatching','committed','aborted','uncertain'))
);
"""

ACTIVE_SCHEMA = """
CREATE TABLE decision_bundle_versions (
    publication_id TEXT PRIMARY KEY, decision_id TEXT, plan_id TEXT
);
CREATE TABLE active_decision_pointers (
    market TEXT, symbol TEXT, publication_id TEXT, PRIMARY KEY (market, symbol)
);
CREATE TABLE decision_refresh_slots (slot_id TEXT PRIMARY KEY);
"""


def _make_db(path: Path, schema: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    return conn


def _ledger_db(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.sqlite3"
    conn = _make_db(path, LEDGER_SCHEMA)
    conn.execute("INSERT INTO order_intents VALUES ('o1', '005930', 'KR')")
    conn.execute("INSERT INTO order_results VALUES ('o1', 'FILLED')")
    conn.execute("INSERT INTO fills VALUES ('f1', 'o1')")
    conn.execute("INSERT INTO current_cash VALUES ('KRW', 'PAPER', '96010000')")
    conn.execute("INSERT INTO current_positions VALUES ('005930', 'KR', 'PAPER', 'KRW', '57')")
    conn.commit()
    conn.close()
    return path


def _journal_db(tmp_path: Path) -> Path:
    path = tmp_path / "journal.sqlite3"
    conn = _make_db(path, JOURNAL_SCHEMA)
    conn.execute("INSERT INTO trigger_fire_journal VALUES ('k1', 'committed')")
    conn.execute("INSERT INTO trigger_fire_journal VALUES ('k2', 'aborted')")
    conn.execute("INSERT INTO trigger_fire_journal VALUES ('k3', 'reserved')")
    conn.commit()
    conn.close()
    return path


def _active_db(tmp_path: Path) -> Path:
    path = tmp_path / "active.sqlite3"
    conn = _make_db(path, ACTIVE_SCHEMA)
    conn.execute("INSERT INTO decision_bundle_versions VALUES ('p1', 'dec-1', 'plan-1')")
    conn.execute("INSERT INTO active_decision_pointers VALUES ('KR', '005930', 'p1')")
    conn.execute("INSERT INTO decision_refresh_slots VALUES ('s1')")
    conn.commit()
    conn.close()
    return path


def test_inspect_sqlite_file_reports_tables_and_counts(tmp_path: Path) -> None:
    inspection = inspect_sqlite_file(_ledger_db(tmp_path))
    assert inspection.user_version == 0
    assert "current_positions" in inspection.tables
    counts = {rc.table: rc.row_count for rc in inspection.row_counts}
    assert counts["order_intents"] == 1
    assert counts["fills"] == 1


def test_summarize_ledger_is_sanitized(tmp_path: Path) -> None:
    summary = summarize_ledger(_ledger_db(tmp_path), symbol="005930", market="KR")
    assert summary.fill_count == 1
    # order_results.status는 실제 domain.OrderStatus(FILLED 등)로 집계된다 — 존재하지 않는
    # 'COMMITTED'는 항상 0이므로 per-status 카운트로 검증한다.
    assert summary.order_result_count == 1
    assert summary.filled_result_count == 1
    assert summary.rejected_result_count == 0
    assert summary.pending_result_count == 0
    assert summary.cancelled_result_count == 0
    assert summary.position_quantity == "57"
    assert summary.cash_entry_count == 1


def test_summarize_ledger_unknown_symbol_has_no_position(tmp_path: Path) -> None:
    summary = summarize_ledger(_ledger_db(tmp_path), symbol="000660", market="KR")
    assert summary.position_quantity is None


def test_summarize_journal_groups_by_state(tmp_path: Path) -> None:
    summary = summarize_journal(_journal_db(tmp_path))
    assert summary.total_rows == 3
    assert summary.terminal_count == 2
    assert summary.nonterminal_count == 1
    states = {sc.state: sc.count for sc in summary.state_counts}
    assert states == {"aborted": 1, "committed": 1, "reserved": 1}


def test_summarize_active_store_resolves_pointer(tmp_path: Path) -> None:
    summary = summarize_active_store(_active_db(tmp_path), symbol="005930", market="KR")
    assert summary.bundle_version_count == 1
    assert summary.active_pointer_present is True
    assert summary.active_decision_id == "dec-1"
    assert summary.active_plan_id == "plan-1"
    assert summary.slot_count == 1


def test_summarize_active_store_missing_pointer(tmp_path: Path) -> None:
    summary = summarize_active_store(_active_db(tmp_path), symbol="000660", market="KR")
    assert summary.active_pointer_present is False
    assert summary.active_decision_id is None
    assert summary.dangling_pointer_count == 0


def test_summarize_active_store_detects_dangling_pointer(tmp_path: Path) -> None:
    # pointer 행은 있으나 가리키는 bundle version이 없는 손상 상태 → dangling으로 검출.
    path = tmp_path / "active_dangling.sqlite3"
    conn = _make_db(path, ACTIVE_SCHEMA)
    conn.execute("INSERT INTO active_decision_pointers VALUES ('KR', '005930', 'ghost')")
    conn.commit()
    conn.close()
    summary = summarize_active_store(path, symbol="005930", market="KR")
    assert summary.active_pointer_present is False
    assert summary.dangling_pointer_count == 1


def test_missing_file_raises_typed_error(tmp_path: Path) -> None:
    with pytest.raises(SqliteInspectionError) as exc:
        inspect_sqlite_file(tmp_path / "nope.sqlite3")
    assert exc.value.reason_code == "sqlite_file_missing"


def test_open_read_only_rejects_writes(tmp_path: Path) -> None:
    path = _ledger_db(tmp_path)
    with open_read_only(path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO fills VALUES ('f2', 'o1')")


def test_read_only_does_not_create_missing_file(tmp_path: Path) -> None:
    target = tmp_path / "absent.sqlite3"
    with pytest.raises(SqliteInspectionError):
        with open_read_only(target):
            pass
    assert not target.exists()
