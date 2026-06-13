from __future__ import annotations

import json as _json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from composition.sqlite_inspector import (
    SqliteInspectionError,
    inspect_active_decision,
    inspect_sqlite_file,
    open_read_only,
    summarize_active_store,
    summarize_journal,
    summarize_ledger,
)
from decision.canonical_json import canonical_json_dumps, payload_sha256

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


# --- active decision integrity (RTM-7c.4b hardening: pointer identity + validity) ---

_FULL_ACTIVE_SCHEMA = """
CREATE TABLE decision_bundle_versions (
    publication_id TEXT PRIMARY KEY, market TEXT, symbol TEXT, decision_id TEXT, plan_id TEXT,
    decision_created_at TEXT, valid_from TEXT, expires_at TEXT, bundle_json TEXT, bundle_hash TEXT
);
CREATE TABLE active_decision_pointers (
    market TEXT, symbol TEXT, publication_id TEXT, PRIMARY KEY (market, symbol)
);
CREATE TABLE decision_refresh_slots (publication_id TEXT);
"""

_CREATED_AT = "2026-06-16T09:00:00+09:00"
_VALID_FROM = "2026-06-16T09:00:00+09:00"
_EXPIRES_AT = "2026-06-17T09:00:00+09:00"


def _decision_payload(*, market: str, symbol: str, decision_id: str, created_at: str = _CREATED_AT,
                      universe: str = "KR_LARGE", action: str = "buy") -> dict:
    return {
        "decision_id": decision_id,
        "created_at": created_at,
        "market": market,
        "symbol": symbol,
        "universe": universe,
        "fund_manager": {"action": action},
    }


def _plan_payload(*, market: str, symbol: str, decision_id: str, plan_id: str) -> dict:
    return {"plan_id": plan_id, "market": market, "symbol": symbol, "decision_id": decision_id}


def _insert_bundle(
    conn: sqlite3.Connection, *, pointer_market: str, pointer_symbol: str,
    version_market: str, version_symbol: str, decision_id: str = "dec-1",
    plan_id: str | None = None, created_at: str = _CREATED_AT,
    valid_from: str = _VALID_FROM, expires_at: str = _EXPIRES_AT,
    decision_payload: dict | None = None, plan_payload: dict | None = None,
) -> None:
    decision = decision_payload if decision_payload is not None else _decision_payload(
        market=version_market, symbol=version_symbol, decision_id=decision_id, created_at=created_at
    )
    bundle = {"decision": decision, "plan": plan_payload, "valid_from": valid_from, "expires_at": expires_at}
    bundle_json = canonical_json_dumps(bundle)
    bundle_hash = payload_sha256(_json.loads(bundle_json))
    pub = payload_sha256(
        {
            "market": version_market,
            "symbol": version_symbol,
            "decision_id": decision_id,
            "decision_created_at": created_at,
            "bundle_hash": bundle_hash,
        }
    )
    conn.execute(
        "INSERT INTO decision_bundle_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pub, version_market, version_symbol, decision_id, plan_id, created_at,
         valid_from, expires_at, bundle_json, bundle_hash),
    )
    conn.execute(
        "INSERT INTO active_decision_pointers VALUES (?,?,?)", (pointer_market, pointer_symbol, pub)
    )


def _active_path(tmp_path: Path, name: str = "active.sqlite3") -> tuple[Path, sqlite3.Connection]:
    path = tmp_path / name
    conn = _make_db(path, _FULL_ACTIVE_SCHEMA)
    return path, conn


def test_active_decision_valid_buy(tmp_path: Path) -> None:
    path, conn = _active_path(tmp_path)
    _insert_bundle(conn, pointer_market="KR", pointer_symbol="005930",
                   version_market="KR", version_symbol="005930", plan_id="plan-1",
                   plan_payload=_plan_payload(market="KR", symbol="005930",
                                              decision_id="dec-1", plan_id="plan-1"))
    conn.commit(); conn.close()
    verdict = inspect_active_decision(path, symbol="005930", market="KR")
    assert verdict.present is True
    assert verdict.integrity_ok is True
    assert verdict.integrity_reason is None
    assert verdict.universe == "KR_LARGE"
    assert verdict.action == "buy"
    assert verdict.has_plan is True


