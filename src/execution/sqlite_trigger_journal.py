"""SqliteTriggerJournal — TriggerJournal 의 SQLite 영속 구현.

저장 위치는 ledger DB 와 분리된 별도 파일(runtime/paper/trigger_journal.sqlite3)을 권장한다.
이 모듈은 broker/ledger/network 를 import 하지 않는다(순수 SQLite 영속화).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterator

from analysis.models import AnalysisAction
from domain._datetime import require_timezone_aware_datetime
from domain.enums import Market
from execution.trigger_journal import (
    IdentityCollisionError,
    IllegalTransitionError,
    JournalResultStatus,
    JournalState,
    NonMonotonicTimestampError,
    OrderIdConflictError,
    RecordNotFoundError,
    ReserveOutcome,
    ReserveResult,
    TERMINAL_STATES,
    TriggerFireSignal,
    TriggerJournalError,
    TriggerJournalRecord,
)

# write 경합 시 "database is locked" 대신 이 시간만큼 대기한다(동시 reserve 정책 고정).
_BUSY_TIMEOUT_SECONDS = 5.0

_IDENTITY_FIELDS =("trigger_id", "decision_id", "plan_id", "market", "symbol", "action")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trigger_fire_journal (
    idempotency_key TEXT PRIMARY KEY,
    trigger_id      TEXT NOT NULL,
    decision_id     TEXT NOT NULL,
    plan_id         TEXT NOT NULL,
    market          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,
    state           TEXT NOT NULL CHECK(
        state IN ('reserved', 'dispatching', 'committed', 'aborted', 'uncertain')
    ),
    order_id        TEXT UNIQUE,
    result_status   TEXT,
    reason_code     TEXT,
    triggered_at    TEXT NOT NULL,
    reserved_at     TEXT NOT NULL,
    dispatching_at  TEXT,
    finalized_at    TEXT,
    updated_at      TEXT NOT NULL
);
"""


# --- 내부 직렬화 helper (public API에 노출하지 않음) ---


def _dt_to_str(value: datetime) -> str:
    # 모든 datetime을 UTC로 normalize해서 저장한다. 그래야 TEXT(ISO-8601) 문자열 정렬이
    # 절대시간 순서와 일치한다(서로 다른 offset이 섞여도 list_nonterminal 정렬이 안전).
    return value.astimezone(timezone.utc).isoformat()


