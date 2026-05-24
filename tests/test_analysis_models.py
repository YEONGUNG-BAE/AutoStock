from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import (
    SUMMARY_ONE_LINER_MAX_LENGTH,
    AnalysisAction,
    AnalysisDecision,
    AnalysisReason,
    AnalysisRole,
    BearPerspective,
    BullPerspective,
    ConvictionLevel,
    FundManagerDecision,
    RiskManagerEvaluation,
)
from domain import DateId, DecisionId, Percent


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def _sample_reason(**overrides: object) -> AnalysisReason:
    base = {
        "reason": "실적 둔화 우려",
        "date_id": DateId("260522-1"),
    }
    base.update(overrides)
    return AnalysisReason(**base)


def _sample_bear(**overrides: object) -> BearPerspective:
    base = {
        "summary": "하방 리스크 우세",
        "risks": ("수요 둔화",),
        "reasons": (_sample_reason(),),
    }
    base.update(overrides)
    return BearPerspective(**base)


def _sample_bull(**overrides: object) -> BullPerspective:
    base = {
        "summary": "성장 모멘텀 유지",
        "catalysts": ("신제품 출시",),
        "reasons": (_sample_reason(date_id="260522-2"),),
    }
    base.update(overrides)
    return BullPerspective(**base)


def _sample_risk_manager(**overrides: object) -> RiskManagerEvaluation:
    base = {
        "summary": "중립적 리스크 평가",
        "risk_flags": ("변동성 확대",),
        "max_weight_percent": Percent("5"),
        "reasons": (_sample_reason(date_id="260522-3"),),
    }
    base.update(overrides)
    return RiskManagerEvaluation(**base)


def _sample_fund_manager(**overrides: object) -> FundManagerDecision:
    base = {
        "action": AnalysisAction.HOLD,
        "target_weight_percent": Percent("5"),
        "rationale": "현재 비중 유지",
        "reasons": (_sample_reason(date_id="260522-4"),),
    }
    base.update(overrides)
    return FundManagerDecision(**base)


def _sample_decision(**overrides: object) -> AnalysisDecision:
    base = {
        "decision_id": DecisionId("analysis-260522-001"),
        "created_at": NOW,
        "universe": "KR_LARGE",
        "symbol": "005930",
        "market": "KR",
        "summary_one_liner": "방어적 HOLD",
        "bear": _sample_bear(),
        "bull": _sample_bull(),
        "risk_manager": _sample_risk_manager(),
        "fund_manager": _sample_fund_manager(),
        "reasons": (_sample_reason(date_id="260522-5"),),
    }
    base.update(overrides)
    return AnalysisDecision(**base)


@pytest.mark.parametrize(
    "enum_cls, value, expected",
    [
        (AnalysisAction, "buy", AnalysisAction.BUY),
        (AnalysisAction, "sell", AnalysisAction.SELL),
        (AnalysisAction, "hold", AnalysisAction.HOLD),
        (AnalysisRole, "bear", AnalysisRole.BEAR),
        (AnalysisRole, "fund_manager", AnalysisRole.FUND_MANAGER),
        (ConvictionLevel, "high", ConvictionLevel.HIGH),
    ],
)
def test_enums_accept_valid_values(enum_cls: type, value: str, expected: object) -> None:
    assert enum_cls(value) == expected


@pytest.mark.parametrize(
    "enum_cls, value",
    [
        (AnalysisAction, "BUY"),
        (AnalysisAction, "invalid"),
        (AnalysisRole, "manager"),
        (ConvictionLevel, "extreme"),
    ],
)
def test_enums_reject_invalid_values(enum_cls: type, value: str) -> None:
    with pytest.raises(ValueError):
        enum_cls(value)


def test_analysis_reason_accepts_valid_values() -> None:
    reason = _sample_reason(source_name="dart", quote="영업이익 -3%")
    assert reason.date_id.value == "260522-1"


def test_analysis_reason_rejects_blank_reason() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_reason(reason=" ")


def test_analysis_reason_rejects_invalid_date_id() -> None:
    with pytest.raises(ValidationError, match="canonical format"):
        _sample_reason(date_id="bad-id")


@pytest.mark.parametrize("field_name", ["source_name", "quote"])
def test_analysis_reason_rejects_blank_optional_strings(field_name: str) -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_reason(**{field_name: " "})


def test_bear_perspective_accepts_valid_values() -> None:
    bear = _sample_bear()
    assert bear.risks == ("수요 둔화",)


def test_bear_perspective_rejects_blank_summary() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_bear(summary=" ")


def test_bear_perspective_rejects_empty_risks() -> None:
    with pytest.raises(ValidationError, match="at least one item"):
        _sample_bear(risks=())


def test_bear_perspective_rejects_blank_risk() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_bear(risks=(" ",))


def test_bear_perspective_rejects_empty_reasons() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        _sample_bear(reasons=())


