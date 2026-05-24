from __future__ import annotations

from decimal import Decimal

from domain.identifiers import Percent


def is_within_allocator_tolerance(
    target_weight: Percent,
    allocator_target_weight: Percent,
    tolerance_percent: Percent,
) -> bool:
    """fund_manager.target_weight_percent가 allocator target ± tolerance band 내인지 확인한다."""
    lower = allocator_target_weight.value - tolerance_percent.value
    upper = allocator_target_weight.value + tolerance_percent.value
    return lower <= target_weight.value <= upper


def tolerance_band(
    allocator_target_weight: Percent,
    tolerance_percent: Percent,
) -> tuple[Decimal, Decimal]:
    """allocator tolerance band의 하한/상한 Decimal 값을 반환한다."""
    lower = allocator_target_weight.value - tolerance_percent.value
    upper = allocator_target_weight.value + tolerance_percent.value
    return lower, upper
