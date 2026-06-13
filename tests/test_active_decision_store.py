"""RTM-7c.1 — atomic decision bundle publication store tests (offline)."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from analysis import (
    AnalysisAction,
    AnalysisDecision,
    AnalysisReason,
    BearPerspective,
    BullPerspective,
    FundManagerDecision,
    RiskManagerEvaluation,
)
from analysis.models import ANALYSIS_DECISION_SCHEMA
from domain import DateId, DecisionId, Percent
from domain.decision import DecisionSnapshot
from domain.enums import Market
from domain.validation import ValidationResult
from market_data.conditions import Comparator, ConditionClause, Metric
from market_data.trigger_engine import TriggerPlan
from orchestration.active_decision_store import (
    ActiveDecisionStore,
    DecisionPublicationCandidate,
    PublicationError,
    PublicationStatus,
    SlotReservationStatus,
    SlotState,
)

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
DECISION_ID = "analysis-260522-001"


def _reason(date_id: str = "260522-1") -> AnalysisReason:
    return AnalysisReason(reason="근거", date_id=DateId(date_id))


def _decision(
    *,
    action: AnalysisAction = AnalysisAction.BUY,
    decision_id: str = DECISION_ID,
    created_at: datetime = NOW,
) -> AnalysisDecision:
    return AnalysisDecision(
        decision_id=DecisionId(decision_id),
        created_at=created_at,
        universe="KR_LARGE",
        symbol="005930",
        market="KR",
        summary_one_liner="요약",
        bear=BearPerspective(summary="하방", risks=("리스크",), reasons=(_reason(),)),
        bull=BullPerspective(summary="상방", catalysts=("촉매",), reasons=(_reason("260522-2"),)),
        risk_manager=RiskManagerEvaluation(summary="중립", reasons=(_reason("260522-3"),)),
        fund_manager=FundManagerDecision(
            action=action,
            target_weight_percent=Percent("5"),
            rationale="근거",
            reasons=(_reason("260522-4"),),
        ),
        reasons=(_reason("260522-5"),),
    )


def _snapshot(
    *,
    decision: AnalysisDecision | None = None,
    schema_name: str = ANALYSIS_DECISION_SCHEMA,
    raw_payload: dict[str, Any] | None = None,
) -> DecisionSnapshot:
    decision = decision or _decision()
    payload = raw_payload if raw_payload is not None else decision.model_dump(mode="json")
    return DecisionSnapshot.create(
        decision_id=decision.decision_id,
        created_at=decision.created_at,
        schema_name=schema_name,
        raw_payload=payload,
        validation_result=ValidationResult(
            passed=True, issues=(), schema_name=ANALYSIS_DECISION_SCHEMA
        ),
    )


def _plan(
    *,
    action: AnalysisAction = AnalysisAction.BUY,
    decision_id: str = DECISION_ID,
    plan_id: str = "plan-1",
    created_at: datetime = NOW,
) -> TriggerPlan:
    rule_cmp = Comparator.LTE if action is AnalysisAction.BUY else Comparator.GTE
    return TriggerPlan(
        plan_id=plan_id,
        decision_id=DecisionId(decision_id),
        created_at=created_at,
        valid_from=created_at,
        expires_at=created_at + DAY,
        universe="KR_LARGE",
        market=Market.KR,
        symbol="005930",
        action=action,
        rules=(
            ConditionClause(metric=Metric.LAST_TRADE_PRICE, comparator=rule_cmp, threshold="100"),
        ),
    )


def _candidate(
    *,
    decision: AnalysisDecision | None = None,
    plan: TriggerPlan | None = None,
    valid_from: datetime = NOW,
    expires_at: datetime = NOW + DAY,
) -> DecisionPublicationCandidate:
    decision = decision or _decision()
    snapshot = _snapshot(decision=decision)
    return DecisionPublicationCandidate(
        snapshot=snapshot, plan=plan, valid_from=valid_from, expires_at=expires_at
    )


def _store(tmp_path: Path, **kwargs: Any) -> ActiveDecisionStore:
    return ActiveDecisionStore(tmp_path / "active.sqlite3", **kwargs)


# --- publish: success paths --------------------------------------------------


def test_valid_buy_with_plan_publishes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.publish(_candidate(plan=_plan()), now=NOW)
    assert result.status is PublicationStatus.PUBLISHED
    active = store.read_active(Market.KR, "005930")
    assert active is not None
    assert active.bundle.action is AnalysisAction.BUY
    assert active.bundle.plan is not None


def test_hold_with_no_plan_publishes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    decision = _decision(action=AnalysisAction.HOLD)
    result = store.publish(_candidate(decision=decision, plan=None), now=NOW)
    assert result.status is PublicationStatus.PUBLISHED
    active = store.read_active(Market.KR, "005930")
    assert active is not None
    assert active.bundle.plan is None
    assert active.bundle.action is AnalysisAction.HOLD


# --- publish: rejections -----------------------------------------------------


def test_buy_without_plan_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.publish(_candidate(plan=None), now=NOW)
    assert result.status is PublicationStatus.REJECTED_INVALID_BUNDLE
    assert store.read_active(Market.KR, "005930") is None


def test_decision_plan_mismatch_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # BUY decision with a SELL plan → bundle validation fails → reject.
    result = store.publish(_candidate(plan=_plan(action=AnalysisAction.SELL)), now=NOW)
    assert result.status is PublicationStatus.REJECTED_INVALID_BUNDLE


def test_duplicate_republish_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.publish(_candidate(plan=_plan()), now=NOW)
    second = store.publish(_candidate(plan=_plan()), now=NOW + timedelta(seconds=5))
    assert first.status is PublicationStatus.PUBLISHED
    assert second.status is PublicationStatus.IDEMPOTENT
    assert second.publication_id == first.publication_id
    # history append-only, no duplicate version row for an idempotent re-publish.
    assert len(store.list_history(Market.KR, "005930")) == 1


def test_same_identity_different_payload_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish(_candidate(plan=_plan(plan_id="plan-1")), now=NOW)
    # same decision identity (decision_id + created_at), different plan content.
    result = store.publish(_candidate(plan=_plan(plan_id="plan-2")), now=NOW)
    assert result.status is PublicationStatus.REJECTED_CONFLICT
    # current pointer unchanged: still plan-1.
    active = store.read_active(Market.KR, "005930")
    assert active is not None and active.plan_id == "plan-1"


def test_older_decision_cannot_replace_newer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    newer = _decision(decision_id="analysis-new", created_at=NOW + timedelta(hours=1))
    store.publish(
        _candidate(
            decision=newer,
            plan=_plan(decision_id="analysis-new", created_at=NOW + timedelta(hours=1)),
            valid_from=NOW + timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1) + DAY,
        ),
        now=NOW + timedelta(hours=1),
    )
    older = _decision(decision_id="analysis-old", created_at=NOW)
    result = store.publish(
        _candidate(decision=older, plan=_plan(decision_id="analysis-old", created_at=NOW)),
        now=NOW + timedelta(hours=1),
    )
    assert result.status is PublicationStatus.REJECTED_OLDER
    active = store.read_active(Market.KR, "005930")
    assert active is not None and active.decision_id == "analysis-new"


def test_expired_candidate_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # decision.created_at <= valid_from을 만족시켜 validity-binding이 아닌 만료 경로를 탄다.
    decision = _decision(action=AnalysisAction.HOLD, created_at=NOW - timedelta(hours=3))
    result = store.publish(
        _candidate(
            decision=decision,
            plan=None,
            valid_from=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(hours=1),
        ),
        now=NOW,
    )
    assert result.status is PublicationStatus.REJECTED_EXPIRED
    assert store.read_active(Market.KR, "005930") is None


# --- atomicity / corruption --------------------------------------------------


def test_transaction_failure_leaves_pointer_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish(_candidate(plan=_plan(plan_id="plan-1")), now=NOW)

    def _boom() -> None:
        raise RuntimeError("simulated mid-transaction failure")

    store._fault_after_version_insert = _boom  # type: ignore[attr-defined]
    newer = _decision(decision_id="analysis-new", created_at=NOW + timedelta(hours=1))
    with pytest.raises(RuntimeError):
        store.publish(
            _candidate(
                decision=newer,
                plan=_plan(decision_id="analysis-new", created_at=NOW + timedelta(hours=1)),
                valid_from=NOW + timedelta(hours=1),
                expires_at=NOW + timedelta(hours=1) + DAY,
            ),
            now=NOW + timedelta(hours=1),
        )
    # pointer unchanged, failed version rolled back (history still has only plan-1).
    active = store.read_active(Market.KR, "005930")
    assert active is not None and active.plan_id == "plan-1"
    assert len(store.list_history(Market.KR, "005930")) == 1


def test_corrupt_pointer_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish(_candidate(plan=_plan()), now=NOW)
    store._conn.execute(  # type: ignore[attr-defined]
        "UPDATE active_decision_pointers SET publication_id = ? WHERE market = ? AND symbol = ?",
        ("does-not-exist", "KR", "005930"),
    )
    store._conn.commit()  # type: ignore[attr-defined]
    with pytest.raises(PublicationError):
        store.read_active(Market.KR, "005930")


def test_reader_deserialize_failure_no_fallback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish(_candidate(plan=_plan()), now=NOW)
    store._conn.execute(  # type: ignore[attr-defined]
        "UPDATE decision_bundle_versions SET bundle_json = ? WHERE 1 = 1",
        ('{"decision": {"garbage": true}, "plan": null}',),
    )
    store._conn.commit()  # type: ignore[attr-defined]
    with pytest.raises(PublicationError):
        store.read_active(Market.KR, "005930")


# --- isolation / ordering ----------------------------------------------------


def test_symbols_are_independent(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def _for_symbol(symbol: str) -> DecisionPublicationCandidate:
        decision = _decision(action=AnalysisAction.HOLD, decision_id=f"d-{symbol}")
        decision = decision.model_copy(update={"symbol": symbol})
        return DecisionPublicationCandidate(
            snapshot=_snapshot(decision=decision),
            plan=None,
            valid_from=NOW,
            expires_at=NOW + DAY,
        )

    assert store.publish(_for_symbol("005930"), now=NOW).status is PublicationStatus.PUBLISHED
    assert store.publish(_for_symbol("000660"), now=NOW).status is PublicationStatus.PUBLISHED
    a = store.read_active(Market.KR, "005930")
    b = store.read_active(Market.KR, "000660")
    assert a is not None and a.bundle.decision.symbol == "005930"
    assert b is not None and b.bundle.decision.symbol == "000660"


def test_reader_sees_old_or_new_never_partial(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = _decision(decision_id="analysis-old", created_at=NOW)
    store.publish(
        _candidate(decision=old, plan=_plan(decision_id="analysis-old", created_at=NOW)), now=NOW
    )
    before = store.read_active(Market.KR, "005930")
    assert before is not None and before.decision_id == "analysis-old"

    new_at = NOW + timedelta(hours=1)
    new = _decision(decision_id="analysis-new", created_at=new_at)
    store.publish(
        _candidate(
            decision=new,
            plan=_plan(decision_id="analysis-new", created_at=new_at),
            valid_from=new_at,
            expires_at=new_at + DAY,
        ),
        now=new_at,
    )
    after = store.read_active(Market.KR, "005930")
    # always a complete bundle — old then new, never a partial mix.
    assert after is not None and after.decision_id == "analysis-new"
    assert after.bundle.decision.decision_id.value == "analysis-new"
    # history append-only: both versions retained.
    assert len(store.list_history(Market.KR, "005930")) == 2


def test_concurrent_writers_one_winner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    results: list[PublicationStatus] = []
    lock = threading.Lock()
    start = threading.Barrier(2)

    def _worker() -> None:
        start.wait()
        res = store.publish(_candidate(plan=_plan()), now=NOW)
        with lock:
            results.append(res.status)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # exactly one PUBLISHED winner; the other is idempotent; one version row.
    assert results.count(PublicationStatus.PUBLISHED) == 1
    assert results.count(PublicationStatus.IDEMPOTENT) == 1
    assert len(store.list_history(Market.KR, "005930")) == 1


# --- BLOCKER 4: candidate↔plan validity binding ------------------------------


def test_candidate_plan_validity_mismatch_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # plan validity = [NOW, NOW+DAY]; candidate validity diverges → fail-closed.
    result = store.publish(
        _candidate(
            decision=_decision(action=AnalysisAction.BUY),
            plan=_plan(action=AnalysisAction.BUY),
            valid_from=NOW + timedelta(minutes=1),
            expires_at=NOW + DAY,
        ),
        now=NOW,
    )
    assert result.status is PublicationStatus.REJECTED_INVALID_BUNDLE
    assert store.read_active(Market.KR, "005930") is None


def test_decision_created_after_valid_from_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # HOLD(plan 없음)이라도 decision.created_at > valid_from이면 거부.
    decision = _decision(action=AnalysisAction.HOLD, created_at=NOW)
    result = store.publish(
        _candidate(
            decision=decision,
            plan=None,
            valid_from=NOW - timedelta(hours=1),
            expires_at=NOW + DAY,
        ),
        now=NOW,
    )
    assert result.status is PublicationStatus.REJECTED_INVALID_BUNDLE


# --- BLOCKER 3: read-time integrity (hash + identity) ------------------------


def test_read_time_identity_tamper_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish(_candidate(plan=_plan()), now=NOW)
    # identity 컬럼(market)을 변조 → 저장 hash/publication_id 재계산과 불일치 → fail-closed.
    store._conn.execute(  # type: ignore[attr-defined]
        "UPDATE decision_bundle_versions SET market = 'US'"
    )
    store._conn.commit()  # type: ignore[attr-defined]
    with pytest.raises(PublicationError):
        store.read_active(Market.KR, "005930")


# --- BLOCKER 2: cross-connection writer ordering -----------------------------


def test_cross_connection_older_cannot_cover_newer(tmp_path: Path) -> None:
    path = tmp_path / "active.sqlite3"
    store_a = ActiveDecisionStore(path)
    store_b = ActiveDecisionStore(path)
    new_at = NOW + timedelta(hours=1)
    # store_a가 최신 결정을 먼저 게시.
    assert (
        store_a.publish(
            _candidate(
                decision=_decision(decision_id="d-new", created_at=new_at),
                plan=_plan(decision_id="d-new", created_at=new_at),
                valid_from=new_at,
                expires_at=new_at + DAY,
            ),
            now=new_at,
        ).status
        is PublicationStatus.PUBLISHED
    )
    # 다른 connection(store_b)이 더 오래된 결정으로 덮으려 해도 거부된다.
    older = store_b.publish(
        _candidate(
            decision=_decision(decision_id="d-old", created_at=NOW),
            plan=_plan(decision_id="d-old", created_at=NOW),
        ),
        now=new_at,
    )
    assert older.status is PublicationStatus.REJECTED_OLDER
    # 양쪽 connection 모두 최신 결정을 final pointer로 본다.
    a = store_a.read_active(Market.KR, "005930")
    b = store_b.read_active(Market.KR, "005930")
    assert a is not None and a.decision_id == "d-new"
    assert b is not None and b.decision_id == "d-new"
    store_a.close()
    store_b.close()


# --- BLOCKER 1: durable slot journal -----------------------------------------

_SD = date(2026, 5, 22)


def test_slot_reserve_then_finalize_is_terminal_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "active.sqlite3"
    store_a = ActiveDecisionStore(path)
    r1 = store_a.reserve_slot(
        market=Market.KR, session_date=_SD, slot_id="s1", scheduled_at=NOW, now=NOW
    )
    assert r1.status is SlotReservationStatus.RESERVED
    store_a.finalize_slot(
        market=Market.KR, session_date=_SD, slot_id="s1", scheduled_at=NOW,
        state=SlotState.PUBLISHED, now=NOW, outcome="published", publication_id="p1",
    )
    store_a.close()

    # 재시작(새 인스턴스)에서도 종료 상태가 유지되어 재실행되지 않는다.
    store_b = ActiveDecisionStore(path)
    r2 = store_b.reserve_slot(
        market=Market.KR, session_date=_SD, slot_id="s1", scheduled_at=NOW, now=NOW
    )
    assert r2.status is SlotReservationStatus.ALREADY_TERMINAL
    assert r2.existing_state is SlotState.PUBLISHED
    assert store_b.slot_states(Market.KR, _SD)["s1"] is SlotState.PUBLISHED
    store_b.close()


def test_dangling_reserved_detected_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "active.sqlite3"
    store_a = ActiveDecisionStore(path)
    store_a.reserve_slot(
        market=Market.KR, session_date=_SD, slot_id="s1", scheduled_at=NOW, now=NOW
    )
    store_a.close()  # finalize 없이 종료(크래시 모사) → RESERVED 잔존.

    store_b = ActiveDecisionStore(path)
    r = store_b.reserve_slot(
        market=Market.KR, session_date=_SD, slot_id="s1", scheduled_at=NOW, now=NOW
    )
    assert r.status is SlotReservationStatus.DANGLING_RESERVED
    assert r.existing_state is SlotState.RESERVED
    store_b.close()


def test_finalize_requires_terminal_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(PublicationError):
        store.finalize_slot(
            market=Market.KR, session_date=_SD, slot_id="s1", scheduled_at=NOW,
            state=SlotState.RESERVED, now=NOW,
        )