def test_active_decision_missing_pointer(tmp_path: Path) -> None:
    path, conn = _active_path(tmp_path)
    conn.commit(); conn.close()
    verdict = inspect_active_decision(path, symbol="005930", market="KR")
    assert verdict.present is False
    assert verdict.integrity_ok is False


def test_active_decision_dangling_pointer(tmp_path: Path) -> None:
    path, conn = _active_path(tmp_path)
    conn.execute("INSERT INTO active_decision_pointers VALUES ('KR', '005930', 'ghost')")
    conn.commit(); conn.close()
    verdict = inspect_active_decision(path, symbol="005930", market="KR")
    assert verdict.present is True
    assert verdict.integrity_ok is False
    assert verdict.integrity_reason == "dangling"


def test_active_decision_pointer_to_foreign_version_is_identity_mismatch(tmp_path: Path) -> None:
    # Finding A: configured pointer (KR,005930) referencing a (KR,000660) version must fail,
    # even though that version is itself internally consistent.
    path, conn = _active_path(tmp_path)
    _insert_bundle(conn, pointer_market="KR", pointer_symbol="000660",
                   version_market="KR", version_symbol="000660", decision_id="dec-660")
    # Cross-wire: make the (KR,005930) pointer reference the (KR,000660) publication.
    pub = conn.execute(
        "SELECT publication_id FROM active_decision_pointers WHERE symbol='000660'"
    ).fetchone()[0]
    conn.execute("INSERT INTO active_decision_pointers VALUES ('KR','005930',?)", (pub,))
    conn.commit(); conn.close()
    verdict = inspect_active_decision(path, symbol="005930", market="KR")
    assert verdict.present is True
    assert verdict.integrity_ok is False
    assert verdict.integrity_reason == "identity_mismatch"


def test_active_decision_internal_identity_mismatch(tmp_path: Path) -> None:
    # version columns match the queried pointer, but the bundle's internal decision.symbol differs.
    path, conn = _active_path(tmp_path)
    tampered = _decision_payload(market="KR", symbol="999999", decision_id="dec-1")
    _insert_bundle(conn, pointer_market="KR", pointer_symbol="005930",
                   version_market="KR", version_symbol="005930", decision_payload=tampered)
    conn.commit(); conn.close()
    verdict = inspect_active_decision(path, symbol="005930", market="KR")
    assert verdict.integrity_reason == "identity_mismatch"


def test_active_decision_plan_identity_mismatch(tmp_path: Path) -> None:
    # BUY plan present but plan.market disagrees with the version it executes.
    path, conn = _active_path(tmp_path)
    _insert_bundle(conn, pointer_market="KR", pointer_symbol="005930",
                   version_market="KR", version_symbol="005930", plan_id="plan-1",
                   plan_payload=_plan_payload(market="US", symbol="005930",
                                              decision_id="dec-1", plan_id="plan-1"))
    conn.commit(); conn.close()
    verdict = inspect_active_decision(path, symbol="005930", market="KR")
    assert verdict.integrity_reason == "identity_mismatch"


@pytest.mark.parametrize(
    "valid_from, expires_at",
    [
        ("not-a-date", _EXPIRES_AT),                       # malformed valid_from
        (_VALID_FROM, "not-a-date"),                        # malformed expires_at
        ("2026-06-16T09:00:00", _EXPIRES_AT),               # naive valid_from
        (_VALID_FROM, "2026-06-17T09:00:00"),               # naive expires_at
        (_EXPIRES_AT, _VALID_FROM),                          # valid_from > expires_at
    ],
)
def test_active_decision_malformed_validity_is_corrupt(
    tmp_path: Path, valid_from: str, expires_at: str
) -> None:
    # Finding B: validity is integrity, not best-effort. Malformed/naive/reversed → corrupt.
    path, conn = _active_path(tmp_path)
    _insert_bundle(conn, pointer_market="KR", pointer_symbol="005930",
                   version_market="KR", version_symbol="005930",
                   valid_from=valid_from, expires_at=expires_at)
    conn.commit(); conn.close()
    verdict = inspect_active_decision(path, symbol="005930", market="KR")
    assert verdict.integrity_ok is False
    assert verdict.integrity_reason == "corrupt"
