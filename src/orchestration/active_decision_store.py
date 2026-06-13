"""RTM-7c.1 — atomic decision bundle publication store (offline).

검증된 `DecisionSnapshot` + (BUY/SELL이면) 별도 구조화 입력 `TriggerPlan`을 받아
`load_decision_bundle`로 한 번 더 검증한 뒤, version INSERT와 pointer UPDATE를 단일
`BEGIN IMMEDIATE` SQLite 트랜잭션으로 원자적으로 게시한다. reader는 pointer가 가리키는
**완성된** bundle만 보거나(old/new) 아무것도 보지 못한다 — 절대 부분 상태를 보지 않는다.

여러 프로세스/connection이 같은 파일을 동시에 쓸 수 있으므로(slow-loop 두 개 등) 게시
전체(현재 pointer 읽기→conflict/older 검사→version insert→pointer update)를 단일
`BEGIN IMMEDIATE` write 트랜잭션으로 감싸 cross-connection write race에서도 더 오래된
결정이 최신 결정을 뒤덮지 못하게 한다. `busy_timeout`/WAL을 명시한다.

durable slot journal(`decision_refresh_slots`)을 함께 보관해 스케줄러가 프로세스 재시작
후에도 "하루 slot 1회"를 보장하고, 미완(reserved) slot을 재시작 시 자동 재실행하지 않고
fail-closed로 reconcile하게 한다.

network/broker/ledger/paper_execution/LLM 접근이 없다. 게시 정책은 fail-closed다:
  - schema/검증 실패·plan 누락·decision↔plan 불일치·validity 불일치 → REJECTED_INVALID_BUNDLE
  - 동일 identity·동일 content → IDEMPOTENT (재게시 no-op)
  - 동일 identity·다른 content → REJECTED_CONFLICT (silent 삼킴 금지)
  - 더 오래된 결정으로 최신 결정 교체 → REJECTED_OLDER
  - 게시 시점에 이미 만료 → REJECTED_EXPIRED
  - pointer가 존재하지 않는 version을 가리킴(손상) → PublicationError (fail-closed)
  - reader 역직렬화/무결성(해시·identity) 실패 → PublicationError (fallback 없음)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from analysis.models import AnalysisDecision
from decision.canonical_json import canonical_json_dumps, payload_sha256
from domain._datetime import require_timezone_aware_datetime
from domain.decision import DecisionSnapshot
from domain.enums import Market
from market_data.decision_loader import DecisionLoadError, load_decision_bundle
from market_data.trigger_engine import DecisionTriggerBundle, TriggerPlan

__all__ = [
    "ActiveBundle",
    "ActiveDecisionStore",
    "DecisionPublicationCandidate",
    "PublicationError",
    "PublicationResult",
    "PublicationStatus",
    "SlotReservation",
    "SlotReservationStatus",
    "SlotState",
]


class PublicationError(Exception):
    """게시/판독 중 fail-closed로 표면화하는 오류(손상된 pointer, 역직렬화/무결성 실패 등)."""


class PublicationStatus(StrEnum):
    """게시 시도 결과. 정상 거부는 예외가 아니라 typed 상태로 반환한다."""

    PUBLISHED = "published"
    IDEMPOTENT = "idempotent"
    REJECTED_INVALID_BUNDLE = "rejected_invalid_bundle"
    REJECTED_CONFLICT = "rejected_conflict"
    REJECTED_OLDER = "rejected_older"
    REJECTED_EXPIRED = "rejected_expired"


class SlotState(StrEnum):
    """durable slot journal의 slot 상태."""

    RESERVED = "reserved"
    PUBLISHED = "published"
    MISSED = "missed"
    MISSED_SESSION_CLOSED = "missed_session_closed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


_TERMINAL_SLOT_STATES = frozenset(
    {
        SlotState.PUBLISHED,
        SlotState.MISSED,
        SlotState.MISSED_SESSION_CLOSED,
        SlotState.FAILED,
        SlotState.UNCERTAIN,
    }
)


class SlotReservationStatus(StrEnum):
    RESERVED = "reserved"  # 이번 호출에서 새로 예약됨 → 실행 진행
    ALREADY_TERMINAL = "already_terminal"  # 이미 종료된 slot(소비됨) → 재실행 금지
    DANGLING_RESERVED = "dangling_reserved"  # 직전 reserved 잔존(크래시) → reconcile 필요


@dataclass(frozen=True)
class SlotReservation:
    status: SlotReservationStatus
    existing_state: SlotState | None = None


@dataclass(frozen=True)
class DecisionPublicationCandidate:
    """게시 후보. `TriggerPlan`은 prose에서 추론하지 않고 반드시 별도 구조화 입력으로 받는다.

    HOLD면 plan은 None이며, BUY/SELL이면 plan이 필수다(검증은 load_decision_bundle이 수행).
    valid_from/expires_at은 활성 bundle의 유효 구간이다. BUY/SELL이면 plan의 valid_from/
    expires_at과 정확히 일치해야 한다(이중 validity 불일치 방지). 모든 경우
    decision.created_at <= valid_from <= expires_at를 만족해야 한다.
    """

    snapshot: DecisionSnapshot
    plan: TriggerPlan | None
    valid_from: datetime
    expires_at: datetime


@dataclass(frozen=True)
class PublicationResult:
    status: PublicationStatus
    publication_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ActiveBundle:
    """현재 pointer가 가리키는 완성된 활성 bundle(판독 결과)."""

    publication_id: str
    market: str
    symbol: str
    decision_id: str
    plan_id: str | None
    decision_created_at: datetime
    valid_from: datetime
    expires_at: datetime
    bundle: DecisionTriggerBundle
    bundle_hash: str
    source_payload_hash: str
    published_at: datetime


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decision_bundle_versions (
    publication_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    plan_id TEXT,
    decision_created_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    bundle_json TEXT NOT NULL,
    bundle_hash TEXT NOT NULL,
    source_payload_hash TEXT NOT NULL,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_decision_pointers (
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    publication_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (market, symbol)
);
CREATE TABLE IF NOT EXISTS decision_refresh_slots (
    market TEXT NOT NULL,
    session_date TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    state TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    publication_id TEXT,
    PRIMARY KEY (market, session_date, slot_id)
);
"""


