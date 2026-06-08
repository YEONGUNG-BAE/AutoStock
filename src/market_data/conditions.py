"""RTM-4a — deterministic intraday trigger conditions over the latest snapshot.

이 모듈은 RTM-2 LatestMarketStateSnapshot의 최신 trade/quote 단일 스냅샷만으로
계산 가능한 price/spread 조건만 지원한다. rolling window/이동평균/거래량 급증 등
과거 누적이 필요한 지표는 RTM-4b로 유보한다(여기서 history를 쌓지 않는다).

조건 조합은 v1에서 ALL(모든 clause 참)만 지원한다. ANY/NOT/중첩 group은 후속 확장.
network/broker/ledger/LLM 접근이 없고, 평가는 순수 함수다.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from market_data.latest_state import LatestMarketStateSnapshot

__all__ = [
    "Metric",
    "Comparator",
    "ConditionClause",
    "metric_value",
    "evaluate_clause",
    "evaluate_all",
    "rule_required_slots",
]


class Metric(StrEnum):
    """RTM-4a v1이 지원하는 지표. 모두 최신 단일 snapshot으로 계산 가능하다."""

    LAST_TRADE_PRICE = "last_trade_price"
    BEST_BID_PRICE = "best_bid_price"
    BEST_ASK_PRICE = "best_ask_price"
    SPREAD_BPS = "spread_bps"


class Comparator(StrEnum):
    LTE = "lte"
    GTE = "gte"


_QUOTE_METRICS = frozenset(
    {Metric.BEST_BID_PRICE, Metric.BEST_ASK_PRICE, Metric.SPREAD_BPS}
)
_BPS = Decimal("10000")


class ConditionClause(BaseModel):
    """단일 조건 절. metric을 comparator로 threshold와 비교한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: Metric
    comparator: Comparator
    threshold: Decimal

    @field_validator("threshold", mode="before")
    @classmethod
    def _coerce_threshold(cls, value: object) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (str, int)):
            return Decimal(value)
        raise ValueError("threshold must be a Decimal, str, or int.")

    @field_validator("threshold", mode="after")
    @classmethod
    def _validate_threshold(cls, value: Decimal, info: object) -> Decimal:
        # spread_bps는 0 이상, 가격 지표는 0 초과만 의미가 있다.
        metric = info.data.get("metric")  # type: ignore[attr-defined]
        if metric is Metric.SPREAD_BPS:
            if value < 0:
                raise ValueError("spread_bps threshold must be >= 0.")
        elif value <= 0:
            raise ValueError("price threshold must be > 0.")
        return value


def metric_value(metric: Metric, snapshot: LatestMarketStateSnapshot) -> Decimal | None:
    """snapshot에서 metric 값을 계산한다. 필요한 slot이 없으면 None을 반환한다
    (freshness/존재 검사는 호출자=엔진이 SUPPRESS로 처리한다)."""
    if metric is Metric.LAST_TRADE_PRICE:
        return snapshot.trade.price if snapshot.trade is not None else None
    if metric is Metric.BEST_BID_PRICE:
        return snapshot.quote.bid_price if snapshot.quote is not None else None
    if metric is Metric.BEST_ASK_PRICE:
        return snapshot.quote.ask_price if snapshot.quote is not None else None
    # SPREAD_BPS
    if snapshot.quote is None:
        return None
    bid = snapshot.quote.bid_price
    ask = snapshot.quote.ask_price
    mid = (bid + ask) / Decimal(2)
    return (ask - bid) / mid * _BPS


def evaluate_clause(clause: ConditionClause, snapshot: LatestMarketStateSnapshot) -> bool:
    """단일 clause 평가. value가 없으면 False(조건 미충족)로 본다.

    엔진은 이 호출 전에 freshness/존재를 이미 검증하므로 정상 경로에서 value는 not None
    이지만, 방어적으로 None이면 미충족 처리해 fail-closed한다."""
    value = metric_value(clause.metric, snapshot)
    if value is None:
        return False
    if clause.comparator is Comparator.LTE:
        return value <= clause.threshold
    return value >= clause.threshold


def evaluate_all(
    rules: tuple[ConditionClause, ...], snapshot: LatestMarketStateSnapshot
) -> bool:
    """ALL 조합: 모든 clause가 참이어야 True. 빈 rules는 False(엔진이 빈 plan을 거부하므로
    정상 경로에서는 도달하지 않는다)."""
    if not rules:
        return False
    return all(evaluate_clause(clause, snapshot) for clause in rules)


def rule_required_slots(rules: tuple[ConditionClause, ...]) -> tuple[bool, bool]:
    """rules가 요구하는 (needs_trade, needs_quote)를 계산한다. 엔진은 여기에 더해
    reference price를 위해 needs_quote를 항상 강제한다."""
    needs_trade = any(r.metric is Metric.LAST_TRADE_PRICE for r in rules)
    needs_quote = any(r.metric in _QUOTE_METRICS for r in rules)
    return needs_trade, needs_quote
