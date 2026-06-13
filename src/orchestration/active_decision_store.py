"""RTM-7c.1 — atomic decision bundle publication store (offline).

검증된 `DecisionSnapshot` + (BUY/SELL이면) 별도 구조화 입력 `TriggerPlan`을 받아
`load_decision_bundle`로 한 번 더 검증한 뒤, version INSERT와 pointer UPDATE를 단일
SQLite 트랜잭션으로 원자적으로 게시한다. reader는 pointer가 가리키는 **완성된** bundle만
보거나(old/new) 아무것도 보지 못한다 — 절대 부분 상태를 보지 않는다.

network/broker/ledger/paper_execution/LLM 접근이 없다. 게시 정책은 fail-closed다:
  - schema/검증 실패·plan 누락·decision↔plan 불일치 → REJECTED_INVALID_BUNDLE
  - 동일 identity·동일 content → IDEMPOTENT (재게시 no-op)
  - 동일 identity·다른 content → REJECTED_CONFLICT (silent 삼킴 금지)
  - 더 오래된 결정으로 최신 결정 교체 → REJECTED_OLDER
  - 게시 시점에 이미 만료 → REJECTED_EXPIRED
  - pointer가 존재하지 않는 version을 가리킴(손상) → PublicationError (fail-closed)
  - reader 역직렬화 실패 → PublicationError (fallback 없음)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
]


class PublicationError(Exception):
    """게시/판독 중 fail-closed로 표면화하는 오류(손상된 pointer, 역직렬화 실패 등)."""


class PublicationStatus(StrEnum):
    """게시 시도 결과. 정상 거부는 예외가 아니라 typed 상태로 반환한다."""

    PUBLISHED = "published"
    IDEMPOTENT = "idempotent"
    REJECTED_INVALID_BUNDLE = "rejected_invalid_bundle"
    REJECTED_CONFLICT = "rejected_conflict"
    REJECTED_OLDER = "rejected_older"
    REJECTED_EXPIRED = "rejected_expired"


@dataclass(frozen=True)
class DecisionPublicationCandidate:
    """게시 후보. `TriggerPlan`은 prose에서 추론하지 않고 반드시 별도 구조화 입력으로 받는다.

    HOLD면 plan은 None이며, BUY/SELL이면 plan이 필수다(검증은 load_decision_bundle이 수행).
    valid_from/expires_at은 활성 bundle의 유효 구간이다(게시 시점 만료 판정에 사용).
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
"""


@dataclass(frozen=True)
class _PointerView:
    publication_id: str
    decision_id: str
    decision_created_at: datetime
    bundle_hash: str


class ActiveDecisionStore:
    """원자적 decision bundle 게시 저장소. version 이력은 append-only, pointer만 교체한다.

    동일 connection을 thread-safe하게 공유하기 위해 모든 쓰기를 단일 Lock으로 직렬화한다
    (동시 writer race에서 정확히 하나만 승자가 되도록). 읽기도 같은 connection을 쓰되
    pointer→version을 한 트랜잭션 안에서 읽어 부분 상태를 보지 않는다.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        _fault_after_version_insert: Callable[[], None] | None = None,
    ) -> None:
        self._db_path = str(db_path)
        # check_same_thread=False: 동시 writer race 테스트(스레드)에서 같은 connection을
        # Lock으로 보호하며 공유한다. 모든 변경은 _lock 안에서만 일어난다.
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
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
        market = decision.market
        symbol = decision.symbol
        decision_id = decision.decision_id.value
        decision_created_at = decision.created_at
        plan_id = bundle.plan.plan_id if bundle.plan is not None else None
        bundle_json = _bundle_to_json(bundle, valid_from=valid_from, expires_at=expires_at)
        bundle_hash = payload_sha256(json.loads(bundle_json))
        source_payload_hash = candidate.snapshot.payload_hash
        publication_id = payload_sha256(
            {
                "market": market,
                "symbol": symbol,
                "decision_id": decision_id,
                "decision_created_at": decision_created_at.isoformat(),
                "bundle_hash": bundle_hash,
            }
        )

        with self._lock:
            current = self._read_pointer_view(market, symbol)
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

            self._atomic_publish(
                publication_id=publication_id,
                market=market,
                symbol=symbol,
                decision_id=decision_id,
                plan_id=plan_id,
                decision_created_at=decision_created_at,
                valid_from=valid_from,
                expires_at=expires_at,
                bundle_json=bundle_json,
                bundle_hash=bundle_hash,
                source_payload_hash=source_payload_hash,
                published_at=now,
            )
            return PublicationResult(PublicationStatus.PUBLISHED, publication_id)

    def _atomic_publish(
        self,
        *,
        publication_id: str,
        market: str,
        symbol: str,
        decision_id: str,
        plan_id: str | None,
        decision_created_at: datetime,
        valid_from: datetime,
        expires_at: datetime,
        bundle_json: str,
        bundle_hash: str,
        source_payload_hash: str,
        published_at: datetime,
    ) -> None:
        """version INSERT + pointer UPSERT를 단일 트랜잭션으로 수행한다.

        중간에 어떤 실패가 나도 rollback되어 pointer는 직전 값을 그대로 유지한다(부분 게시 금지)."""
        try:
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
                    published_at.isoformat(),
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
                (market, symbol, publication_id, published_at.isoformat()),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # --- read ------------------------------------------------------------------

    def read_active(self, market: Market | str, symbol: str) -> ActiveBundle | None:
        """현재 pointer가 가리키는 완성된 활성 bundle을 반환한다.

        pointer가 없으면 None. pointer가 존재하지 않는 version을 가리키면(손상) 또는
        bundle_json 역직렬화가 실패하면 PublicationError로 fail-closed한다(fallback 없음)."""
        market_value = market.value if isinstance(market, Market) else str(market)
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
        market_value = market.value if isinstance(market, Market) else str(market)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decision_bundle_versions WHERE market = ? AND symbol = ? "
                "ORDER BY published_at ASC, publication_id ASC",
                (market_value, symbol),
            ).fetchall()
        return tuple(_row_to_active_bundle(row) for row in rows)

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
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()


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
    return ActiveBundle(
        publication_id=row["publication_id"],
        market=row["market"],
        symbol=row["symbol"],
        decision_id=row["decision_id"],
        plan_id=row["plan_id"],
        decision_created_at=_parse(row["decision_created_at"]),
        valid_from=_parse(row["valid_from"]),
        expires_at=_parse(row["expires_at"]),
        bundle=bundle,
        bundle_hash=row["bundle_hash"],
        source_payload_hash=row["source_payload_hash"],
        published_at=_parse(row["published_at"]),
    )


def _parse(value: str) -> datetime:
    return require_timezone_aware_datetime(
        datetime.fromisoformat(value), field_name="stored_datetime"
    )
