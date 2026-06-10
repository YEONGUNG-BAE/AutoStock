"""RTM-4a/4b.2 — deterministic intraday trigger conditions.

latest 지표(price/spread)는 RTM-2 LatestMarketStateSnapshot의 최신 단일 trade/quote로
계산한다. RTM-4b.2에서 rolling 지표(SMA/RETURN_BPS/ROLLING_VOLUME/VWAP)를 추가했다.
rolling 지표는 history를 직접 쌓지 않고, 미리 evaluate된 불변 IndicatorContext(window
snapshot 묶음)를 주입받아 READY window에서만 값을 계산한다.

조건 조합은 v1에서 ALL(모든 clause 참)만 지원한다. ANY/NOT/중첩 group은 후속 확장.
network/broker/ledger/LLM 접근이 없고, 평가는 순수 함수다.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from typing_extensions import Self

from market_data.indicators import (
    IndicatorContext,
    IndicatorKind,
    IndicatorNotReadyError,
    IndicatorWindowSpec,
    compute_indicator,
)
from market_data.latest_state import LatestMarketStateSnapshot

__all__ = [
    "Metric",
    "Comparator",
    "ConditionClause",
    "metric_value",
    "evaluate_clause",
    "evaluate_all",
    "rule_required_slots",
    "rule_required_windows",
]


class Metric(StrEnum):
    """지원 지표. latest 지표는 단일 snapshot으로, rolling 지표는 IndicatorContext로 계산한다."""

    # latest (RTM-4a): 최신 단일 snapshot으로 계산.
    LAST_TRADE_PRICE = "last_trade_price"
    BEST_BID_PRICE = "best_bid_price"
    BEST_ASK_PRICE = "best_ask_price"
    SPREAD_BPS = "spread_bps"
    # rolling (RTM-4b.2): IndicatorContext의 READY window로 계산. window 필수.
    SMA_PRICE = "sma_price"
    RETURN_BPS = "return_bps"
    ROLLING_VOLUME = "rolling_volume"
    VWAP = "vwap"


class Comparator(StrEnum):
    LTE = "lte"
    GTE = "gte"


_QUOTE_METRICS = frozenset(
    {Metric.BEST_BID_PRICE, Metric.BEST_ASK_PRICE, Metric.SPREAD_BPS}
)
# rolling metric → 대응하는 pure indicator kind. 이름이 IndicatorKind 값과 정합한다.
_ROLLING_METRICS: dict[Metric, IndicatorKind] = {
    Metric.SMA_PRICE: IndicatorKind.SMA_PRICE,
    Metric.RETURN_BPS: IndicatorKind.RETURN_BPS,
    Metric.ROLLING_VOLUME: IndicatorKind.ROLLING_VOLUME,
    Metric.VWAP: IndicatorKind.VWAP,
}
# 양수만 의미 있는 가격성 지표.
_POSITIVE_METRICS = frozenset(
    {
        Metric.LAST_TRADE_PRICE,
        Metric.BEST_BID_PRICE,
        Metric.BEST_ASK_PRICE,
        Metric.SMA_PRICE,
        Metric.VWAP,
    }
)
# 0 이상이면 되는 지표(스프레드/누적 거래량).
_NON_NEGATIVE_METRICS = frozenset({Metric.SPREAD_BPS, Metric.ROLLING_VOLUME})
# RETURN_BPS만 음수/0/양수 모두 허용(단 finite).
_BPS = Decimal("10000")


def _is_rolling(metric: Metric) -> bool:
    return metric in _ROLLING_METRICS


class ConditionClause(BaseModel):
    """단일 조건 절. metric을 comparator로 threshold와 비교한다.

    rolling metric은 IndicatorWindowSpec(`window`)을 반드시 동반하고, latest metric은
    절대 동반하지 않는다(extra=forbid + 명시 검증)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: Metric
    comparator: Comparator
    threshold: Decimal
    window: IndicatorWindowSpec | None = None

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
        if not value.is_finite():
            raise ValueError("threshold must be a finite Decimal.")
        metric = info.data.get("metric")  # type: ignore[attr-defined]
        if metric in _POSITIVE_METRICS:
            if value <= 0:
                raise ValueError(f"{metric.value} threshold must be > 0.")
        elif metric in _NON_NEGATIVE_METRICS:
            if value < 0:
                raise ValueError(f"{metric.value} threshold must be >= 0.")
        # RETURN_BPS: 음수/0/양수 모두 허용(finite는 위에서 보장).
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> Self:
        if _is_rolling(self.metric):
            if self.window is None:
                raise ValueError(f"{self.metric.value} requires a window spec.")
        elif self.window is not None:
            raise ValueError(f"{self.metric.value} must not carry a window spec.")
        return self


