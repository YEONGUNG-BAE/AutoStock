"""RTM-4b.2 — rolling-metric conditions + IndicatorContext (network/broker-free).

ConditionClause window 계약, per-metric threshold, rolling metric_value(READY window
에서만 계산), rule_required_windows/slots, IndicatorContext 불변·식별·정렬·build helper를
fixture만으로 검증한다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from domain.enums import Market
from market_data.conditions import (
    Comparator,
    ConditionClause,
    Metric,
    evaluate_all,
    evaluate_clause,
    metric_value,
    rule_required_slots,
    rule_required_windows,
)
from market_data.indicators import (
    IndicatorContext,
    IndicatorReadiness,
    IndicatorWindowSpec,
    build_indicator_context,
    evaluate_window,
)
from market_data.latest_state import LatestMarketStateSnapshot
from market_data.rolling_window import (
    EpochStartReason,
    RollingRetentionPolicy,
    TradeHistorySnapshot,
    TradeSample,
)

_BASE = datetime(2026, 6, 10, 0, 0, 0, tzinfo=UTC)


def _sample(*, seq: int, price: str, qty: str, offset: int) -> TradeSample:
    t = _BASE + timedelta(seconds=offset)
    return TradeSample(
        price=Decimal(price),
        quantity=Decimal(qty),
        trade_at=t,
        received_at=t,
        sequence=seq,
    )


def _retention() -> RollingRetentionPolicy:
    return RollingRetentionPolicy(hard_max_events=1000, hard_max_age_seconds=Decimal("86400"))


def _history(
    samples: tuple[TradeSample, ...],
    *,
    provider: str = "kis",
    channel: str = "H0STCNT0|005930",
    market: Market = Market.KR,
    symbol: str = "005930",
) -> TradeHistorySnapshot:
    latest = samples[-1]
    oldest = samples[0]
    return TradeHistorySnapshot(
        market=market,
        symbol=symbol,
        samples=samples,
        retention=_retention(),
        provider=provider,
        channel=channel,
        was_ever_observed=True,
        continuity_epoch=1,
        epoch_start_reason=EpochStartReason.INITIAL,
        latest_sequence=latest.sequence,
        latest_event_time=latest.trade_at,
        latest_received_at=latest.received_at,
        oldest_event_time=oldest.trade_at,
        evicted_event_count=0,
        evicted_through_event_time=None,
        retention_truncated=False,
    )


def _spec(**over: object) -> IndicatorWindowSpec:
    base: dict[str, object] = {
        "lookback_events": 3,
        "min_events": 2,
        "freshness_max_age_seconds": Decimal("3600"),
    }
    base.update(over)
    return IndicatorWindowSpec(**base)  # type: ignore[arg-type]


def _ctx(
    samples: tuple[TradeSample, ...],
    specs: tuple[IndicatorWindowSpec, ...],
    *,
    now: datetime | None = None,
    **hist: object,
) -> IndicatorContext:
    now = now or (samples[-1].trade_at + timedelta(seconds=1))
    return build_indicator_context(_history(samples, **hist), specs, now=now)  # type: ignore[arg-type]


def _snap(
    *, trade: object = None, quote: object = None, market: Market = Market.KR, symbol: str = "005930"
) -> LatestMarketStateSnapshot:
    return LatestMarketStateSnapshot(
        market=market, symbol=symbol, trade=trade, quote=quote, trade_fresh=True, quote_fresh=True
    )


_RISING = (
    _sample(seq=1, price="100", qty="10", offset=1),
    _sample(seq=2, price="110", qty="10", offset=2),
    _sample(seq=3, price="120", qty="10", offset=3),
)


# --------------------------------------------------------------------------- #
# ConditionClause window contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "metric",
    [Metric.SMA_PRICE, Metric.RETURN_BPS, Metric.ROLLING_VOLUME, Metric.VWAP],
)
def test_rolling_metric_requires_window(metric: Metric) -> None:
    with pytest.raises(ValidationError):
        ConditionClause(metric=metric, comparator=Comparator.GTE, threshold="1")


@pytest.mark.parametrize(
    "metric",
    [
        Metric.LAST_TRADE_PRICE,
        Metric.BEST_BID_PRICE,
        Metric.BEST_ASK_PRICE,
        Metric.SPREAD_BPS,
    ],
)
def test_latest_metric_forbids_window(metric: Metric) -> None:
    threshold = "0" if metric is Metric.SPREAD_BPS else "1"
    with pytest.raises(ValidationError):
        ConditionClause(
            metric=metric, comparator=Comparator.LTE, threshold=threshold, window=_spec()
        )


def test_rolling_clause_accepts_window() -> None:
    clause = ConditionClause(
        metric=Metric.SMA_PRICE, comparator=Comparator.GTE, threshold="100", window=_spec()
    )
    assert clause.window is not None


# --------------------------------------------------------------------------- #
# Threshold categories
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("metric", [Metric.SMA_PRICE, Metric.VWAP])
def test_rolling_price_threshold_must_be_positive(metric: Metric) -> None:
    with pytest.raises(ValidationError):
        ConditionClause(metric=metric, comparator=Comparator.GTE, threshold="0", window=_spec())


def test_rolling_volume_threshold_allows_zero() -> None:
    clause = ConditionClause(
        metric=Metric.ROLLING_VOLUME, comparator=Comparator.GTE, threshold="0", window=_spec()
    )
    assert clause.threshold == Decimal("0")


def test_rolling_volume_threshold_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        ConditionClause(
            metric=Metric.ROLLING_VOLUME, comparator=Comparator.GTE, threshold="-1", window=_spec()
        )


@pytest.mark.parametrize("value", ["-500", "0", "750"])
def test_return_bps_threshold_allows_any_finite(value: str) -> None:
    clause = ConditionClause(
        metric=Metric.RETURN_BPS, comparator=Comparator.GTE, threshold=value, window=_spec()
    )
    assert clause.threshold == Decimal(value)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_threshold_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        ConditionClause(
            metric=Metric.RETURN_BPS, comparator=Comparator.GTE, threshold=bad, window=_spec()
        )


# --------------------------------------------------------------------------- #
# rule_required_windows / rule_required_slots
# --------------------------------------------------------------------------- #


def test_rule_required_windows_dedupes_and_orders() -> None:
    spec_a = _spec(lookback_events=20, min_events=2)
    spec_b = _spec(lookback_seconds=Decimal("60"), lookback_events=None, min_events=2)
    rules = (
        ConditionClause(metric=Metric.SMA_PRICE, comparator=Comparator.GTE, threshold="1", window=spec_a),
        ConditionClause(metric=Metric.VWAP, comparator=Comparator.GTE, threshold="1", window=spec_a),
        ConditionClause(metric=Metric.ROLLING_VOLUME, comparator=Comparator.GTE, threshold="1", window=spec_b),
    )
    windows = rule_required_windows(rules)
    assert len(windows) == 2
    ids = [w.window_id for w in windows]
    assert ids == sorted(ids)


def test_rule_required_windows_empty_for_price_only() -> None:
    rules = (
        ConditionClause(metric=Metric.LAST_TRADE_PRICE, comparator=Comparator.LTE, threshold="100"),
    )
    assert rule_required_windows(rules) == ()


def test_rolling_metric_requires_trade_slot() -> None:
    rolling = (
        ConditionClause(metric=Metric.VWAP, comparator=Comparator.GTE, threshold="1", window=_spec()),
    )
    needs_trade, needs_quote = rule_required_slots(rolling)
    assert needs_trade is True
    assert needs_quote is False


# --------------------------------------------------------------------------- #
# IndicatorContext
# --------------------------------------------------------------------------- #


def test_context_rejects_duplicate_window_id() -> None:
    win = evaluate_window(_history(_RISING), _spec(), now=_BASE + timedelta(seconds=4))
    with pytest.raises(ValueError, match="duplicate window_id"):
        IndicatorContext(market=Market.KR, symbol="005930", windows=(win, win))


def test_context_rejects_identity_mismatch() -> None:
    win = evaluate_window(_history(_RISING), _spec(), now=_BASE + timedelta(seconds=4))
    with pytest.raises(ValueError, match="market/symbol"):
        IndicatorContext(market=Market.US, symbol="005930", windows=(win,))


def test_context_orders_windows_by_id() -> None:
    spec_a = _spec(lookback_events=20, min_events=2)
    spec_b = _spec(lookback_events=10, min_events=2)
    w_a = evaluate_window(_history(_RISING), spec_a, now=_BASE + timedelta(seconds=4))
    w_b = evaluate_window(_history(_RISING), spec_b, now=_BASE + timedelta(seconds=4))
    ctx = IndicatorContext(market=Market.KR, symbol="005930", windows=(w_a, w_b))
    ids = [w.window_id for w in ctx.windows]
    assert ids == sorted(ids)


def test_context_get_missing_returns_none() -> None:
    ctx = _ctx(_RISING, (_spec(),))
    assert ctx.get("does-not-exist") is None


def test_build_context_computes_unique_window_once() -> None:
    spec = _spec()
    ctx = build_indicator_context(_history(_RISING), (spec, spec), now=_BASE + timedelta(seconds=4))
    assert len(ctx.windows) == 1


def test_build_context_preserves_future_readiness() -> None:
    # now before anchor → FUTURE; context must keep it (no silent drop).
    ctx = build_indicator_context(_history(_RISING), (_spec(),), now=_BASE)
    win = ctx.windows[0]
    assert win.readiness is IndicatorReadiness.FUTURE


# --------------------------------------------------------------------------- #
# Rolling metric evaluation
# --------------------------------------------------------------------------- #


def test_sma_price_value_and_comparison() -> None:
    spec = _spec()
    ctx = _ctx(_RISING, (spec,))
    # SMA of 100,110,120 = 110
    assert metric_value(Metric.SMA_PRICE, _snap(), indicators=ctx, window=spec) == Decimal("110")
    true_clause = ConditionClause(metric=Metric.SMA_PRICE, comparator=Comparator.GTE, threshold="110", window=spec)
    false_clause = ConditionClause(metric=Metric.SMA_PRICE, comparator=Comparator.GTE, threshold="111", window=spec)
    assert evaluate_clause(true_clause, _snap(), indicators=ctx) is True
    assert evaluate_clause(false_clause, _snap(), indicators=ctx) is False


def test_vwap_return_volume_values() -> None:
    spec = _spec()
    ctx = _ctx(_RISING, (spec,))
    assert metric_value(Metric.VWAP, _snap(), indicators=ctx, window=spec) == Decimal("110")
    assert metric_value(Metric.RETURN_BPS, _snap(), indicators=ctx, window=spec) == Decimal("2000")
    assert metric_value(Metric.ROLLING_VOLUME, _snap(), indicators=ctx, window=spec) == Decimal("30")


def test_rolling_metric_not_ready_is_fail_closed_none() -> None:
    spec = _spec(freshness_max_age_seconds=Decimal("1"))
    # now far past anchor → STALE window.
    ctx = _ctx(_RISING, (spec,), now=_BASE + timedelta(seconds=10_000))
    assert ctx.windows[0].readiness is IndicatorReadiness.STALE
    assert metric_value(Metric.SMA_PRICE, _snap(), indicators=ctx, window=spec) is None
    clause = ConditionClause(metric=Metric.SMA_PRICE, comparator=Comparator.GTE, threshold="1", window=spec)
    assert evaluate_clause(clause, _snap(), indicators=ctx) is False


def test_rolling_metric_without_context_is_none() -> None:
    spec = _spec()
    assert metric_value(Metric.SMA_PRICE, _snap(), indicators=None, window=spec) is None


def test_mixed_latest_and_rolling_all_rule() -> None:
    from domain import Currency
    from market_data.models import NormalizedTradeTick, ProviderSequence

    trade = NormalizedTradeTick(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        price="120",
        quantity="10",
        trade_at=_BASE + timedelta(seconds=3),
        received_at=_BASE + timedelta(seconds=3),
        provider_sequence=ProviderSequence(
            provider="kis", channel="H0STCNT0|005930", sequence=3, received_at=_BASE + timedelta(seconds=3)
        ),
    )
    spec = _spec()
    ctx = _ctx(_RISING, (spec,))
    rules = (
        ConditionClause(metric=Metric.LAST_TRADE_PRICE, comparator=Comparator.GTE, threshold="120"),
        ConditionClause(metric=Metric.SMA_PRICE, comparator=Comparator.GTE, threshold="110", window=spec),
    )
    assert evaluate_all(rules, _snap(trade=trade), indicators=ctx) is True
    # rolling clause false → whole AND false.
    rules_false = (
        ConditionClause(metric=Metric.LAST_TRADE_PRICE, comparator=Comparator.GTE, threshold="120"),
        ConditionClause(metric=Metric.SMA_PRICE, comparator=Comparator.GTE, threshold="200", window=spec),
    )
    assert evaluate_all(rules_false, _snap(trade=trade), indicators=ctx) is False


def test_price_only_unaffected_by_indicators_argument() -> None:
    from domain import Currency
    from market_data.models import NormalizedTradeTick, ProviderSequence

    trade = NormalizedTradeTick(
        provider="kis",
        symbol="005930",
        market=Market.KR,
        currency=Currency.KRW,
        price="100",
        quantity="1",
        trade_at=_BASE,
        received_at=_BASE,
        provider_sequence=ProviderSequence(provider="kis", channel="t", sequence=1, received_at=_BASE),
    )
    clause = ConditionClause(metric=Metric.LAST_TRADE_PRICE, comparator=Comparator.LTE, threshold="100")
    # indicators present but irrelevant to a latest metric.
    ctx = _ctx(_RISING, (_spec(),))
    assert evaluate_clause(clause, _snap(trade=trade), indicators=ctx) is True
    assert evaluate_clause(clause, _snap(trade=trade)) is True


def test_window_id_stable_across_equivalent_decimal() -> None:
    # 60 ≡ 60.0 → same window_id.
    a = _spec(lookback_seconds=Decimal("60"), lookback_events=None, min_events=2)
    b = _spec(lookback_seconds=Decimal("60.0"), lookback_events=None, min_events=2)
    assert a.window_id == b.window_id
    win = evaluate_window(_history(_RISING), a, now=_BASE + timedelta(seconds=4))
    _ = replace  # imported for parity with other suites; not needed here
    ctx = IndicatorContext(market=Market.KR, symbol="005930", windows=(win,))
    assert ctx.get(b.window_id) is not None