def _str_to_dt(value: str | None, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return require_timezone_aware_datetime(parsed, field_name=field_name)


def _coerce_text(value: object, *, field_name: str) -> str:
    """str / StrEnum / .value(str) 보유 식별자를 문자열로 정규화. bool/None 등은 거부."""
    if isinstance(value, bool):
        raise TriggerJournalError(f"{field_name} must not be a bool.")
    if isinstance(value, StrEnum):
        return str(value.value)
    if isinstance(value, str):
        return value
    inner = getattr(value, "value", None)
    if isinstance(inner, str):
        return inner
    raise TriggerJournalError(f"{field_name} must be a string-like value, got {type(value)!r}.")


def _coerce_enum(value: object, enum_cls: type[StrEnum], *, field_name: str) -> str:
    if isinstance(value, bool):
        raise TriggerJournalError(f"{field_name} must not be a bool.")
    if isinstance(value, enum_cls):
        return str(value.value)
    if isinstance(value, str):
        try:
            return str(enum_cls(value).value)
        except ValueError as exc:
            raise TriggerJournalError(f"unknown {field_name}: {value!r}.") from exc
    raise TriggerJournalError(f"{field_name} must be {enum_cls.__name__} or str, got {type(value)!r}.")


def _require_nonblank(value: object, *, field_name: str) -> str:
    text = _coerce_text(value, field_name=field_name)
    if not text.strip():
        raise TriggerJournalError(f"{field_name} must not be blank.")
    return text


class SqliteTriggerJournal:
    """TriggerJournal 의 SQLite 구현."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), timeout=_BUSY_TIMEOUT_SECONDS)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(_BUSY_TIMEOUT_SECONDS * 1000)}")
        # 파일 DB는 WAL로 동시 reader/writer 경합을 줄인다(:memory:는 WAL 미지원이라 무시).
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:  # pragma: no cover - :memory: 등
            pass
        self._depth = 0
        self._init_schema()

    def _init_schema(self) -> None:
        with self.transaction():
            self._conn.execute(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- transaction 경계 ---

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._depth += 1
        try:
            yield
        except Exception:
            self._depth -= 1
            if self._depth == 0:
                self._conn.rollback()
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self._conn.commit()

    # --- reserve ---

    def reserve(self, signal: TriggerFireSignal, now: datetime) -> ReserveResult:
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        fields = self._coerce_signal(signal)
        triggered_at = fields["triggered_at"]
        assert isinstance(triggered_at, datetime)
        if triggered_at > aware_now:
            raise NonMonotonicTimestampError(
                f"triggered_at {triggered_at.isoformat()} is after reserved_at "
                f"{aware_now.isoformat()}."
            )
        now_str = _dt_to_str(aware_now)
        try:
            with self.transaction():
                self._conn.execute(
                    """
                    INSERT INTO trigger_fire_journal (
                        idempotency_key, trigger_id, decision_id, plan_id, market, symbol,
                        action, state, triggered_at, reserved_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fields["idempotency_key"],
                        fields["trigger_id"],
                        fields["decision_id"],
                        fields["plan_id"],
                        fields["market"],
                        fields["symbol"],
                        fields["action"],
                        JournalState.RESERVED.value,
                        _dt_to_str(fields["triggered_at"]),
                        now_str,
                        now_str,
                    ),
                )
        except sqlite3.IntegrityError:
            existing = self.get(fields["idempotency_key"])
            if existing is None:  # pragma: no cover - PK 충돌인데 행이 없을 수 없음
                raise
            self._assert_identity_match(existing, fields)
            outcome = (
                ReserveOutcome.EXISTING_TERMINAL
                if existing.state in TERMINAL_STATES
                else ReserveOutcome.EXISTING_PENDING
            )
            return ReserveResult(outcome=outcome, record=existing)

        return ReserveResult(
            outcome=ReserveOutcome.RESERVED_NEW,
            record=self._require_record(fields["idempotency_key"]),
        )

    # --- 상태 전이 ---

    def mark_dispatching(
        self, idempotency_key: str, order_id: str, now: datetime
    ) -> TriggerJournalRecord:
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        order_id_text = _require_nonblank(order_id, field_name="order_id")
        now_str = _dt_to_str(aware_now)
        try:
            self._guarded_update(
                idempotency_key,
                expected=JournalState.RESERVED,
                now=aware_now,
                sql="""
                    UPDATE trigger_fire_journal
                    SET state = ?, order_id = ?, dispatching_at = ?, updated_at = ?
                    WHERE idempotency_key = ? AND state = ?
                """,
                params=(
                    JournalState.DISPATCHING.value,
                    order_id_text,
                    now_str,
                    now_str,
                    idempotency_key,
                    JournalState.RESERVED.value,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise OrderIdConflictError(f"order_id already in use: {order_id_text!r}.") from exc
        return self._require_record(idempotency_key)

    def mark_committed(
        self, idempotency_key: str, result_status: JournalResultStatus | str, now: datetime
    ) -> TriggerJournalRecord:
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        status_text = _coerce_enum(
            result_status, JournalResultStatus, field_name="result_status"
        )
        now_str = _dt_to_str(aware_now)
        self._guarded_update(
            idempotency_key,
            expected=JournalState.DISPATCHING,
            now=aware_now,
            sql="""
                UPDATE trigger_fire_journal
                SET state = ?, result_status = ?, finalized_at = ?, updated_at = ?
                WHERE idempotency_key = ? AND state = ?
            """,
            params=(
                JournalState.COMMITTED.value,
                status_text,
                now_str,
                now_str,
                idempotency_key,
                JournalState.DISPATCHING.value,
            ),
        )
        return self._require_record(idempotency_key)

    def mark_aborted(
        self, idempotency_key: str, reason_code: str, now: datetime
    ) -> TriggerJournalRecord:
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        reason_text = _require_nonblank(reason_code, field_name="reason_code")
        now_str = _dt_to_str(aware_now)
        self._guarded_update(
            idempotency_key,
            expected=JournalState.RESERVED,
            now=aware_now,
            sql="""
                UPDATE trigger_fire_journal
                SET state = ?, reason_code = ?, finalized_at = ?, updated_at = ?
                WHERE idempotency_key = ? AND state = ?
            """,
            params=(
                JournalState.ABORTED.value,
                reason_text,
                now_str,
                now_str,
                idempotency_key,
                JournalState.RESERVED.value,
            ),
        )
        return self._require_record(idempotency_key)

    def mark_uncertain(
        self, idempotency_key: str, reason_code: str, now: datetime
    ) -> TriggerJournalRecord:
        aware_now = require_timezone_aware_datetime(now, field_name="now")
        reason_text = _require_nonblank(reason_code, field_name="reason_code")
        now_str = _dt_to_str(aware_now)
        self._guarded_update(
            idempotency_key,
            expected=JournalState.DISPATCHING,
            now=aware_now,
            sql="""
                UPDATE trigger_fire_journal
                SET state = ?, reason_code = ?, finalized_at = ?, updated_at = ?
                WHERE idempotency_key = ? AND state = ?
            """,
            params=(
                JournalState.UNCERTAIN.value,
                reason_text,
                now_str,
                now_str,
                idempotency_key,
                JournalState.DISPATCHING.value,
            ),
        )
        return self._require_record(idempotency_key)

    # --- 조회 ---

    def get(self, idempotency_key: str) -> TriggerJournalRecord | None:
        row = self._conn.execute(
            "SELECT * FROM trigger_fire_journal WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_nonterminal(self) -> tuple[TriggerJournalRecord, ...]:
        rows = self._conn.execute(
            """
            SELECT * FROM trigger_fire_journal
            WHERE state IN (?, ?)
            ORDER BY reserved_at ASC, idempotency_key ASC
            """,
            (JournalState.RESERVED.value, JournalState.DISPATCHING.value),
        ).fetchall()
        return tuple(self._row_to_record(row) for row in rows)

    # --- 내부 helper ---

    def _guarded_update(
        self,
        idempotency_key: str,
        *,
        expected: JournalState,
        now: datetime,
        sql: str,
        params: tuple[object, ...],
    ) -> None:
        with self.transaction():
            existing = self.get(idempotency_key)
            if existing is None:
                raise RecordNotFoundError(f"no journal record for {idempotency_key!r}.")
            if existing.state is not expected:
                raise IllegalTransitionError(
                    f"{idempotency_key!r}: expected state {expected.value!r}, "
                    f"found {existing.state.value!r}."
                )
            if now < existing.updated_at:
                raise NonMonotonicTimestampError(
                    f"{idempotency_key!r}: transition time {now.isoformat()} precedes "
                    f"updated_at {existing.updated_at.isoformat()}."
                )
            cursor = self._conn.execute(sql, params)
            if cursor.rowcount != 1:  # pragma: no cover - 선검사 통과 후 race에서만 발생
                raise IllegalTransitionError(
                    f"{idempotency_key!r}: concurrent transition lost the CAS race."
                )

    def _coerce_signal(self, signal: TriggerFireSignal) -> dict[str, object]:
        return {
            "idempotency_key": _require_nonblank(
                signal.idempotency_key, field_name="idempotency_key"
            ),
            "trigger_id": _require_nonblank(signal.trigger_id, field_name="trigger_id"),
            "decision_id": _require_nonblank(signal.decision_id, field_name="decision_id"),
            "plan_id": _require_nonblank(signal.plan_id, field_name="plan_id"),
            "market": _coerce_enum(signal.market, Market, field_name="market"),
            "symbol": _require_nonblank(signal.symbol, field_name="symbol"),
            "action": _coerce_enum(signal.action, AnalysisAction, field_name="action"),
            "triggered_at": require_timezone_aware_datetime(
                signal.triggered_at, field_name="triggered_at"
            ),
        }

    def _assert_identity_match(
        self, existing: TriggerJournalRecord, fields: dict[str, object]
    ) -> None:
        for name in _IDENTITY_FIELDS:
            if getattr(existing, name) != fields[name]:
                raise IdentityCollisionError(
                    f"idempotency_key {existing.idempotency_key!r} identity mismatch on "
                    f"{name!r}: stored {getattr(existing, name)!r} != {fields[name]!r}."
                )

    def _require_record(self, idempotency_key: str) -> TriggerJournalRecord:
        record = self.get(idempotency_key)
        if record is None:  # pragma: no cover - 방금 INSERT/UPDATE 한 행이 없을 수 없음
            raise RecordNotFoundError(f"no journal record for {idempotency_key!r}.")
        return record

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TriggerJournalRecord:
        return TriggerJournalRecord(
            idempotency_key=row["idempotency_key"],
            trigger_id=row["trigger_id"],
            decision_id=row["decision_id"],
            plan_id=row["plan_id"],
            market=row["market"],
            symbol=row["symbol"],
            action=row["action"],
            state=JournalState(row["state"]),
            order_id=row["order_id"],
            result_status=row["result_status"],
            reason_code=row["reason_code"],
            triggered_at=_str_to_dt(row["triggered_at"], field_name="triggered_at"),  # type: ignore[arg-type]
            reserved_at=_str_to_dt(row["reserved_at"], field_name="reserved_at"),  # type: ignore[arg-type]
            dispatching_at=_str_to_dt(row["dispatching_at"], field_name="dispatching_at"),
            finalized_at=_str_to_dt(row["finalized_at"], field_name="finalized_at"),
            updated_at=_str_to_dt(row["updated_at"], field_name="updated_at"),  # type: ignore[arg-type]
        )
