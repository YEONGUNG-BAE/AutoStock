from __future__ import annotations

from decimal import Decimal

from allocator.rules import CASH_TARGET_MAX, CASH_TARGET_MIN
from domain.enums import Market
from domain.identifiers import Percent
from domain.money import Money
from risk.models import AssetClassWeights, RiskMode

# 단일 종목 누적 매수 원금 cap — 전체 NAV 기준 5%
SINGLE_POSITION_CAP_PERCENT = Decimal("5")

# 운용 비중 band — NORMAL 모드 production target
INVESTED_MIN_NORMAL_PERCENT = Decimal("70")
INVESTED_MAX_PERCENT = Decimal("90")
PAPER_OBSERVATION_MIN_ALLOWED = Decimal("50")
PAPER_OBSERVATION_MAX_ALLOWED = Decimal("70")

# 자산군 soft band — WARNING only (Phase 10 foundation)
ASSET_CLASS_SOFT_BAND_MIN = Decimal("15")
ASSET_CLASS_SOFT_BAND_MAX = Decimal("55")

# directional slippage tolerance
SLIPPAGE_TOLERANCE_KR = Decimal("0.5")
SLIPPAGE_TOLERANCE_US = Decimal("0.2")

# MDD killswitch threshold — drawdown magnitude (양수 %, peak 대비 하락폭)
MDD_LEVEL_1_MAGNITUDE = Decimal("10")
MDD_LEVEL_2_MAGNITUDE = Decimal("15")
MDD_LEVEL_3_MAGNITUDE = Decimal("20")

# gold trade frequency limits
GOLD_TRADES_MONTHLY_MAX = 2
GOLD_TRADES_QUARTERLY_MAX = 4

RISK_FILTER_SCHEMA = "risk_filter.v1"
RISK_FILTER_VALIDATOR_VERSION = "phase10"


def slippage_tolerance_percent(market: Market) -> Decimal:
    """시장별 directional slippage tolerance(%)를 반환한다."""
    if market == Market.KR:
        return SLIPPAGE_TOLERANCE_KR
    return SLIPPAGE_TOLERANCE_US


def invested_lower_bound_percent(
    mode: RiskMode,
    paper_observation_min: Percent | None,
) -> Decimal:
    """모드와 paper observation 설정에 따른 invested 하한(%)을 반환한다."""
    if paper_observation_min is not None:
        return paper_observation_min.value
    return INVESTED_MIN_NORMAL_PERCENT


def percent_of_money(numerator: Money, denominator: Money) -> Decimal:
    """numerator / denominator * 100 을 Decimal로 반환한다."""
    if denominator.amount == 0:
        return Decimal("0")
    return (numerator.amount / denominator.amount) * Decimal("100")


def money_cap_from_nav_percent(total_nav: Money, cap_percent: Decimal) -> Money:
    """total_nav * cap_percent / 100 Money cap을 반환한다."""
    cap_amount = (total_nav.amount * cap_percent) / Decimal("100")
    return Money(amount=cap_amount, currency=total_nav.currency)


def proposed_target_market_value(total_nav: Money, target_weight_percent: Percent) -> Money:
    """전체 NAV 기준 target_weight_percent에 해당하는 목표 시장가치를 반환한다."""
    amount = (total_nav.amount * target_weight_percent.value) / Decimal("100")
    return Money(amount=amount, currency=total_nav.currency)


def additional_buy_cost(
    total_nav: Money,
    target_weight_percent: Percent,
    current_symbol_market_value: Money,
) -> Money:
    """target weight 증가에 필요한 추가 매수 원금 추정치를 반환한다."""
    proposed = proposed_target_market_value(total_nav, target_weight_percent)
    delta = proposed.amount - current_symbol_market_value.amount
    if delta <= 0:
        return Money.zero(total_nav.currency)
    return Money(amount=delta, currency=total_nav.currency)


def asset_class_soft_band_violations(weights: AssetClassWeights) -> tuple[str, ...]:
    """15~55 soft band를 벗어난 자산군 이름 tuple을 반환한다."""
    violations: list[str] = []
    for name, percent in (
        ("kr", weights.kr),
        ("us", weights.us),
        ("gold", weights.gold),
    ):
        value = percent.value
        if value < ASSET_CLASS_SOFT_BAND_MIN or value > ASSET_CLASS_SOFT_BAND_MAX:
            violations.append(name)
    return tuple(violations)


def is_cash_target_in_band_value(cash_target_percent: Decimal) -> bool:
    """cash target percent가 10~30 band 내인지 확인한다."""
    return CASH_TARGET_MIN <= cash_target_percent <= CASH_TARGET_MAX


def mdd_level_from_percent(mdd_percent: Percent | None) -> int | None:
    """MDD drawdown magnitude(%)에 해당하는 killswitch level(1~3)을 반환한다."""
    if mdd_percent is None:
        return None
    value = mdd_percent.value
    if value >= MDD_LEVEL_3_MAGNITUDE:
        return 3
    if value >= MDD_LEVEL_2_MAGNITUDE:
        return 2
    if value >= MDD_LEVEL_1_MAGNITUDE:
        return 1
    return None
