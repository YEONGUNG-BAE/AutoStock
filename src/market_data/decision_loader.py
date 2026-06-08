"""RTM-4a — validated decision → trigger bundle loader.

DecisionSnapshot(검증·정규화된 의사결정 저장본)와 (BUY/SELL이면) TriggerPlan을 받아
TriggerEngine이 소비할 DecisionTriggerBundle을 만든다. 모든 실패는 fail-closed로
DecisionLoadError를 raise한다(잘못된 의사결정이 조용히 무장되는 일을 막는다).

network/broker/ledger/LLM 접근이 없다. snapshot은 이미 검증된 저장본이므로 여기서
LLM을 다시 호출하거나 prose에서 조건을 추론하지 않는다.
"""

from __future__ import annotations

from pydantic import ValidationError

from analysis.models import ANALYSIS_DECISION_SCHEMA, AnalysisDecision
from domain.decision import DecisionSnapshot
from market_data.trigger_engine import DecisionTriggerBundle, TriggerPlan

__all__ = ["DecisionLoadError", "load_decision_bundle"]


class DecisionLoadError(Exception):
    """검증된 의사결정을 trigger bundle로 적재하지 못했을 때 raise한다(fail-closed)."""


def load_decision_bundle(
    snapshot: DecisionSnapshot, plan: TriggerPlan | None
) -> DecisionTriggerBundle:
    """검증된 DecisionSnapshot + (BUY/SELL이면) TriggerPlan을 bundle로 적재한다.

    fail-closed: schema 불일치, 검증 실패, payload 파싱 실패, decision_id/created_at
    불일치, action↔plan 불일치 중 하나라도 있으면 DecisionLoadError를 raise한다."""
    if snapshot.schema_name != ANALYSIS_DECISION_SCHEMA:
        raise DecisionLoadError(
            f"snapshot.schema_name must be {ANALYSIS_DECISION_SCHEMA!r}, "
            f"got {snapshot.schema_name!r}."
        )
    if not snapshot.validation_result.passed:
        raise DecisionLoadError("snapshot.validation_result.passed must be True.")

    try:
        decision = AnalysisDecision.model_validate(snapshot.normalized_payload)
    except ValidationError as exc:
        raise DecisionLoadError("normalized_payload is not a valid AnalysisDecision.") from exc

    if decision.decision_id != snapshot.decision_id:
        raise DecisionLoadError("decision.decision_id must equal snapshot.decision_id.")
    if decision.created_at != snapshot.created_at:
        raise DecisionLoadError("decision.created_at must equal snapshot.created_at.")

    try:
        return DecisionTriggerBundle(decision=decision, plan=plan)
    except ValidationError as exc:
        raise DecisionLoadError("decision and plan are inconsistent.") from exc