def test_bear_perspective_rejects_extra_action_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BearPerspective.model_validate(
            {
                "summary": "bear view",
                "risks": ["risk"],
                "reasons": [_sample_reason().model_dump(mode="json")],
                "action": "sell",
            }
        )


def test_bull_perspective_accepts_valid_values() -> None:
    bull = _sample_bull()
    assert bull.catalysts == ("신제품 출시",)


def test_bull_perspective_rejects_blank_summary() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_bull(summary=" ")


def test_bull_perspective_rejects_empty_catalysts() -> None:
    with pytest.raises(ValidationError, match="at least one item"):
        _sample_bull(catalysts=())


def test_bull_perspective_rejects_blank_catalyst() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_bull(catalysts=(" ",))


def test_bull_perspective_rejects_empty_reasons() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        _sample_bull(reasons=())


def test_risk_manager_accepts_empty_risk_flags() -> None:
    evaluation = _sample_risk_manager(risk_flags=())
    assert evaluation.risk_flags == ()


def test_risk_manager_rejects_blank_flag() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_risk_manager(risk_flags=(" ",))


def test_risk_manager_accepts_valid_max_weight_percent() -> None:
    evaluation = _sample_risk_manager(max_weight_percent=Percent("10"))
    assert evaluation.max_weight_percent is not None
    assert evaluation.max_weight_percent.value == Decimal("10")


def test_risk_manager_rejects_empty_reasons() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        _sample_risk_manager(reasons=())


def test_risk_manager_rejects_extra_order_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RiskManagerEvaluation.model_validate(
            {
                "summary": "risk",
                "reasons": [_sample_reason().model_dump(mode="json")],
                "riskfilter": True,
            }
        )


@pytest.mark.parametrize("action", [AnalysisAction.BUY, AnalysisAction.SELL, AnalysisAction.HOLD])
def test_fund_manager_accepts_valid_actions(action: AnalysisAction) -> None:
    decision = _sample_fund_manager(action=action)
    assert decision.action == action


def test_fund_manager_rejects_invalid_action() -> None:
    with pytest.raises(ValidationError):
        FundManagerDecision(
            action="keep",
            target_weight_percent=Percent("5"),
            rationale="invalid",
            reasons=(_sample_reason(),),
        )


def test_fund_manager_rejects_blank_rationale() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_fund_manager(rationale=" ")


def test_fund_manager_rejects_empty_reasons() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        _sample_fund_manager(reasons=())


def test_fund_manager_rejects_extra_order_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FundManagerDecision.model_validate(
            {
                "action": "hold",
                "target_weight_percent": "5",
                "rationale": "hold",
                "reasons": [_sample_reason().model_dump(mode="json")],
                "quantity": 100,
            }
        )


def test_fund_manager_target_weight_percent_validation() -> None:
    with pytest.raises(ValidationError, match="between 0 and 100"):
        FundManagerDecision(
            action=AnalysisAction.HOLD,
            target_weight_percent="101",
            rationale="too high",
            reasons=(_sample_reason(),),
        )


def test_analysis_decision_accepts_valid_values() -> None:
    decision = _sample_decision()
    assert decision.schema_name == "analysis_decision.v1"
    assert decision.fund_manager.action == AnalysisAction.HOLD


def test_analysis_decision_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        _sample_decision(created_at=NAIVE_NOW)


@pytest.mark.parametrize("field_name", ["universe", "symbol", "market"])
def test_analysis_decision_rejects_blank_identity_fields(field_name: str) -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_decision(**{field_name: " "})


def test_analysis_decision_rejects_invalid_schema_name() -> None:
    with pytest.raises(ValidationError, match="analysis_decision.v1"):
        _sample_decision(schema_name="analysis.v0")


def test_analysis_decision_rejects_too_long_summary_one_liner() -> None:
    too_long = "x" * (SUMMARY_ONE_LINER_MAX_LENGTH + 1)
    with pytest.raises(ValidationError, match=f"at most {SUMMARY_ONE_LINER_MAX_LENGTH}"):
        _sample_decision(summary_one_liner=too_long)


def test_analysis_decision_rejects_empty_top_level_reasons() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        _sample_decision(reasons=())


def test_analysis_decision_rejects_invalid_metadata() -> None:
    with pytest.raises(ValidationError, match="float values are not allowed"):
        _sample_decision(metadata={"bad": 1.5})


def test_analysis_decision_rejects_extra_order_intent_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AnalysisDecision.model_validate(
            {
                **_sample_decision().model_dump(mode="json"),
                "order_intent": {"side": "BUY"},
            }
        )


def test_analysis_decision_rejects_top_level_action_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AnalysisDecision.model_validate(
            {
                **_sample_decision().model_dump(mode="json"),
                "action": "buy",
            }
        )


def test_analysis_decision_to_canonical_dict_is_deterministic() -> None:
    decision = _sample_decision(metadata={"z": "3", "a": "1"})
    canonical = decision.to_canonical_dict()
    assert canonical["metadata"] == {"a": "1", "z": "3"}
