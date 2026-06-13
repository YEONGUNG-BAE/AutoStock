"""Read-only SQLite inspection for offline paper fast-loop operator tooling.

Every connection is opened with ``mode=ro`` (URI) and immediately sets
``PRAGMA query_only = ON`` so a programming error cannot mutate operator state.
This module NEVER:

* constructs ``SQLiteLedger`` / ``SqliteTriggerJournal`` / ``ActiveDecisionStore``
  (their constructors create or migrate schema),
* writes rows, creates tables, runs migrations, changes ``user_version``, or
  reconciles state,
* returns raw payloads, credentials, exception reprs, or tracebacks.

Results are sanitized counts and a small set of non-secret identifiers
(decision_id / plan_id) plus integer-quantity strings. None of the inspected
databases store credentials.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class SqliteInspectionError(Exception):
    """Read-only inspection failure with a typed, sanitized reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class TableRowCount:
    table: str
    row_count: int


@dataclass(frozen=True)
class SqliteFileInspection:
    path: str
    user_version: int
    tables: tuple[str, ...]
    row_counts: tuple[TableRowCount, ...]


@dataclass(frozen=True)
class LedgerSummary:
    path: str
    order_intent_count: int
    fill_count: int
    committed_result_count: int
    position_quantity: str | None
    cash_entry_count: int


@dataclass(frozen=True)
class PositionRow:
    symbol: str
    market: str
    account_role: str
    currency: str
    quantity: str


@dataclass(frozen=True)
class JournalStateCount:
    state: str
    count: int


@dataclass(frozen=True)
class JournalSummary:
    path: str
    total_rows: int
    state_counts: tuple[JournalStateCount, ...]
    terminal_count: int
    nonterminal_count: int


@dataclass(frozen=True)
class ActiveStoreSummary:
    path: str
    bundle_version_count: int
    active_pointer_present: bool
    active_decision_id: str | None
    active_plan_id: str | None
    slot_count: int


_TERMINAL_JOURNAL_STATES = frozenset({"committed", "aborted", "uncertain"})
_NONTERMINAL_JOURNAL_STATES = frozenset({"reserved", "dispatching"})


