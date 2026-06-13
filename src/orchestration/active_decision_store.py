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
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
    REJECTED_RESERVATION_LOST = "rejected_reservation_lost"


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
    RESERVED = "reserved"  # 이번 호출에서 새로 예약됨 → 실행 진행(token 반환)
    ALREADY_TERMINAL = "already_terminal"  # 이미 종료된 slot(소비됨) → 재실행 금지
    ACTIVE_ELSEWHERE = "active_elsewhere"  # 다른 owner가 lease 유효한 채 실행 중 → 대기/skip
    DANGLING_RESERVED = "dangling_reserved"  # lease 만료된 잔존 예약(크래시) → reconcile 필요


@dataclass(frozen=True)
class SlotReservation:
    status: SlotReservationStatus
    token: str | None = None  # RESERVED일 때만 채워지는 소유권 토큰(finalize CAS용)
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
    reservation_token TEXT,
    owner_id TEXT,
    reserved_at TEXT NOT NULL,
    lease_expires_at TEXT,
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


@dataclass(frozen=True)
class _Prepared:
    """검증을 통과한 게시 후보의 파생값. `publish`/`publish_reserved_slot`이 공유한다."""

    market: str
    symbol: str
    decision_id: str
    decision_created_at: datetime
    plan_id: str | None
    valid_from: datetime
    expires_at: datetime
    bundle_json: str
    bundle_hash: str
    source_payload_hash: str
    publication_id: str


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
        self,
        candidate: DecisionPublicationCandidate,
        *,
        now: datetime,
        expected_market: Market | str | None = None,
    ) -> PublicationResult:
        """검증된 후보를 원자적으로 게시한다. 정상 거부는 typed 상태로 반환한다(fail-closed).

        `expected_market`이 주어지면 후보 decision의 market과 정확히 일치해야 한다
        (scheduler↔candidate market binding: KR 스케줄러가 US 후보를 게시하지 못하게 함).
        """
        prepared = self._validate_and_prepare(
            candidate, now=now, expected_market=expected_market
        )
        if isinstance(prepared, PublicationResult):
            return prepared

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._read_pointer_view(prepared.market, prepared.symbol)
                result = self._publish_decision(
                    current=current,
                    decision_id=prepared.decision_id,
                    decision_created_at=prepared.decision_created_at,
                    bundle_hash=prepared.bundle_hash,
                    expires_at=prepared.expires_at,
                    now=now,
                    publication_id=prepared.publication_id,
                    market=prepared.market,
                    symbol=prepared.symbol,
                    plan_id=prepared.plan_id,
                    valid_from=prepared.valid_from,
                    bundle_json=prepared.bundle_json,
                    source_payload_hash=prepared.source_payload_hash,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return result

    def _validate_and_prepare(
        self,
        candidate: DecisionPublicationCandidate,
        *,
        now: datetime,
        expected_market: Market | str | None,
    ) -> _Prepared | PublicationResult:
        """후보를 검증하고 게시 파생값을 계산한다. 정상 거부는 `PublicationResult`로 반환한다.

        DB를 만지지 않는 순수 검증 단계이므로 `publish`와 `publish_reserved_slot`이
        동일한 게시 정책을 공유한다(중복 구현 금지)."""
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

        # 1b) market binding: 후보 decision market이 기대 market과 일치해야 한다.
        if expected_market is not None and decision.market != _market_value(expected_market):
            return PublicationResult(
                PublicationStatus.REJECTED_INVALID_BUNDLE,
                reason="decision market does not match expected market",
            )

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

        bundle_json = _bundle_to_json(bundle, valid_from=valid_from, expires_at=expires_at)
        bundle_hash = payload_sha256(json.loads(bundle_json))
        market = decision.market
        symbol = decision.symbol
        decision_id = decision.decision_id.value
        return _Prepared(
            market=market,
            symbol=symbol,
            decision_id=decision_id,
            decision_created_at=decision_created_at,
            plan_id=bundle.plan.plan_id if bundle.plan is not None else None,
            valid_from=valid_from,
            expires_at=expires_at,
            bundle_json=bundle_json,
            bundle_hash=bundle_hash,
            source_payload_hash=candidate.snapshot.payload_hash,
            publication_id=_publication_id(
                market=market,
                symbol=symbol,
                decision_id=decision_id,
                decision_created_at=decision_created_at,
                bundle_hash=bundle_hash,
            ),
        )

    def publish_reserved_slot(
        self,
        candidate: DecisionPublicationCandidate,
        *,
        now: datetime,
        market: Market | str,
        session_date: date,
        slot_id: str,
        token: str,
        expected_market: Market | str | None = None,
    ) -> PublicationResult:
        """예약 토큰을 보유한 owner가 단일 트랜잭션에서 게시 + slot 종료를 원자적으로 수행한다.

        같은 `BEGIN IMMEDIATE` 안에서:
          1) 예약 가드: slot이 여전히 RESERVED + 동일 token + lease 유효해야 한다.
             아니면 ROLLBACK + REJECTED_RESERVATION_LOST (active pointer 절대 불변).
          2) 검증 거부(invalid/conflict/older/expired)면 slot을 FAILED로 종료(pointer 불변).
          3) 정상 게시면 version insert + active pointer update + slot PUBLISHED를 함께 커밋.

        publish와 complete_slot을 별도 트랜잭션으로 두던 crash window와, lease 만료 후에도
        게시되던 ownership 경합을 단일 트랜잭션으로 닫는다."""
        prepared = self._validate_and_prepare(
            candidate, now=now, expected_market=expected_market
        )
        market_value = _market_value(market)
        sd = session_date.isoformat()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # 1) 예약 가드(CAS): RESERVED + token 일치 + lease 유효.
                row = self._conn.execute(
                    "SELECT state, reservation_token, lease_expires_at "
                    "FROM decision_refresh_slots "
                    "WHERE market = ? AND session_date = ? AND slot_id = ?",
                    (market_value, sd, slot_id),
                ).fetchone()
                if not self._reservation_held(row, token=token, now=now):
                    self._conn.execute("ROLLBACK")
                    return PublicationResult(
                        PublicationStatus.REJECTED_RESERVATION_LOST,
                        reason="slot reservation lost (state/token/lease changed).",
                    )

                # 2) 검증 거부: slot을 FAILED로 종료하되 active pointer는 건드리지 않는다.
                if isinstance(prepared, PublicationResult):
                    self._finalize_slot_in_tx(
                        market_value=market_value,
                        sd=sd,
                        slot_id=slot_id,
                        token=token,
                        state=SlotState.FAILED,
                        now=now,
                        outcome=prepared.status.value,
                        publication_id=prepared.publication_id,
                    )
                    self._conn.execute("COMMIT")
                    return prepared

                # 3) 게시.
                current = self._read_pointer_view(prepared.market, prepared.symbol)
                result = self._publish_decision(
                    current=current,
                    decision_id=prepared.decision_id,
                    decision_created_at=prepared.decision_created_at,
                    bundle_hash=prepared.bundle_hash,
                    expires_at=prepared.expires_at,
                    now=now,
                    publication_id=prepared.publication_id,
                    market=prepared.market,
                    symbol=prepared.symbol,
                    plan_id=prepared.plan_id,
                    valid_from=prepared.valid_from,
                    bundle_json=prepared.bundle_json,
                    source_payload_hash=prepared.source_payload_hash,
                )
                slot_state = (
                    SlotState.PUBLISHED
                    if result.status
                    in (PublicationStatus.PUBLISHED, PublicationStatus.IDEMPOTENT)
                    else SlotState.FAILED
                )
                self._finalize_slot_in_tx(
                    market_value=market_value,
                    sd=sd,
                    slot_id=slot_id,
                    token=token,
                    state=slot_state,
                    now=now,
                    outcome=result.status.value,
                    publication_id=result.publication_id,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return result

    @staticmethod
    def _reservation_held(
        row: sqlite3.Row | None, *, token: str, now: datetime
    ) -> bool:
        """slot row가 여전히 RESERVED + 동일 token + lease 유효한지(소유권 유지) 검사."""
        if row is None or SlotState(row["state"]) is not SlotState.RESERVED:
            return False
        if row["reservation_token"] != token:
            return False
        lease = (
            _parse(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        )
        return lease is not None and lease > now

    def _finalize_slot_in_tx(
        self,
        *,
        market_value: str,
        sd: str,
        slot_id: str,
        token: str,
        state: SlotState,
        now: datetime,
        outcome: str | None,
        publication_id: str | None,
    ) -> None:
        """진행 중인 트랜잭션 안에서 RESERVED slot을 종료 상태로 전이한다(CAS).

        호출 전 예약 가드를 이미 통과했으므로 정확히 1행이 갱신되어야 한다 — 아니면 예약
        불변식이 깨진 것이므로 fail-closed."""
        cur = self._conn.execute(
            """
            UPDATE decision_refresh_slots
            SET state = ?, finished_at = ?, outcome = ?, publication_id = ?
            WHERE market = ? AND session_date = ? AND slot_id = ?
              AND state = 'reserved' AND reservation_token = ?
            """,
            (
                state.value,
                now.isoformat(),
                outcome,
                publication_id,
                market_value,
                sd,
                slot_id,
                token,
            ),
        )
        if cur.rowcount != 1:
            raise PublicationError(
                "reserved slot disappeared mid-transaction (reservation invariant broken)."
            )

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
        owner_id: str,
        now: datetime,
        lease_seconds: float,
    ) -> SlotReservation:
        """slot을 durable하게 예약한다(runner 호출 전 1회). lease+token으로 소유권을 표현해
        '실행 중인 예약'과 '크래시 잔존 예약'을 구분한다.

        - 미존재 → RESERVED (token 발급, 실행 진행)
        - 종료 상태(published/missed/failed/...) 존재 → ALREADY_TERMINAL (재실행 금지)
        - RESERVED + lease 유효 → ACTIVE_ELSEWHERE (다른 owner 실행 중, 건드리지 않음)
        - RESERVED + lease 만료 → DANGLING_RESERVED (크래시 잔존, reconcile 필요)
        """
        market_value = _market_value(market)
        sd = session_date.isoformat()
        token = uuid.uuid4().hex
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._lock:
            with self._immediate():
                row = self._conn.execute(
                    "SELECT state, lease_expires_at FROM decision_refresh_slots "
                    "WHERE market = ? AND session_date = ? AND slot_id = ?",
                    (market_value, sd, slot_id),
                ).fetchone()
                if row is not None:
                    state = SlotState(row["state"])
                    if state is not SlotState.RESERVED:
                        return SlotReservation(
                            SlotReservationStatus.ALREADY_TERMINAL, existing_state=state
                        )
                    lease = (
                        _parse(row["lease_expires_at"])
                        if row["lease_expires_at"] is not None
                        else None
                    )
                    if lease is not None and lease > now:
                        return SlotReservation(
                            SlotReservationStatus.ACTIVE_ELSEWHERE, existing_state=state
                        )
                    return SlotReservation(
                        SlotReservationStatus.DANGLING_RESERVED, existing_state=state
                    )
                self._conn.execute(
                    """
                    INSERT INTO decision_refresh_slots (
                        market, session_date, slot_id, scheduled_at, state,
                        reservation_token, owner_id, reserved_at, lease_expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        market_value,
                        sd,
                        slot_id,
                        scheduled_at.isoformat(),
                        SlotState.RESERVED.value,
                        token,
                        owner_id,
                        now.isoformat(),
                        lease_expires_at.isoformat(),
                    ),
                )
        return SlotReservation(SlotReservationStatus.RESERVED, token=token)

    def complete_slot(
        self,
        *,
        market: Market | str,
        session_date: date,
        slot_id: str,
        state: SlotState,
        token: str,
        now: datetime,
        outcome: str | None = None,
        publication_id: str | None = None,
    ) -> bool:
        """예약을 보유한 owner가 slot을 종료 상태로 전이한다(CAS). 토큰이 일치하고 아직
        RESERVED일 때만 성공한다 — 다른 owner/이미 종료된 slot은 덮어쓰지 못한다.

        반환값은 전이 성공 여부. terminal이 아닌 state는 거부한다."""
        if state not in _TERMINAL_SLOT_STATES:
            raise PublicationError(f"complete_slot state must be terminal, got {state!r}.")
        market_value = _market_value(market)
        sd = session_date.isoformat()
        with self._lock:
            with self._immediate():
                cur = self._conn.execute(
                    """
                    UPDATE decision_refresh_slots
                    SET state = ?, finished_at = ?, outcome = ?, publication_id = ?
                    WHERE market = ? AND session_date = ? AND slot_id = ?
                      AND state = 'reserved' AND reservation_token = ?
                    """,
                    (
                        state.value,
                        now.isoformat(),
                        outcome,
                        publication_id,
                        market_value,
                        sd,
                        slot_id,
                        token,
                    ),
                )
                changed = cur.rowcount
        return changed == 1

    def mark_unreserved_terminal(
        self,
        *,
        market: Market | str,
        session_date: date,
        slot_id: str,
        scheduled_at: datetime,
        state: SlotState,
        now: datetime,
        outcome: str | None = None,
    ) -> bool:
        """예약 없이 종료 상태(missed/missed_session_closed)를 기록한다. 이미 row가 있으면
        (예약 중이든 종료든) 덮어쓰지 않는다(INSERT OR IGNORE). 반환값은 새로 기록 여부."""
        if state not in (SlotState.MISSED, SlotState.MISSED_SESSION_CLOSED):
            raise PublicationError(
                f"mark_unreserved_terminal state must be a missed state, got {state!r}."
            )
        market_value = _market_value(market)
        sd = session_date.isoformat()
        with self._lock:
            with self._immediate():
                cur = self._conn.execute(
                    """
                    INSERT INTO decision_refresh_slots (
                        market, session_date, slot_id, scheduled_at, state,
                        reserved_at, finished_at, outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market, session_date, slot_id) DO NOTHING
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
                    ),
                )
                changed = cur.rowcount
        return changed == 1

    def expired_reservations(
        self, market: Market | str, session_date: date, now: datetime
    ) -> tuple[tuple[str, datetime], ...]:
        """lease가 만료된 RESERVED slot 목록((slot_id, scheduled_at))을 반환한다(크래시 잔존)."""
        market_value = _market_value(market)
        sd = session_date.isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT slot_id, scheduled_at, lease_expires_at "
                "FROM decision_refresh_slots "
                "WHERE market = ? AND session_date = ? AND state = 'reserved'",
                (market_value, sd),
            ).fetchall()
        out: list[tuple[str, datetime]] = []
        for row in rows:
            lease = (
                _parse(row["lease_expires_at"])
                if row["lease_expires_at"] is not None
                else None
            )
            if lease is not None and lease <= now:
                out.append((row["slot_id"], _parse(row["scheduled_at"])))
        return tuple(out)

    def reconcile_expired_reservation(
        self,
        *,
        market: Market | str,
        session_date: date,
        slot_id: str,
        now: datetime,
        outcome: str | None = None,
    ) -> bool:
        """lease가 만료된 RESERVED slot을 UNCERTAIN으로 reconcile한다(fail-closed, 재실행 금지).

        lease가 아직 유효(다른 owner 실행 중)하면 건드리지 않는다. 반환값은 전이 성공 여부."""
        market_value = _market_value(market)
        sd = session_date.isoformat()
        with self._lock:
            with self._immediate():
                row = self._conn.execute(
                    "SELECT state, lease_expires_at FROM decision_refresh_slots "
                    "WHERE market = ? AND session_date = ? AND slot_id = ?",
                    (market_value, sd, slot_id),
                ).fetchone()
                if row is None or SlotState(row["state"]) is not SlotState.RESERVED:
                    return False
                lease = (
                    _parse(row["lease_expires_at"])
                    if row["lease_expires_at"] is not None
                    else None
                )
                if lease is None or lease > now:
                    return False
                self._conn.execute(
                    """
                    UPDATE decision_refresh_slots
                    SET state = ?, finished_at = ?, outcome = ?
                    WHERE market = ? AND session_date = ? AND slot_id = ?
                      AND state = 'reserved'
                    """,
                    (
                        SlotState.UNCERTAIN.value,
                        now.isoformat(),
                        outcome,
                        market_value,
                        sd,
                        slot_id,
                    ),
                )
        return True

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
    # 1) 저장된 bundle_json 전체에 대한 hash 검증(fail-closed): top-level 키/추가 키/
    #    valid_from/expires_at 변조까지 모두 잡는다(컬럼으로 재구성하지 않는다).
    try:
        stored_payload = json.loads(row["bundle_json"])
    except Exception as exc:  # noqa: BLE001 - fail-closed, no fallback
        raise PublicationError("stored bundle_json is not valid JSON.") from exc
    if not isinstance(stored_payload, dict):
        raise PublicationError("stored bundle_json must be a JSON object.")
    try:
        recomputed_hash = payload_sha256(stored_payload)
    except Exception as exc:  # noqa: BLE001 - fail-closed
        raise PublicationError("stored bundle_json could not be canonicalized.") from exc
    if recomputed_hash != row["bundle_hash"]:
        raise PublicationError("stored bundle_hash does not match the stored payload.")

    # 2) 모델 복원: 검증 가능한 DecisionTriggerBundle이어야 한다.
    try:
        decision = AnalysisDecision.model_validate(stored_payload["decision"])
        plan_payload = stored_payload.get("plan")
        plan = TriggerPlan.model_validate(plan_payload) if plan_payload is not None else None
        bundle = DecisionTriggerBundle(decision=decision, plan=plan)
    except Exception as exc:  # noqa: BLE001 - fail-closed, no fallback
        raise PublicationError(
            "stored bundle could not be deserialized into a valid DecisionTriggerBundle."
        ) from exc

    valid_from = _parse(row["valid_from"])
    expires_at = _parse(row["expires_at"])
    decision_created_at = _parse(row["decision_created_at"])

    # 3) publication_id 재계산 일치(식별 + hash 결합) 검증.
    recomputed_pub_id = _publication_id(
        market=row["market"],
        symbol=row["symbol"],
        decision_id=row["decision_id"],
        decision_created_at=decision_created_at,
        bundle_hash=row["bundle_hash"],
    )
    if recomputed_pub_id != row["publication_id"]:
        raise PublicationError("stored publication_id does not match recomputed id.")

    # 4) 저장 payload의 top-level validity/created_at이 컬럼과 일치해야 한다.
    if (
        stored_payload.get("valid_from") != row["valid_from"]
        or stored_payload.get("expires_at") != row["expires_at"]
    ):
        raise PublicationError("stored payload validity does not match stored columns.")

    # 5) identity 대조: 컬럼 vs bundle 내부 정체성(created_at 포함).
    plan_id = plan.plan_id if plan is not None else None
    if (
        decision.market != row["market"]
        or decision.symbol != row["symbol"]
        or decision.decision_id.value != row["decision_id"]
        or plan_id != row["plan_id"]
        or decision.created_at != decision_created_at
    ):
        raise PublicationError("stored row identity does not match bundle identity.")

    return ActiveBundle(
        publication_id=row["publication_id"],
        market=row["market"],
        symbol=row["symbol"],
        decision_id=row["decision_id"],
        plan_id=row["plan_id"],
        decision_created_at=decision_created_at,
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