def metric_value(
    metric: Metric,
    snapshot: LatestMarketStateSnapshot,
    *,
    indicators: IndicatorContext | None = None,
    window: IndicatorWindowSpec | None = None,
) -> Decimal | None:
    """metric 값을 계산한다. 필요한 입력이 없거나 READY가 아니면 None을 반환한다
    (freshness/readiness/coherence 검사는 호출자=엔진이 SUPPRESS로 처리한다).

    latest metric은 snapshot만 사용하고 indicators/window를 무시한다(후방호환). rolling
    metric은 IndicatorContext에서 window_id로 snapshot을 찾아 READY일 때만 계산한다."""
    if _is_rolling(metric):
        if indicators is None or window is None:
            return None
        win = indicators.get(window.window_id)
        if win is None or not win.is_ready:
            return None
        try:
            return compute_indicator(_ROLLING_METRICS[metric], win)
        except IndicatorNotReadyError:
            # 엔진이 readiness를 먼저 gate하지만, 방어적으로 fail-closed.
            return None
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


def evaluate_clause(
    clause: ConditionClause,
    snapshot: LatestMarketStateSnapshot,
    *,
    indicators: IndicatorContext | None = None,
) -> bool:
    """단일 clause 평가. value가 없으면 False(조건 미충족)로 본다.

    엔진은 이 호출 전에 freshness/readiness/coherence를 이미 검증하므로 정상 경로에서
    value는 not None이지만, 방어적으로 None이면 미충족 처리해 fail-closed한다."""
    value = metric_value(
        clause.metric, snapshot, indicators=indicators, window=clause.window
    )
    if value is None:
        return False
    if clause.comparator is Comparator.LTE:
        return value <= clause.threshold
    return value >= clause.threshold


def evaluate_all(
    rules: tuple[ConditionClause, ...],
    snapshot: LatestMarketStateSnapshot,
    *,
    indicators: IndicatorContext | None = None,
) -> bool:
    """ALL 조합: 모든 clause가 참이어야 True. 빈 rules는 False(엔진이 빈 plan을 거부하므로
    정상 경로에서는 도달하지 않는다)."""
    if not rules:
        return False
    return all(evaluate_clause(clause, snapshot, indicators=indicators) for clause in rules)


def rule_required_slots(rules: tuple[ConditionClause, ...]) -> tuple[bool, bool]:
    """rules가 요구하는 (needs_trade, needs_quote)를 계산한다. 엔진은 여기에 더해
    reference price를 위해 needs_quote를 항상 강제한다.

    rolling metric은 최신 trade와의 coherence(stream/sequence/시각 일치)를 검사해야 하므로
    needs_trade를 강제한다(엔진이 최신 trade slot 신선도를 먼저 gate하게 함)."""
    needs_trade = any(
        r.metric is Metric.LAST_TRADE_PRICE or _is_rolling(r.metric) for r in rules
    )
    needs_quote = any(r.metric in _QUOTE_METRICS for r in rules)
    return needs_trade, needs_quote


def rule_required_windows(
    rules: tuple[ConditionClause, ...],
) -> tuple[IndicatorWindowSpec, ...]:
    """rules가 요구하는 고유 IndicatorWindowSpec을 window_id 기준으로 중복 제거해
    결정론적(window_id 정렬) 순서로 반환한다. price-only rule이면 빈 tuple."""
    by_id: dict[str, IndicatorWindowSpec] = {}
    for r in rules:
        if r.window is not None:
            by_id[r.window.window_id] = r.window
    return tuple(by_id[k] for k in sorted(by_id))
