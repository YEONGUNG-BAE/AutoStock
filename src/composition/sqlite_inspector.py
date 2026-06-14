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

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from analysis import AnalysisAction
from decision.canonical_json import payload_sha256
from orchestration.active_decision_store import deserialize_validated_bundle


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
    order_result_count: int
    filled_result_count: int
    rejected_result_count: int
    pending_result_count: int
    cancelled_result_count: int
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
    dangling_pointer_count: int


_TERMINAL_JOURNAL_STATES = frozenset({"committed", "aborted", "uncertain"})
_NONTERMINAL_JOURNAL_STATES = frozenset({"reserved", "dispatching"})


@contextmanager
def open_read_only(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open ``path`` strictly read-only. Raises ``SqliteInspectionError`` (never leaks
    the raw sqlite exception text) if the file is missing or cannot be opened.

    A plain ``mode=ro`` open of a WAL-mode database materializes a ``-shm``/``-wal``
    sidecar even for a reader (SQLite takes a WAL read lock via shared memory), which
    would silently mutate the operator's filesystem. We therefore add ``immutable=1``
    **only when the database is already quiescent** (no live ``-wal``/``-shm``/``-journal``
    sidecar): for a quiescent file ``immutable=1`` reads the main file directly and
    creates no sidecar, and is safe precisely because nothing else holds the file. When a
    sidecar *is* present the DB is non-quiescent — callers fail-closed with
    ``database_not_quiescent`` — so we keep a faithful ``mode=ro`` read there rather than
    blindly ignoring the live WAL with ``immutable=1``."""

    resolved = Path(path)
    if not resolved.exists():
        raise SqliteInspectionError("sqlite_file_missing", f"SQLite file not found: {resolved}")
    if not resolved.is_file():
        raise SqliteInspectionError("sqlite_not_a_file", f"SQLite path is not a regular file: {resolved}")

    quiescent = not sidecar_files(resolved)
    uri = f"file:{resolved}?mode=ro&immutable=1" if quiescent else f"file:{resolved}?mode=ro"
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


def _table_columns(conn: sqlite3.Connection, table: str, *, known_tables: frozenset[str]) -> frozenset[str]:
    if table not in known_tables:
        return frozenset()
    # table은 sqlite_master 화이트리스트 값이므로 식별자 인터폴레이션이 안전하다.
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return frozenset(str(row["name"]) for row in rows)


# Minimal required schema per database for an operator-trustworthy inspection.
# Column names are verified against the live DDL in SQLiteLedger / SqliteTriggerJournal /
# ActiveDecisionStore; a missing table/column means the DB is not the expected store.
LEDGER_REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "order_intents": frozenset({"order_id"}),
    "order_results": frozenset({"order_id", "status"}),
    "fills": frozenset({"order_id"}),
    "current_cash": frozenset({"currency", "account_role", "amount"}),
    "current_positions": frozenset({"symbol", "market", "account_role", "currency", "quantity"}),
}
JOURNAL_REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "trigger_fire_journal": frozenset({"idempotency_key", "state"}),
}
ACTIVE_STORE_REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "decision_bundle_versions": frozenset(
        {
            "publication_id",
            "decision_id",
            "plan_id",
            "market",
            "symbol",
            "decision_created_at",
            "valid_from",
            "expires_at",
            "bundle_json",
            "bundle_hash",
            # The runtime reader (ActiveDecisionStore._row_to_active_bundle) also reads these
            # two columns; a DB missing them would be unreadable at activation time, so an
            # operator-trustworthy inspection must require them too.
            "source_payload_hash",
            "published_at",
        }
    ),
    "active_decision_pointers": frozenset({"market", "symbol", "publication_id"}),
    "decision_refresh_slots": frozenset({"publication_id"}),
}

# SQLite sidecar files that indicate an in-flight (non-quiescent) database: a live
# WAL/shared-memory segment or a rollback journal. Read-only inspection is only
# trustworthy against a quiescent DB, so their presence is surfaced as a reason.
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def sidecar_files(path: str | Path) -> tuple[str, ...]:
    """Return the suffixes of any existing SQLite sidecar files for ``path``.

    A non-empty result means the database is not quiescent (a writer left a live
    WAL/shm/journal); read-only inspection cannot prove a clean snapshot of it."""

    resolved = Path(path)
    return tuple(
        suffix
        for suffix in _SIDECAR_SUFFIXES
        if resolved.with_name(resolved.name + suffix).exists()
    )


_SQLITE_HEADER_MAGIC = b"SQLite format 3\x00"
_SQLITE_USER_VERSION_OFFSET = 60


def _user_version_from_header(data: bytes) -> int | None:
    """Read the SQLite ``user_version`` from the 100-byte file header bytes, side-effect free.

    The user version is a 4-byte big-endian integer at offset 60 of a SQLite database
    header. Returning it from already-read bytes avoids opening a connection (which on a
    WAL-mode database would materialize a ``-shm``/``-wal`` sidecar). Non-SQLite files
    (e.g. the JSON snapshot) return ``None``."""

    if len(data) < _SQLITE_USER_VERSION_OFFSET + 4 or data[:16] != _SQLITE_HEADER_MAGIC:
        return None
    return int.from_bytes(data[_SQLITE_USER_VERSION_OFFSET:_SQLITE_USER_VERSION_OFFSET + 4], "big")


@dataclass(frozen=True)
class ArtifactFingerprint:
    """Side-effect-free fingerprint of one on-disk artifact (snapshot or SQLite DB).

    Captures only what can be read WITHOUT opening a SQLite connection: presence,
    regular-file-ness, byte size, byte SHA-256 of the *main* file, the SQLite
    ``user_version`` (from the header bytes; ``None`` for non-SQLite or absent files), and
    the set of live sidecar suffixes. Two fingerprints taken around a read-only operation
    must be equal — any difference proves the operation mutated operator state."""

    name: str
    present: bool
    is_regular_file: bool
    size: int | None
    sha256: str | None
    user_version: int | None
    sidecar_suffixes: tuple[str, ...]


_FINGERPRINT_CHUNK_BYTES = 1 << 20  # 1 MiB; bounds peak memory independent of artifact size.
_SQLITE_HEADER_BYTES = 100  # full SQLite header; enough for magic + user_version at offset 60.


def _stream_hash(resolved: Path) -> tuple[int, str, bytes]:
    """Stream ``resolved`` in fixed-size chunks to compute (size, SHA-256 hex, header bytes).

    Hashing the whole file is required for a faithful byte fingerprint, but the file is never
    loaded into memory in full — peak memory is bounded by the chunk size regardless of how
    large the ledger/journal/active-store grows. ``size`` is the actual number of bytes read
    (a single sequential pass, so it cannot disagree with what was hashed). The first
    ``_SQLITE_HEADER_BYTES`` bytes are retained separately so ``user_version`` can be parsed
    without opening a SQLite connection (which would materialize a ``-shm``/``-wal`` sidecar)."""

    hasher = hashlib.sha256()
    size = 0
    header = b""
    with open(resolved, "rb") as handle:
        while True:
            chunk = handle.read(_FINGERPRINT_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
            if len(header) < _SQLITE_HEADER_BYTES:
                header += chunk[: _SQLITE_HEADER_BYTES - len(header)]
    return size, hasher.hexdigest(), header


def fingerprint_artifact(path: str | Path, *, name: str, is_sqlite: bool) -> ArtifactFingerprint:
    """Fingerprint ``path`` without opening a SQLite connection (no sidecar materialization).

    Reads the main file's bytes directly for size + SHA-256 and parses ``user_version`` from
    the header bytes when ``is_sqlite``. An absent path yields an all-``None`` fingerprint; a
    present-but-irregular path (directory, socket, fifo) yields ``is_regular_file=False`` with
    no hash. Sidecar suffixes are always probed so a writer that left a live WAL/shm/journal
    shows up in the fingerprint (and thus in any before/after diff)."""

    resolved = Path(path)
    if not resolved.exists():
        return ArtifactFingerprint(
            name=name, present=False, is_regular_file=False, size=None, sha256=None,
            user_version=None, sidecar_suffixes=(),
        )
    sidecars = sidecar_files(resolved)
    if not resolved.is_file():
        return ArtifactFingerprint(
            name=name, present=True, is_regular_file=False, size=None, sha256=None,
            user_version=None, sidecar_suffixes=sidecars,
        )
    size, digest, header = _stream_hash(resolved)
    return ArtifactFingerprint(
        name=name,
        present=True,
        is_regular_file=True,
        size=size,
        sha256=digest,
        user_version=_user_version_from_header(header) if is_sqlite else None,
        sidecar_suffixes=sidecars,
    )


def schema_issues(path: str | Path, required: dict[str, frozenset[str]]) -> tuple[str, ...]:
    """Return sanitized ``missing_table:<t>`` / ``missing_column:<t>.<c>`` codes for any
    required table/column absent from ``path``. Empty tuple ⇒ schema satisfies the contract.
    Raises ``SqliteInspectionError`` (never the raw sqlite text) on open/read failure."""

    issues: list[str] = []
    try:
        with open_read_only(path) as conn:
            known = frozenset(_list_tables(conn))
            for table, columns in required.items():
                if table not in known:
                    issues.append(f"missing_table:{table}")
                    continue
                present = _table_columns(conn, table, known_tables=known)
                for column in sorted(columns - present):
                    issues.append(f"missing_column:{table}.{column}")
    except sqlite3.Error as exc:  # pragma: no cover - sanitized, raw text not surfaced
        raise SqliteInspectionError(
            "sqlite_schema_read_failed", f"Unable to read schema: {type(exc).__name__}"
        ) from exc
    return tuple(issues)


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
        # order_results.status는 domain.OrderStatus (FILLED/PENDING/REJECTED/CANCELLED)를
        # 저장한다. 존재하지 않는 'COMMITTED' 대신 실제 enum별로 집계한다.
        status_counts: dict[str, int] = {}
        if "order_results" in known:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM order_results GROUP BY status"
            ).fetchall()
            status_counts = {str(row["status"]): int(row["n"]) for row in rows}
        return LedgerSummary(
            path=str(Path(path)),
            order_intent_count=_count_rows(conn, "order_intents", known_tables=known),
            fill_count=_count_rows(conn, "fills", known_tables=known),
            order_result_count=sum(status_counts.values()),
            filled_result_count=status_counts.get("FILLED", 0),
            rejected_result_count=status_counts.get("REJECTED", 0),
            pending_result_count=status_counts.get("PENDING", 0),
            cancelled_result_count=status_counts.get("CANCELLED", 0),
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
        dangling_pointer_count = 0
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
            # dangling pointer: pointer 행은 있으나 가리키는 bundle version이 없는 손상 상태.
            # JOIN 결과 없음 ≠ "정상적으로 active 없음"을 구분하기 위해 LEFT JOIN으로 검출한다.
            dangling = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM active_decision_pointers AS p
                LEFT JOIN decision_bundle_versions AS v ON v.publication_id = p.publication_id
                WHERE v.publication_id IS NULL
                """
            ).fetchone()
            dangling_pointer_count = int(dangling["n"]) if dangling is not None else 0
        return ActiveStoreSummary(
            path=str(Path(path)),
            bundle_version_count=bundle_count,
            active_pointer_present=pointer_present,
            active_decision_id=active_decision_id,
            active_plan_id=active_plan_id,
            slot_count=slot_count,
            dangling_pointer_count=dangling_pointer_count,
        )


