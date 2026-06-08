from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from domain.validation import (
    ValidationIssue,
    ValidationResult,
    ValidationSeverity,
)
from market_data.conditions import Comparator, ConditionClause, Metric
from market_data.decision_loader import DecisionLoadError, load_decision_bundle
from market_data.trigger_engine import DecisionTriggerBundle, TriggerPlan

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
DECISION_ID = "analysis-260522-001"


def _reason(date_id: str = "260522-1") -> AnalysisReason:
    return AnalysisReason(reason="근거", date_id=DateId(date_id))


def _decision(
    *, action: AnalysisAction = AnalysisAction.BUY, decision_id: str = DECISION_ID
) -> AnalysisDecision:
    return AnalysisDecision(
        decision_id=DecisionId(decision_id),
        created_at=NOW,
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


def _passed_result() -> ValidationResult:
    return ValidationResult(passed=True, issues=(), schema_name=ANALYSIS_DECISION_SCHEMA)


def _failed_result() -> ValidationResult:
    return ValidationResult(
        passed=False,
        issues=(
            ValidationIssue(code="E1", message="bad", severity=ValidationSeverity.ERROR),
        ),
    )


def _snapshot(
    *,
    decision: AnalysisDecision | None = None,
    schema_name: str = ANALYSIS_DECISION_SCHEMA,
    validation_result: ValidationResult | None = None,
    created_at: datetime = NOW,
    raw_payload: dict[str, Any] | None = None,
) -> DecisionSnapshot:
    decision = decision or _decision()
    payload = raw_payload if raw_payload is not None else decision.model_dump(mode="json")
    return DecisionSnapshot.create(
        decision_id=decision.decision_id,
        created_at=created_at,
        schema_name=schema_name,
        raw_payload=payload,
        validation_result=validation_result or _passed_result(),
    )


def _plan(*, action: AnalysisAction = AnalysisAction.BUY, decision_id: str = DECISION_ID) -> TriggerPlan:
    rule_cmp = Comparator.LTE if action is AnalysisAction.BUY else Comparator.GTE
    return TriggerPlan(
        plan_id="plan-1",
        decision_id=DecisionId(decision_id),
        created_at=NOW,
        valid_from=NOW,
        expires_at=NOW + DAY,
        universe="KR_LARGE",
        market=Market.KR,
        symbol="005930",
        action=action,
        rules=(ConditionClause(metric=Metric.LAST_TRADE_PRICE, comparator=rule_cmp, threshold="100"),),
    )


def test_load_buy_bundle_succeeds() -> None:
    bundle = load_decision_bundle(_snapshot(), _plan())
    assert isinstance(bundle, DecisionTriggerBundle)
    assert bundle.action is AnalysisAction.BUY
    assert bundle.plan is not None


def test_load_hold_bundle_with_no_plan_succeeds() -> None:
    decision = _decision(action=AnalysisAction.HOLD)
    bundle = load_decision_bundle(_snapshot(decision=decision), None)
    assert bundle.plan is None
    assert bundle.action is AnalysisAction.HOLD


def test_wrong_schema_name_rejected() -> None:
    with pytest.raises(DecisionLoadError):
        load_decision_bundle(_snapshot(schema_name="allocator_decision.v1"), _plan())


def test_failed_validation_rejected() -> None:
    with pytest.raises(DecisionLoadError):
        load_decision_bundle(_snapshot(validation_result=_failed_result()), _plan())


def test_unparseable_payload_rejected() -> None:
    snap = _snapshot(raw_payload={"not": "an analysis decision"})
    with pytest.raises(DecisionLoadError):
        load_decision_bundle(snap, _plan())


def test_created_at_mismatch_rejected() -> None:
    # snapshot.created_at differs from the payload's created_at
    snap = _snapshot(created_at=NOW + timedelta(hours=1))
    with pytest.raises(DecisionLoadError):
        load_decision_bundle(snap, _plan())


def test_plan_mismatch_rejected() -> None:
    # BUY decision with a SELL plan → bundle validation fails inside the loader
    with pytest.raises(DecisionLoadError):
        load_decision_bundle(_snapshot(), _plan(action=AnalysisAction.SELL))


def test_hold_with_plan_rejected() -> None:
    decision = _decision(action=AnalysisAction.HOLD)
    with pytest.raises(DecisionLoadError):
        load_decision_bundle(_snapshot(decision=decision), _plan())
