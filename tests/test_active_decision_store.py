"""RTM-7c.1 — atomic decision bundle publication store tests (offline)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
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
    decision = _decision(action=AnalysisAction.HOLD)
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
