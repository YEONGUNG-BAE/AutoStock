from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from allocator import (
    SUMMARY_ONE_LINER_MAX_LENGTH,
    AllocationRegime,
    AllocatorAction,
    AllocatorDecision,
    AllocatorReason,
    AssetAllocatorView,
    AssetBucket,
    CashManagerView,
    CashPolicy,
    ConsistencyCheckerView,
    GoldPolicyMode,
    SignalSummary,
    TargetWeights,
)
from domain import DateId, DecisionId, Percent


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def _sample_reason(**overrides: object) -> AllocatorReason:
    base = {
        "reason": "VIX 상승으로 방어적 현금 비중 유지",
        "date_id": DateId("260522-1"),
    }
    base.update(overrides)
    return AllocatorReason(**base)


def _sample_weights(**overrides: object) -> TargetWeights:
    base = {"kr": Percent("50"), "us": Percent("30"), "gold": Percent("20")}
    base.update(overrides)
    return TargetWeights(**base)


def _sample_decision(**overrides: object) -> AllocatorDecision:
    weights = _sample_weights()
    cash = Percent("20")
    reasons = (_sample_reason(),)
    base = {
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


@pytest.mark.parametrize(
    "enum_cls, value, expected",
    [
        (AssetBucket, "kr", AssetBucket.KR),
        (AssetBucket, "us", AssetBucket.US),
        (AssetBucket, "gold", AssetBucket.GOLD),
        (AllocatorAction, "keep", AllocatorAction.KEEP),
        (AllocatorAction, "rebalance", AllocatorAction.REBALANCE),
        (GoldPolicyMode, "normal", GoldPolicyMode.NORMAL),
        (GoldPolicyMode, "exception", GoldPolicyMode.EXCEPTION),
        (AllocationRegime, "defensive", AllocationRegime.DEFENSIVE),
    ],
)
def test_enums_accept_valid_values(enum_cls: type, value: str, expected: object) -> None:
    assert enum_cls(value) == expected


@pytest.mark.parametrize(
    "enum_cls, value",
    [
        (AssetBucket, "invalid"),
        (AllocatorAction, "BUY"),
        (GoldPolicyMode, "strict"),
    ],
)
def test_enums_reject_invalid_values(enum_cls: type, value: str) -> None:
    with pytest.raises(ValueError):
        enum_cls(value)


def test_allocator_reason_accepts_valid_values() -> None:
    reason = _sample_reason(source_name="fred", quote="VIX 18.2")
    assert reason.date_id.value == "260522-1"


def test_allocator_reason_rejects_blank_reason() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_reason(reason=" ")


def test_allocator_reason_rejects_invalid_date_id() -> None:
    with pytest.raises(ValidationError, match="canonical format"):
        _sample_reason(date_id="bad-id")


@pytest.mark.parametrize("field_name", ["source_name", "quote"])
def test_allocator_reason_rejects_blank_optional_strings(field_name: str) -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_reason(**{field_name: " "})


def test_cash_policy_accepts_valid_20_percent() -> None:
    policy = CashPolicy(
        cash_target_percent=Percent("20"),
        rationale="유동성 확보",
        reasons=(_sample_reason(),),
    )
    assert policy.cash_target_percent.value == Decimal("20")


def test_cash_policy_rejects_min_greater_than_max() -> None:
    with pytest.raises(ValidationError, match="min_cash_percent must be <= max_cash_percent"):
        CashPolicy(
            cash_target_percent=Percent("20"),
            min_cash_percent=Percent("25"),
            max_cash_percent=Percent("15"),
            rationale="invalid bounds",
            reasons=(_sample_reason(),),
        )


def test_cash_policy_rejects_target_outside_explicit_min_max() -> None:
    with pytest.raises(ValidationError, match="cash_target_percent must be >= min_cash_percent"):
        CashPolicy(
            cash_target_percent=Percent("15"),
            min_cash_percent=Percent("20"),
            max_cash_percent=Percent("30"),
            rationale="target too low",
            reasons=(_sample_reason(),),
        )


def test_cash_policy_rejects_empty_reasons() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        CashPolicy(
            cash_target_percent=Percent("20"),
            rationale="no reasons",
            reasons=(),
        )


def test_target_weights_accepts_sum_100() -> None:
    weights = _sample_weights(kr=Percent("50"), us=Percent("30"), gold=Percent("20"))
    assert weights.kr.value + weights.us.value + weights.gold.value == Decimal("100")


def test_signal_summary_rejects_blank_summary() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        SignalSummary(summary=" ", reasons=(_sample_reason(),))


def test_signal_summary_rejects_empty_reasons() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        SignalSummary(summary="summary", reasons=())


def test_consistency_checker_rejects_blank_issue() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        ConsistencyCheckerView(
            passed=False,
            summary="failed",
            issues=(" ",),
            reasons=(_sample_reason(),),
        )


def test_allocator_decision_accepts_valid_values() -> None:
    decision = _sample_decision()
    assert decision.schema_name == "allocator_decision.v1"
    assert decision.target_weights.gold.value == Decimal("20")


def test_allocator_decision_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        _sample_decision(created_at=NAIVE_NOW)


def test_allocator_decision_rejects_blank_universe() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_decision(universe=" ")


def test_allocator_decision_rejects_invalid_schema_name() -> None:
    with pytest.raises(ValidationError, match="allocator_decision.v1"):
        _sample_decision(schema_name="allocator.v0")


def test_allocator_decision_rejects_too_long_summary_one_liner() -> None:
    too_long = "x" * (SUMMARY_ONE_LINER_MAX_LENGTH + 1)
    with pytest.raises(ValidationError, match=f"at most {SUMMARY_ONE_LINER_MAX_LENGTH}"):
        _sample_decision(summary_one_liner=too_long)


def test_allocator_decision_rejects_empty_top_level_reasons() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        _sample_decision(reasons=())


def test_allocator_decision_rejects_invalid_metadata() -> None:
    with pytest.raises(ValidationError, match="float values are not allowed"):
        _sample_decision(metadata={"bad": 1.5})


def test_allocator_decision_rejects_extra_order_intent_field() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AllocatorDecision.model_validate(
            {
                **_sample_decision().model_dump(mode="json"),
                "order_intent": {"side": "BUY"},
            }
        )


def test_allocator_decision_to_canonical_dict_is_deterministic() -> None:
    decision = _sample_decision(metadata={"z": "3", "a": "1"})
    canonical = decision.to_canonical_dict()
    assert canonical["metadata"] == {"a": "1", "z": "3"}