@dataclass(frozen=True)
class _PointerView:
    publication_id: str
    decision_id: str
    decision_created_at: datetime
    bundle_hash: str


class ActiveDecisionStore:
    """원자적 decision bundle 게시 저장소. version 이력은 append-only, pointer만 교체한다.

    autocommit(isolation_level=None) connection을 쓰고, 모든 변경 트랜잭션은 명시적
    `BEGIN IMMEDIATE`로 시작해 cross-connection write race에서도 직렬화한다. in-process
    동시성은 추가로 Lock으로 보호한다(같은 connection의 cursor 재진입 방지).
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        _fault_after_version_insert: Callable[[], None] | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._db_path = str(db_path)
        # isolation_level=None: 파이썬 sqlite3의 암묵 트랜잭션을 끄고 BEGIN IMMEDIATE를
        # 직접 발급한다. check_same_thread=False: 동시 writer 테스트(스레드)에서 같은
        # connection을 Lock으로 보호하며 공유한다.
        self._conn = sqlite3.connect(
            self._db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        if self._db_path != ":memory:":
            # WAL: writer가 reader를 막지 않게(파일 기반에서만 의미; :memory:는 미적용).
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.Lock()
        self._fault_after_version_insert = _fault_after_version_insert
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def list_tables(self) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        return tuple(row["name"] for row in rows)

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        """`BEGIN IMMEDIATE`로 write 락을 즉시 획득하는 트랜잭션. 실패 시 ROLLBACK."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # --- publish ---------------------------------------------------------------

    def publish(
        self, candidate: DecisionPublicationCandidate, *, now: datetime
    ) -> PublicationResult:
        """검증된 후보를 원자적으로 게시한다. 정상 거부는 typed 상태로 반환한다(fail-closed)."""
        require_timezone_aware_datetime(now, field_name="now")
        valid_from = require_timezone_aware_datetime(
            candidate.valid_from, field_name="valid_from"
        )
        expires_at = require_timezone_aware_datetime(
            candidate.expires_at, field_name="expires_at"
        )
        if valid_from > expires_at:
            return PublicationResult(
                PublicationStatus.REJECTED_INVALID_BUNDLE,
                reason="valid_from must be <= expires_at",
            )

        # 1) bundle 검증: schema/검증/plan 일치 실패는 모두 typed 거부로 표면화.
        try:
            bundle = load_decision_bundle(candidate.snapshot, candidate.plan)
        except DecisionLoadError as exc:
            return PublicationResult(
                PublicationStatus.REJECTED_INVALID_BUNDLE, reason=str(exc)
            )

        decision = bundle.decision
        decision_created_at = decision.created_at

        # 2) validity binding: 이중 validity 불일치 방지.
        if decision_created_at > valid_from:
            return PublicationResult(
                PublicationStatus.REJECTED_INVALID_BUNDLE,
                reason="decision.created_at must be <= valid_from",
            )
        if bundle.plan is not None:
            # BUY/SELL: candidate validity는 plan validity와 정확히 일치해야 한다.
            if valid_from != bundle.plan.valid_from or expires_at != bundle.plan.expires_at:
                return PublicationResult(
                    PublicationStatus.REJECTED_INVALID_BUNDLE,
                    reason="candidate validity must equal plan validity for BUY/SELL",
                )

        market = decision.market
        symbol = decision.symbol
        decision_id = decision.decision_id.value
        plan_id = bundle.plan.plan_id if bundle.plan is not None else None
        bundle_json = _bundle_to_json(bundle, valid_from=valid_from, expires_at=expires_at)
        bundle_hash = payload_sha256(json.loads(bundle_json))
        source_payload_hash = candidate.snapshot.payload_hash
        publication_id = _publication_id(
            market=market,
            symbol=symbol,
            decision_id=decision_id,
            decision_created_at=decision_created_at,
            bundle_hash=bundle_hash,
        )

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._read_pointer_view(market, symbol)
                result = self._publish_decision(
                    current=current,
                    decision_id=decision_id,
                    decision_created_at=decision_created_at,
                    bundle_hash=bundle_hash,
                    expires_at=expires_at,
                    now=now,
                    publication_id=publication_id,
                    market=market,
                    symbol=symbol,
                    plan_id=plan_id,
                    valid_from=valid_from,
                    bundle_json=bundle_json,
                    source_payload_hash=source_payload_hash,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return result

    def _publish_decision(
        self,
        *,
        current: _PointerView | None,
        decision_id: str,
        decision_created_at: datetime,
        bundle_hash: str,
        expires_at: datetime,
        now: datetime,
        publication_id: str,
        market: str,
        symbol: str,
        plan_id: str | None,
        valid_from: datetime,
        bundle_json: str,
        source_payload_hash: str,
    ) -> PublicationResult:
        if current is not None:
            same_id = current.decision_id == decision_id
            same_time = current.decision_created_at == decision_created_at
            if same_id and same_time:
                if current.bundle_hash == bundle_hash:
                    return PublicationResult(
                        PublicationStatus.IDEMPOTENT, current.publication_id
                    )
                return PublicationResult(
                    PublicationStatus.REJECTED_CONFLICT,
                    current.publication_id,
                    reason="same identity, different content",
                )
            if same_time and not same_id:
                # 동일 시각의 서로 다른 결정 — 순서를 정할 수 없어 fail-closed 거부.
                return PublicationResult(
                    PublicationStatus.REJECTED_CONFLICT,
                    current.publication_id,
                    reason="same created_at, different decision_id",
                )
            if decision_created_at < current.decision_created_at:
                return PublicationResult(
                    PublicationStatus.REJECTED_OLDER, current.publication_id
                )

        # 만료 판정은 식별/순서 판정 이후, 실제 INSERT 직전에 수행한다.
        if expires_at <= now:
            return PublicationResult(
                PublicationStatus.REJECTED_EXPIRED,
                reason="candidate already expired at publish time",
            )

        self._conn.execute(
            """
            INSERT INTO decision_bundle_versions (
                publication_id, market, symbol, decision_id, plan_id,
                decision_created_at, valid_from, expires_at,
                bundle_json, bundle_hash, source_payload_hash, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                publication_id,
                market,
                symbol,
                decision_id,
                plan_id,
                decision_created_at.isoformat(),
                valid_from.isoformat(),
                expires_at.isoformat(),
                bundle_json,
                bundle_hash,
                source_payload_hash,
                now.isoformat(),
            ),
        )
        # 테스트 전용 fault hook: version INSERT 후 pointer UPDATE 전에 끼어들어
        # 트랜잭션 원자성(pointer 불변)을 검증한다.
        if self._fault_after_version_insert is not None:
            self._fault_after_version_insert()
        self._conn.execute(
            """
            INSERT INTO active_decision_pointers (market, symbol, publication_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(market, symbol) DO UPDATE SET
                publication_id = excluded.publication_id,
                updated_at = excluded.updated_at
            """,
            (market, symbol, publication_id, now.isoformat()),
        )
        return PublicationResult(PublicationStatus.PUBLISHED, publication_id)

    # --- read ------------------------------------------------------------------

    def read_active(self, market: Market | str, symbol: str) -> ActiveBundle | None:
        """현재 pointer가 가리키는 완성된 활성 bundle을 반환한다.

        pointer가 없으면 None. pointer가 존재하지 않는 version을 가리키면(손상) 또는
        bundle_json 역직렬화/무결성(해시·identity) 검증이 실패하면 PublicationError로
        fail-closed한다(fallback 없음)."""
        market_value = _market_value(market)
        with self._lock:
            pointer = self._conn.execute(
                "SELECT publication_id FROM active_decision_pointers "
                "WHERE market = ? AND symbol = ?",
                (market_value, symbol),
            ).fetchone()
            if pointer is None:
                return None
            row = self._conn.execute(
                "SELECT * FROM decision_bundle_versions WHERE publication_id = ?",
                (pointer["publication_id"],),
            ).fetchone()
        if row is None:
            raise PublicationError(
                "active pointer references a missing bundle version (corrupt pointer)."
            )
        return _row_to_active_bundle(row)

    def list_history(self, market: Market | str, symbol: str) -> tuple[ActiveBundle, ...]:
        """해당 (market, symbol)의 게시 이력(append-only)을 published_at 순으로 반환한다."""
        market_value = _market_value(market)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decision_bundle_versions WHERE market = ? AND symbol = ? "
                "ORDER BY published_at ASC, publication_id ASC",
                (market_value, symbol),
            ).fetchall()
        return tuple(_row_to_active_bundle(row) for row in rows)

    # --- durable slot journal --------------------------------------------------

    def reserve_slot(
        self,
        *,
        market: Market | str,
        session_date: date,
        slot_id: str,
        scheduled_at: datetime,
        now: datetime,
    ) -> SlotReservation:
        """slot을 durable하게 예약한다(runner 호출 전 1회). 결과로 실행 가능 여부를 알린다.

        - 미존재 → RESERVED (실행 진행)
        - 종료 상태(published/missed/failed/...) 존재 → ALREADY_TERMINAL (재실행 금지)
        - reserved 잔존(직전 크래시) → DANGLING_RESERVED (자동 재실행 금지, reconcile 필요)
        """
        market_value = _market_value(market)
        sd = session_date.isoformat()
        with self._lock:
            with self._immediate():
                row = self._conn.execute(
                    "SELECT state FROM decision_refresh_slots "
                    "WHERE market = ? AND session_date = ? AND slot_id = ?",
                    (market_value, sd, slot_id),
                ).fetchone()
                if row is not None:
                    state = SlotState(row["state"])
                    if state is SlotState.RESERVED:
                        return SlotReservation(
                            SlotReservationStatus.DANGLING_RESERVED, state
                        )
                    return SlotReservation(
                        SlotReservationStatus.ALREADY_TERMINAL, state
                    )
                self._conn.execute(
                    """
                    INSERT INTO decision_refresh_slots (
                        market, session_date, slot_id, scheduled_at, state, reserved_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        market_value,
                        sd,
                        slot_id,
                        scheduled_at.isoformat(),
                        SlotState.RESERVED.value,
                        now.isoformat(),
                    ),
                )
        return SlotReservation(SlotReservationStatus.RESERVED)

    def finalize_slot(
        self,
        *,
        market: Market | str,
        session_date: date,
        slot_id: str,
        scheduled_at: datetime,
        state: SlotState,
        now: datetime,
        outcome: str | None = None,
        publication_id: str | None = None,
    ) -> None:
        """slot을 종료 상태로 기록한다(durable). 미존재면 종료 상태로 직접 INSERT한다
        (missed/missed_session_closed는 예약 없이 바로 종료 기록될 수 있다)."""
        if state not in _TERMINAL_SLOT_STATES:
            raise PublicationError(f"finalize_slot state must be terminal, got {state!r}.")
        market_value = _market_value(market)
        sd = session_date.isoformat()
        with self._lock:
            with self._immediate():
                self._conn.execute(
                    """
                    INSERT INTO decision_refresh_slots (
                        market, session_date, slot_id, scheduled_at, state,
                        reserved_at, finished_at, outcome, publication_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market, session_date, slot_id) DO UPDATE SET
                        state = excluded.state,
                        finished_at = excluded.finished_at,
                        outcome = excluded.outcome,
                        publication_id = excluded.publication_id
                    """,
                    (
                        market_value,
                        sd,
                        slot_id,
                        scheduled_at.isoformat(),
                        state.value,
                        now.isoformat(),
                        now.isoformat(),
                        outcome,
                        publication_id,
                    ),
                )

    def slot_states(
        self, market: Market | str, session_date: date
    ) -> dict[str, SlotState]:
        """해당 (market, session_date)의 slot_id→state 맵을 반환한다."""
        market_value = _market_value(market)
        sd = session_date.isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT slot_id, state FROM decision_refresh_slots "
                "WHERE market = ? AND session_date = ?",
                (market_value, sd),
            ).fetchall()
        return {row["slot_id"]: SlotState(row["state"]) for row in rows}

    # --- internals -------------------------------------------------------------

    def _read_pointer_view(self, market: str, symbol: str) -> _PointerView | None:
        row = self._conn.execute(
            """
            SELECT v.publication_id AS publication_id,
                   v.decision_id AS decision_id,
                   v.decision_created_at AS decision_created_at,
                   v.bundle_hash AS bundle_hash
            FROM active_decision_pointers p
            JOIN decision_bundle_versions v ON v.publication_id = p.publication_id
            WHERE p.market = ? AND p.symbol = ?
            """,
            (market, symbol),
        ).fetchone()
        if row is None:
            # pointer 없음(정상) 또는 pointer가 손상되어 version과 join 실패. 둘을 구분한다.
            pointer = self._conn.execute(
                "SELECT publication_id FROM active_decision_pointers "
                "WHERE market = ? AND symbol = ?",
                (market, symbol),
            ).fetchone()
            if pointer is not None:
                raise PublicationError(
                    "active pointer references a missing bundle version (corrupt pointer)."
                )
            return None
        return _PointerView(
            publication_id=row["publication_id"],
            decision_id=row["decision_id"],
            decision_created_at=_parse(row["decision_created_at"]),
            bundle_hash=row["bundle_hash"],
        )

    def _init_schema(self) -> None:
        # executescript은 자체적으로 선행 COMMIT을 발급하므로 BEGIN IMMEDIATE로 감싸면
        # "no transaction is active"가 된다. autocommit 모드에서 직접 실행한다.
        with self._lock:
            self._conn.executescript(_SCHEMA_SQL)


def _publication_id(
    *,
    market: str,
    symbol: str,
    decision_id: str,
    decision_created_at: datetime,
    bundle_hash: str,
) -> str:
    return payload_sha256(
        {
            "market": market,
            "symbol": symbol,
            "decision_id": decision_id,
            "decision_created_at": decision_created_at.isoformat(),
            "bundle_hash": bundle_hash,
        }
    )


def _bundle_to_json(
    bundle: DecisionTriggerBundle, *, valid_from: datetime, expires_at: datetime
) -> str:
    payload = {
        "decision": bundle.decision.model_dump(mode="json"),
        "plan": bundle.plan.model_dump(mode="json") if bundle.plan is not None else None,
        "valid_from": valid_from.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    return canonical_json_dumps(payload)


def _row_to_active_bundle(row: sqlite3.Row) -> ActiveBundle:
    try:
        payload = json.loads(row["bundle_json"])
        decision = AnalysisDecision.model_validate(payload["decision"])
        plan_payload = payload.get("plan")
        plan = TriggerPlan.model_validate(plan_payload) if plan_payload is not None else None
        bundle = DecisionTriggerBundle(decision=decision, plan=plan)
    except Exception as exc:  # noqa: BLE001 - fail-closed, no fallback
        raise PublicationError(
            "stored bundle could not be deserialized into a valid DecisionTriggerBundle."
        ) from exc

    valid_from = _parse(row["valid_from"])
    expires_at = _parse(row["expires_at"])
    # 무결성 재검증(fail-closed): 저장 후 bundle_json/columns가 변조되었거나 불일치하면 거부.
    recomputed_hash = payload_sha256(
        json.loads(_bundle_to_json(bundle, valid_from=valid_from, expires_at=expires_at))
    )
    if recomputed_hash != row["bundle_hash"]:
        raise PublicationError("stored bundle_hash does not match recomputed bundle hash.")
    recomputed_pub_id = _publication_id(
        market=row["market"],
        symbol=row["symbol"],
        decision_id=row["decision_id"],
        decision_created_at=_parse(row["decision_created_at"]),
        bundle_hash=row["bundle_hash"],
    )
    if recomputed_pub_id != row["publication_id"]:
        raise PublicationError("stored publication_id does not match recomputed id.")
    plan_id = plan.plan_id if plan is not None else None
    if (
        decision.market != row["market"]
        or decision.symbol != row["symbol"]
        or decision.decision_id.value != row["decision_id"]
        or plan_id != row["plan_id"]
    ):
        raise PublicationError("stored row identity does not match bundle identity.")

    return ActiveBundle(
        publication_id=row["publication_id"],
        market=row["market"],
        symbol=row["symbol"],
        decision_id=row["decision_id"],
        plan_id=row["plan_id"],
        decision_created_at=_parse(row["decision_created_at"]),
        valid_from=valid_from,
        expires_at=expires_at,
        bundle=bundle,
        bundle_hash=row["bundle_hash"],
        source_payload_hash=row["source_payload_hash"],
        published_at=_parse(row["published_at"]),
    )


def _market_value(market: Market | str) -> str:
    return market.value if isinstance(market, Market) else str(market)


def _parse(value: str) -> datetime:
    return require_timezone_aware_datetime(
        datetime.fromisoformat(value), field_name="stored_datetime"
    )