# Integrity reasons surfaced by ``inspect_active_decision``. Sanitized — never raw
# exception text. ``dangling`` ⇒ pointer references a missing version row;
# ``corrupt`` ⇒ stored bundle JSON / hash / publication-id / validity columns or
# validity/published-at datetimes (missing / unparseable / naive / valid_from >
# expires_at) do not reconcile, or the bundle is not a restorable
# ``DecisionTriggerBundle`` (e.g. an unknown action or an incomplete model);
# ``identity_mismatch`` ⇒ the configured pointer (market, symbol), the version columns,
# the bundle's internal decision identity, or the plan identity disagree with one
# another; ``plan_consistency_mismatch`` ⇒ a *recognized* action carries the wrong plan
# presence (BUY/SELL without a plan, or HOLD with one) — a distinct, actionable operator
# classification that must not be collapsed into the generic ``corrupt`` bucket.
_INTEGRITY_DANGLING = "dangling"
_INTEGRITY_CORRUPT = "corrupt"
_INTEGRITY_IDENTITY_MISMATCH = "identity_mismatch"
_INTEGRITY_PLAN_CONSISTENCY_MISMATCH = "plan_consistency_mismatch"


def _parse_required_aware_datetime(value: object) -> datetime | None:
    """Parse a *required*, timezone-aware ISO-8601 datetime. ``None`` ⇒ invalid.

    Returns ``None`` (never raises, never falls back to a naive value) when the input is
    missing, not a string, unparseable, or naive. Validity is integrity, not best-effort:
    callers treat ``None`` as corruption (fail-closed), never as "currently valid"."""

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


