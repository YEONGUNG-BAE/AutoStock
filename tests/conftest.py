from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import (
    AnalysisAction,
    AnalysisDecision,
    AnalysisReason,
    BearPerspective,
    BullPerspective,
    FundManagerDecision,
    RiskManagerEvaluation,
)
from allocator import (
    AllocatorDecision,
    AllocatorReason,
    AssetAllocatorView,
    CashManagerView,
    CashPolicy,
    ConsistencyCheckerView,
    GoldPolicyMode,
    SignalSummary,
    TargetWeights,
)
from domain import Currency, DateId, DecisionId, Money, Percent
from risk import RiskFilterContext, RiskFilterInput, RiskMode


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
KRW = Currency.KRW


def _analysis_reason(**overrides: object) -> AnalysisReason:
    base = {"reason": "실적 둔화 우려", "date_id": DateId("260522-1")}
    base.update(overrides)
    return AnalysisReason(**base)


def _allocator_reason(**overrides: object) -> AllocatorReason:
    base = {"reason": "VIX 상승으로 방어적 현금 비중 유지", "date_id": DateId("260522-1")}
    base.update(overrides)
    return AllocatorReason(**base)


def _sample_allocator_decision(**overrides: object) -> AllocatorDecision:
    weights = TargetWeights(kr=Percent("50"), us=Percent("30"), gold=Percent("20"))
    cash = Percent("20")
    reasons = (_allocator_reason(),)
    base: dict[str, Any] = {
        "decision_id": DecisionId("allocator-260522-001"),
        "created_at": NOW,
        "universe": "ALL",
        "summary_one_liner": "균형 배분 유지",
        "gold_policy_mode": GoldPolicyMode.NORMAL,
        "signal_summary": SignalSummary(summary="시장 신호 요약", reasons=reasons),
        "cash_manager": CashManagerView(
            summary="현금 20% 유지",
            recommended_cash_percent=cash,
            reasons=reasons,
        ),
        "asset_allocator": AssetAllocatorView(
            summary="KR/US/Gold 균형",
            target_weights=weights,
            reasons=reasons,
        ),
        "consistency_checker": ConsistencyCheckerView(
            passed=True,
            summary="일관성 확인 완료",
            reasons=reasons,
        ),
        "cash_policy": CashPolicy(
            cash_target_percent=cash,
            rationale="유동성 확보",
            reasons=reasons,
        ),
        "target_weights": weights,
        "reasons": reasons,
    }
    base.update(overrides)
    return AllocatorDecision(**base)


def _sample_analysis_decision(**overrides: object) -> AnalysisDecision:
    reasons = (_analysis_reason(),)
    base: dict[str, Any] = {
        "decision_id": DecisionId("analysis-260522-001"),
        "created_at": NOW,
        "universe": "KR_LARGE",
        "symbol": "005930",
        "market": "KR",
        "summary_one_liner": "방어적 HOLD",
        "bear": BearPerspective(
            summary="하방 리스크",
            risks=("수요 둔화",),
            reasons=reasons,
        ),
        "bull": BullPerspective(
            summary="성장 모멘텀",
            catalysts=("신제품",),
            reasons=(_analysis_reason(date_id="260522-2"),),
        ),
        "risk_manager": RiskManagerEvaluation(
            summary="중립 평가",
            reasons=(_analysis_reason(date_id="260522-3"),),
        ),
        "fund_manager": FundManagerDecision(
            action=AnalysisAction.HOLD,
            target_weight_percent=Percent("5"),
            rationale="유지",
            reasons=(_analysis_reason(date_id="260522-4"),),
        ),
        "reasons": (_analysis_reason(date_id="260522-5"),),
    }
    base.update(overrides)
    return AnalysisDecision(**base)


def _sample_risk_context(**overrides: object) -> RiskFilterContext:
    nav = Money.from_str("100000000", KRW)
    base: dict[str, Any] = {
        "created_at": NOW,
        "mode": RiskMode.NORMAL,
        "total_nav": nav,
        "cash": Money.from_str("20000000", KRW),
        "invested_amount": Money.from_str("80000000", KRW),
    }
    base.update(overrides)
    return RiskFilterContext(**base)


@pytest.fixture
def sample_risk_input_factory():
    """RiskFilter/OrderIntent 테스트용 RiskFilterInput factory."""

    def _factory(
        *,
        action: AnalysisAction = AnalysisAction.HOLD,
        target_weight_percent: Percent | None = None,
        symbol: str = "005930",
        market: str = "KR",
        context_overrides: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        allocator_overrides: dict[str, Any] | None = None,
        analysis_overrides: dict[str, Any] | None = None,
    ) -> RiskFilterInput:
        fm_kwargs: dict[str, Any] = {"action": action}
        if target_weight_percent is not None:
            fm_kwargs["target_weight_percent"] = target_weight_percent

        analysis_kwargs: dict[str, Any] = {
            "symbol": symbol,
            "market": market,
        }
        if analysis_overrides:
            analysis_kwargs.update(analysis_overrides)

        analysis = _sample_analysis_decision(**analysis_kwargs)
        # fund_manager action/weight 갱신
        fm = analysis.fund_manager.model_copy(update=fm_kwargs)
        analysis = analysis.model_copy(update={"fund_manager": fm})

        allocator_kwargs = allocator_overrides or {}
        allocator = _sample_allocator_decision(**allocator_kwargs)

        ctx_kwargs = context_overrides or {}
        context = _sample_risk_context(**ctx_kwargs)

        return RiskFilterInput(
            allocator_decision=allocator,
            analysis_decision=analysis,
            context=context,
            correlation_id=correlation_id,
        )

    return _factory