@contextmanager
def open_read_only(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open ``path`` strictly read-only. Raises ``SqliteInspectionError`` (never leaks
    the raw sqlite exception text) if the file is missing or cannot be opened."""

    resolved = Path(path)
    if not resolved.exists():
        raise SqliteInspectionError("sqlite_file_missing", f"SQLite file not found: {resolved}")
    if not resolved.is_file():
        raise SqliteInspectionError("sqlite_not_a_file", f"SQLite path is not a regular file: {resolved}")

    uri = f"file:{resolved}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - sanitized, exception text not surfaced
        raise SqliteInspectionError(
            "sqlite_open_failed", f"Unable to open SQLite file read-only: {resolved} ({type(exc).__name__})"
        ) from exc

    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def _list_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return tuple(row["name"] for row in rows)


def _read_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def _count_rows(conn: sqlite3.Connection, table: str, *, known_tables: frozenset[str]) -> int:
    if table not in known_tables:
        return 0
    # table는 sqlite_master에서 읽은 화이트리스트 값이므로 식별자 인터폴레이션이 안전하다.
    row = conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()
    return int(row["n"]) if row is not None else 0


def inspect_sqlite_file(path: str | Path) -> SqliteFileInspection:
    """Generic read-only structural inspection: user_version, table names, per-table counts."""

    with open_read_only(path) as conn:
        tables = _list_tables(conn)
        known = frozenset(tables)
        counts = tuple(
            TableRowCount(table=name, row_count=_count_rows(conn, name, known_tables=known)) for name in tables
        )
        return SqliteFileInspection(
            path=str(Path(path)),
            user_version=_read_user_version(conn),
            tables=tables,
            row_counts=counts,
        )


def summarize_ledger(path: str | Path, *, symbol: str, market: str) -> LedgerSummary:
    """Sanitized ledger summary for the configured single symbol/market."""

    with open_read_only(path) as conn:
        known = frozenset(_list_tables(conn))
        position_quantity: str | None = None
        if "current_positions" in known:
            row = conn.execute(
                "SELECT quantity FROM current_positions WHERE symbol = ? AND market = ?",
                (symbol, market),
            ).fetchone()
            if row is not None:
                position_quantity = str(row["quantity"])
        committed = 0
        if "order_results" in known:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM order_results WHERE status = 'COMMITTED'"
            ).fetchone()
            committed = int(row["n"]) if row is not None else 0
        return LedgerSummary(
            path=str(Path(path)),
            order_intent_count=_count_rows(conn, "order_intents", known_tables=known),
            fill_count=_count_rows(conn, "fills", known_tables=known),
            committed_result_count=committed,
            position_quantity=position_quantity,
            cash_entry_count=_count_rows(conn, "current_cash", known_tables=known),
        )


def scan_positions(path: str | Path) -> tuple[PositionRow, ...]:
    """Read-only scan of all non-zero-keyed ``current_positions`` rows for preflight.

    Returns an empty tuple if the table is absent (e.g. ledger never initialised)."""

    with open_read_only(path) as conn:
        if "current_positions" not in frozenset(_list_tables(conn)):
            return ()
        rows = conn.execute(
            "SELECT symbol, market, account_role, currency, quantity FROM current_positions"
        ).fetchall()
        return tuple(
            PositionRow(
                symbol=str(row["symbol"]),
                market=str(row["market"]),
                account_role=str(row["account_role"]),
                currency=str(row["currency"]),
                quantity=str(row["quantity"]),
            )
            for row in rows
        )


def summarize_journal(path: str | Path) -> JournalSummary:
    """Sanitized trigger-journal summary grouped by state."""

    with open_read_only(path) as conn:
        known = frozenset(_list_tables(conn))
        if "trigger_fire_journal" not in known:
            return JournalSummary(
                path=str(Path(path)), total_rows=0, state_counts=(), terminal_count=0, nonterminal_count=0
            )
        rows = conn.execute(
            "SELECT state, COUNT(*) AS n FROM trigger_fire_journal GROUP BY state ORDER BY state"
        ).fetchall()
        state_counts = tuple(JournalStateCount(state=row["state"], count=int(row["n"])) for row in rows)
        total = sum(item.count for item in state_counts)
        terminal = sum(item.count for item in state_counts if item.state in _TERMINAL_JOURNAL_STATES)
        nonterminal = sum(item.count for item in state_counts if item.state in _NONTERMINAL_JOURNAL_STATES)
        return JournalSummary(
            path=str(Path(path)),
            total_rows=total,
            state_counts=state_counts,
            terminal_count=terminal,
            nonterminal_count=nonterminal,
        )


def summarize_active_store(path: str | Path, *, symbol: str, market: str) -> ActiveStoreSummary:
    """Sanitized active-decision-store summary for the configured single symbol/market."""

    with open_read_only(path) as conn:
        known = frozenset(_list_tables(conn))
        bundle_count = _count_rows(conn, "decision_bundle_versions", known_tables=known)
        slot_count = _count_rows(conn, "decision_refresh_slots", known_tables=known)
        active_decision_id: str | None = None
        active_plan_id: str | None = None
        pointer_present = False
        if "active_decision_pointers" in known and "decision_bundle_versions" in known:
            row = conn.execute(
                """
                SELECT v.decision_id AS decision_id, v.plan_id AS plan_id
                FROM active_decision_pointers AS p
                JOIN decision_bundle_versions AS v ON v.publication_id = p.publication_id
                WHERE p.market = ? AND p.symbol = ?
                """,
                (market, symbol),
            ).fetchone()
            if row is not None:
                pointer_present = True
                active_decision_id = str(row["decision_id"])
                active_plan_id = None if row["plan_id"] is None else str(row["plan_id"])
        return ActiveStoreSummary(
            path=str(Path(path)),
            bundle_version_count=bundle_count,
            active_pointer_present=pointer_present,
            active_decision_id=active_decision_id,
            active_plan_id=active_plan_id,
            slot_count=slot_count,
        )