@dataclass(frozen=True)
class ActiveDecisionIntegrity:
    """Read-only integrity verdict for the configured (market, symbol) active pointer.

    Replicates the publish-time integrity invariants of ``ActiveDecisionStore`` WITHOUT
    constructing it (its ``__init__`` creates/migrates schema). All fields are sanitized:
    non-secret identifiers and parsed validity strings only — never the raw bundle JSON."""

    present: bool
    integrity_ok: bool
    integrity_reason: str | None
    decision_id: str | None
    plan_id: str | None
    market: str | None
    symbol: str | None
    universe: str | None
    action: str | None
    valid_from: str | None
    expires_at: str | None
    has_plan: bool


def _absent_active_decision() -> ActiveDecisionIntegrity:
    return ActiveDecisionIntegrity(
        present=False,
        integrity_ok=False,
        integrity_reason=None,
        decision_id=None,
        plan_id=None,
        market=None,
        symbol=None,
        universe=None,
        action=None,
        valid_from=None,
        expires_at=None,
        has_plan=False,
    )


def inspect_active_decision(path: str | Path, *, symbol: str, market: str) -> ActiveDecisionIntegrity:
    """Read-only integrity verdict for the active decision pointer of (market, symbol).

    Opens ``path`` strictly read-only and reconciles the stored bundle exactly as
    ``ActiveDecisionStore._row_to_active_bundle`` would, but returns a sanitized verdict
    instead of raising ``PublicationError``. Never constructs the store, never writes,
    never creates schema. Raises ``SqliteInspectionError`` (sanitized) on sqlite failure."""

    try:
        with open_read_only(path) as conn:
            known = frozenset(_list_tables(conn))
            if "active_decision_pointers" not in known or "decision_bundle_versions" not in known:
                # Schema gating lives in ``schema_issues``; absent tables ⇒ no active decision here.
                return _absent_active_decision()
            pointer = conn.execute(
                "SELECT publication_id FROM active_decision_pointers WHERE market = ? AND symbol = ?",
                (market, symbol),
            ).fetchone()
            if pointer is None:
                return _absent_active_decision()
            publication_id = str(pointer["publication_id"])
            version = conn.execute(
                """
                SELECT publication_id, market, symbol, decision_id, plan_id,
                       decision_created_at, valid_from, expires_at, bundle_json, bundle_hash,
                       published_at
                FROM decision_bundle_versions
                WHERE publication_id = ?
                """,
                (publication_id,),
            ).fetchone()
    except sqlite3.Error as exc:  # pragma: no cover - sanitized, raw text not surfaced
        raise SqliteInspectionError(
            "active_decision_read_failed", f"Unable to read active decision: {type(exc).__name__}"
        ) from exc

    if version is None:
        # Pointer exists but references no version row → dangling/corrupt pointer.
        return ActiveDecisionIntegrity(
            present=True,
            integrity_ok=False,
            integrity_reason=_INTEGRITY_DANGLING,
            decision_id=None,
            plan_id=None,
            market=market,
            symbol=symbol,
            universe=None,
            action=None,
            valid_from=None,
            expires_at=None,
            has_plan=False,
        )

    return _verify_active_version(version, queried_market=market, queried_symbol=symbol)


