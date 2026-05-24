from __future__ import annotations

from decimal import Decimal

from allocator.models import GoldPolicyMode, TargetWeights
from domain.identifiers import Percent

# cash target band — 전체 계좌 기준 (Phase 8 hard validator)
CASH_TARGET_MIN = Decimal("10")
CASH_TARGET_MAX = Decimal("30")

# gold band — 운용 자산 기준 target_weights.gold
GOLD_NORMAL_MIN = Decimal("18")
GOLD_NORMAL_MAX = Decimal("22")
GOLD_EXCEPTION_MIN = Decimal("15")
GOLD_EXCEPTION_MAX = Decimal("25")

TARGET_WEIGHTS_REQUIRED_SUM = Decimal("100")


def target_weights_sum(weights: TargetWeights) -> Decimal:
    """KR/US/GOLD target weights 합계를 Decimal로 반환한다."""
    return weights.kr.value + weights.us.value + weights.gold.value


def is_target_weights_sum_valid(weights: TargetWeights) -> bool:
    """운용 자산 기준 target weights 합계가 정확히 100인지 확인한다."""
    return target_weights_sum(weights) == TARGET_WEIGHTS_REQUIRED_SUM


def is_cash_target_in_band(cash_target_percent: Percent) -> bool:
    """cash_target_percent가 전체 계좌 기준 10~30 band 내인지 확인한다."""
    value = cash_target_percent.value
    return CASH_TARGET_MIN <= value <= CASH_TARGET_MAX


def is_gold_in_band(gold: Percent, mode: GoldPolicyMode) -> bool:
    """gold_policy_mode에 따른 gold target band를 확인한다."""
    value = gold.value
    if mode == GoldPolicyMode.NORMAL:
        return GOLD_NORMAL_MIN <= value <= GOLD_NORMAL_MAX
    return GOLD_EXCEPTION_MIN <= value <= GOLD_EXCEPTION_MAX


def target_weights_equal(left: TargetWeights, right: TargetWeights) -> bool:
    """두 TargetWeights가 Decimal 기준으로 동일한지 확인한다."""
    return (
        left.kr.value == right.kr.value
        and left.us.value == right.us.value
        and left.gold.value == right.gold.value
    )


def percent_equal(left: Percent, right: Percent) -> bool:
    """두 Percent 값이 Decimal 기준으로 동일한지 확인한다."""
    return left.value == right.value