def _verify_active_version(
    row: sqlite3.Row, *, queried_market: str, queried_symbol: str
) -> ActiveDecisionIntegrity:
    """Reconcile a stored ``decision_bundle_versions`` row read-only; return a verdict.

    ``queried_market`` / ``queried_symbol`` are the *configured* pointer key. The version
    a pointer references MUST belong to that same (market, symbol); a pointer that
    references a foreign version is ``identity_mismatch`` even if that version is itself
    internally consistent."""

    col_market = str(row["market"])
    col_symbol = str(row["symbol"])
    col_decision_id = str(row["decision_id"])
    col_plan_id = None if row["plan_id"] is None else str(row["plan_id"])
    col_valid_from = str(row["valid_from"])
    col_expires_at = str(row["expires_at"])
    col_decision_created_at = str(row["decision_created_at"])
    col_published_at = None if row["published_at"] is None else str(row["published_at"])

    def _corrupt(reason: str = _INTEGRITY_CORRUPT) -> ActiveDecisionIntegrity:
        return ActiveDecisionIntegrity(
            present=True,
            integrity_ok=False,
            integrity_reason=reason,
            decision_id=col_decision_id,
            plan_id=col_plan_id,
            market=col_market,
            symbol=col_symbol,
            universe=None,
            action=None,
            valid_from=col_valid_from,
            expires_at=col_expires_at,
            has_plan=col_plan_id is not None,
        )

    # 0) configured pointer identity: the referenced version must be for this (market, symbol).
    if col_market != queried_market or col_symbol != queried_symbol:
        return _corrupt(_INTEGRITY_IDENTITY_MISMATCH)

    # 1) bundle_json must parse to a JSON object and hash to the stored bundle_hash.
    try:
        payload = json.loads(row["bundle_json"])
    except Exception:  # noqa: BLE001 - fail-closed, sanitized
        return _corrupt()
    if not isinstance(payload, dict):
        return _corrupt()
    try:
        recomputed_hash = payload_sha256(payload)
    except Exception:  # noqa: BLE001 - fail-closed, sanitized
        return _corrupt()
    if recomputed_hash != str(row["bundle_hash"]):
        return _corrupt()

    # 2) publication_id must recompute from identity + hash.
    recomputed_pub_id = payload_sha256(
        {
            "market": col_market,
            "symbol": col_symbol,
            "decision_id": col_decision_id,
            "decision_created_at": col_decision_created_at,
            "bundle_hash": str(row["bundle_hash"]),
        }
    )
    if recomputed_pub_id != str(row["publication_id"]):
        return _corrupt()

    # 3) stored payload top-level validity must match the columns AND be well-formed:
    #    present, ISO-parseable, timezone-aware, with valid_from <= expires_at.
    if payload.get("valid_from") != col_valid_from or payload.get("expires_at") != col_expires_at:
        return _corrupt()
    valid_from_dt = _parse_required_aware_datetime(col_valid_from)
    expires_at_dt = _parse_required_aware_datetime(col_expires_at)
    if valid_from_dt is None or expires_at_dt is None or valid_from_dt > expires_at_dt:
        return _corrupt()

    # 3b) published_at column must itself be a present, ISO-parseable, timezone-aware
    #     datetime. The runtime reader (ActiveDecisionStore._row_to_active_bundle) parses
    #     this column via require_timezone_aware_datetime; a malformed/naive value would make
    #     the row unreadable at activation time. published_at is NOT part of the hashed
    #     bundle payload (it is set to ``now`` at publish), so the model-restoration gate
    #     below cannot cover it — it must be validated here for runtime-reader parity.
    if _parse_required_aware_datetime(col_published_at) is None:
        return _corrupt()

    decision = payload.get("decision")
    plan = payload.get("plan")
    if not isinstance(decision, dict):
        return _corrupt()

    # 4) internal identity must match the stored columns.
    plan_id_internal: str | None = None
    if plan is not None:
        if not isinstance(plan, dict):
            return _corrupt()
        plan_id_value = plan.get("plan_id")
        plan_id_internal = None if plan_id_value is None else str(plan_id_value)
    decision_id_obj = decision.get("decision_id")
    decision_id_internal = decision_id_obj if isinstance(decision_id_obj, str) else None
    if (
        decision.get("market") != col_market
        or decision.get("symbol") != col_symbol
        or decision_id_internal != col_decision_id
        or plan_id_internal != col_plan_id
        or decision.get("created_at") != col_decision_created_at
    ):
        return _corrupt(_INTEGRITY_IDENTITY_MISMATCH)

    # 4b) a present plan must agree with the version/decision identity it executes.
    if isinstance(plan, dict):
        if (
            plan.get("market") != col_market
            or plan.get("symbol") != col_symbol
            or plan.get("decision_id") != decision_id_internal
        ):
            return _corrupt(_INTEGRITY_IDENTITY_MISMATCH)

    # 4c) plan-consistency is a *stable, distinct* operator classification, so check it
    #     explicitly BEFORE the full model restoration below. DecisionTriggerBundle validates
    #     the same BUY/SELL-needs-plan / HOLD-forbids-plan invariant, but the shared helper
    #     would collapse it into the generic ``corrupt`` reason — losing the actionable signal
    #     that distinguishes a wrong plan presence from a genuinely unreadable bundle. Only a
    #     *recognized* action with the wrong plan presence is a plan-consistency mismatch; an
    #     unknown/malformed action falls through to model restoration (rejected as corrupt).
    fund_manager = decision.get("fund_manager")
    action = fund_manager.get("action") if isinstance(fund_manager, dict) else None
    has_plan = plan is not None
    if action in (AnalysisAction.BUY.value, AnalysisAction.SELL.value):
        if not has_plan:
            return _corrupt(_INTEGRITY_PLAN_CONSISTENCY_MISMATCH)
    elif action == AnalysisAction.HOLD.value:
        if has_plan:
            return _corrupt(_INTEGRITY_PLAN_CONSISTENCY_MISMATCH)

    # 5) full model-restoration parity with the runtime reader
    #    (ActiveDecisionStore._row_to_active_bundle, via the shared pure helper). Run last so
    #    the more specific identity/validity/plan-consistency reasons above win when both
    #    apply; a payload that clears every check above but is not a valid
    #    DecisionTriggerBundle (incomplete model, unknown action, plan/decision time binding,
    #    etc.) is rejected at activation-read time, so inspect must fail-closed here too —
    #    never report integrity_ok for a bundle the runtime reader cannot load.
    try:
        deserialize_validated_bundle(payload)
    except Exception:  # noqa: BLE001 - fail-closed, sanitized (no model/exception text surfaced)
        return _corrupt()

    universe = decision.get("universe")
    return ActiveDecisionIntegrity(
        present=True,
        integrity_ok=True,
        integrity_reason=None,
        decision_id=col_decision_id,
        plan_id=col_plan_id,
        market=col_market,
        symbol=col_symbol,
        universe=str(universe) if isinstance(universe, str) else None,
        action=str(action) if isinstance(action, str) else None,
        valid_from=col_valid_from,
        expires_at=col_expires_at,
        has_plan=col_plan_id is not None,
    )
