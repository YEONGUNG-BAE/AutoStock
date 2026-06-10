"""RTM-4b.2 — TriggerEngine rolling-indicator gating (F4/F5 closure).

엔진은 store를 직접 읽지 않고 주입된 불변 IndicatorContext만 본다. rolling rule이 있는
plan은 (1) context 존재·identity, (2) 각 window readiness, (3) 최신 trade와의 정확한
coherence를 모두 만족할 때만 평가/발화하며, 어느 하나라도 어긋나면 fail-closed로
typed suppress한다(8개 reason). price-only plan은 indicators 없이 RTM-4a와 동일하다.

readiness/coherence 실패는 직접 구성한 IndicatorWindowSnapshot으로 격리 검증하고,
값 계산이 필요한 성공 경로는 실제 samples(SMA=110)로 검증한다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from analysis import (
    AnalysisAction,
    AnalysisDecision,
    AnalysisReason,
    BearPerspective,
    BullPerspective,
    FundManagerDecision,
    RiskManagerEvaluation,
)
from domain import Currency, DateId, DecisionId, Percent
from domain.enums import Market
from market_data.conditions import Comparator, ConditionClause, Metric
from market_data.indicators import (
    IndicatorContext,
    IndicatorReadiness,
    IndicatorWindowSnapshot,
    IndicatorWindowSpec,
)
from market_data.latest_state import LatestMarketStateSnapshot
from market_data.models import (
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)
from market_data.rolling_window import EpochStartReason, TradeSample
from market_data.trigger_engine import (
    DecisionTriggerBundle,
    TradingPermission,
    TriggerEngine,
    TriggerPlan,
    TriggerReason,
    TriggerState,
    TriggerStatus,
)

BASE = datetime(2026, 6, 10, 0, 0, 0, tzinfo=UTC)
EVENT_TIME = BASE + timedelta(seconds=3)  # latest trade/indicator timestamp
NOW = BASE + timedelta(seconds=4)
DAY = timedelta(days=1)
PROVIDER = "kis"
CHANNEL = "H0STCNT0|005930"
SYMBOL = "005930"
SEQ = 3

SPEC = IndicatorWindowSpec(
    lookback_events=3, min_events=2, freshness_max_age_seconds=Decimal("3600")
)
SPEC_RESET = IndicatorWindowSpec(
    lookback_events=4, min_events=2, freshness_max_age_seconds=Decimal("3600")
)

_SAMPLES = (
    TradeSample(price=Decimal("100"), quantity=Decimal("10"), trade_at=BASE + timedelta(seconds=1), received_at=BASE + timedelta(seconds=1), sequence=1),
    TradeSample(price=Decimal("110"), quantity=Decimal("10"), trade_at=BASE + timedelta(seconds=2), received_at=BASE + timedelta(seconds=2), sequence=2),
    TradeSample(price=Decimal("120"), quantity=Decimal("10"), trade_at=EVENT_TIME, received_at=EVENT_TIME, sequence=SEQ),
)  # SMA = 110, VWAP = 110, RETURN_BPS = 2000, VOLUME = 30


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _reason(date_id: str = "260610-1") -> AnalysisReason:
    return AnalysisReason(reason="근거", date_id=DateId(date_id))


def _decision(*, action: AnalysisAction = AnalysisAction.BUY) -> AnalysisDecision:
    return AnalysisDecision(
        decision_id=DecisionId("analysis-260610-001"),
        created_at=BASE,
        universe="KR_LARGE",
        symbol=SYMBOL,
        market="KR",
        summary_one_liner="요약",
        bear=BearPerspective(summary="하방", risks=("리스크",), reasons=(_reason(),)),
        bull=BullPerspective(summary="상방", catalysts=("촉매",), reasons=(_reason("260610-2"),)),
        risk_manager=RiskManagerEvaluation(summary="중립", reasons=(_reason("260610-3"),)),
        fund_manager=FundManagerDecision(
            action=action,
            target_weight_percent=Percent("5"),
            rationale="근거",
            reasons=(_reason("260610-4"),),
        ),
        reasons=(_reason("260610-5"),),
    )


def _rolling_clause(
    metric: Metric = Metric.SMA_PRICE,
    comparator: Comparator = Comparator.GTE,
    threshold: str = "110",
    *,
    window: IndicatorWindowSpec = SPEC,
) -> ConditionClause:
    return ConditionClause(metric=metric, comparator=comparator, threshold=threshold, window=window)


def _plan(
    *,
    action: AnalysisAction = AnalysisAction.BUY,
    rules: tuple[ConditionClause, ...] | None = None,
    reset_rules: tuple[ConditionClause, ...] = (),
    **over: object,
) -> TriggerPlan:
    if rules is None:
        rules = (_rolling_clause(),)
    return TriggerPlan(
        plan_id="plan-1",
        decision_id=DecisionId("analysis-260610-001"),
        created_at=BASE,
        valid_from=BASE,
        expires_at=BASE + DAY,
        universe="KR_LARGE",
        market=Market.KR,
        symbol=SYMBOL,
        action=action,
        rules=rules,
        reset_rules=reset_rules,
        **over,
    )


def _bundle(*, action: AnalysisAction = AnalysisAction.BUY, plan: TriggerPlan | None = None) -> DecisionTriggerBundle:
    if plan is None:
        plan = _plan(action=action)
    return DecisionTriggerBundle(decision=_decision(action=action), plan=plan)


def _permission(*, allowed: bool = True) -> TradingPermission:
    return TradingPermission(
        market=Market.KR, allowed=allowed, checked_at=BASE, valid_until=BASE + DAY, reason_code="open"
    )


def _quote() -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider=PROVIDER, symbol=SYMBOL, market=Market.KR, currency=Currency.KRW,
        bid_price="119", ask_price="121", bid_quantity="10", ask_quantity="10",
        quote_at=EVENT_TIME, received_at=EVENT_TIME,
        provider_sequence=ProviderSequence(provider=PROVIDER, channel="q", sequence=9, received_at=EVENT_TIME),
    )


def _trade(*, seq: int = SEQ, trade_at: datetime = EVENT_TIME, received_at: datetime = EVENT_TIME) -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider=PROVIDER, symbol=SYMBOL, market=Market.KR, currency=Currency.KRW,
        price="120", quantity="10", trade_at=trade_at, received_at=received_at,
        provider_sequence=ProviderSequence(provider=PROVIDER, channel=CHANNEL, sequence=seq, received_at=received_at),
    )


def _snap(
    *, trade: NormalizedTradeTick | None = None, trade_fresh: bool = True,
    evaluated_at: datetime = NOW,
) -> LatestMarketStateSnapshot:
    if trade is None:
        trade = _trade()
    return LatestMarketStateSnapshot(
        market=Market.KR, symbol=SYMBOL, trade=trade, quote=_quote(),
        trade_fresh=trade_fresh, quote_fresh=True, evaluated_at=evaluated_at,
    )


def _window(
    readiness: IndicatorReadiness = IndicatorReadiness.READY,
    *,
    window_id: str | None = None,
    market: Market = Market.KR,
    symbol: str = SYMBOL,
    provider: str | None = PROVIDER,
    channel: str | None = CHANNEL,
    seq: int | None = SEQ,
    event_time: datetime | None = EVENT_TIME,
    received_at: datetime | None = EVENT_TIME,
    selected: tuple[TradeSample, ...] = _SAMPLES,
) -> IndicatorWindowSnapshot:
    """coherent READY window를 기본으로, override로 readiness/coherence 변형을 만든다."""
    return IndicatorWindowSnapshot(
        window_id=window_id or SPEC.window_id,
        market=market,
        symbol=symbol,
        readiness=readiness,
        selected=selected,
        anchor_event_time=event_time,
        latest_event_time=event_time,
        oldest_selected_event_time=selected[0].trade_at if selected else None,
        age_seconds=Decimal("1"),
        continuity_epoch=1,
        epoch_start_reason=EpochStartReason.INITIAL,
        provider=provider,
        channel=channel,
        latest_sequence=seq,
        latest_received_at=received_at,
    )


def _ctx(
    *windows: IndicatorWindowSnapshot, market: Market = Market.KR, symbol: str = SYMBOL,
    evaluated_at: datetime = NOW,
) -> IndicatorContext:
    if not windows:
        windows = (_window(),)
    return IndicatorContext(market=market, symbol=symbol, windows=windows, evaluated_at=evaluated_at)


def _armed(*, plan: TriggerPlan | None = None, action: AnalysisAction = AnalysisAction.BUY) -> TriggerEngine:
    engine = TriggerEngine()
    engine.replace_bundle(_bundle(action=action, plan=plan), now=BASE)
    return engine


# --------------------------------------------------------------------------- #
# happy path: READY + exact coherence → fire with window_id evidence
# --------------------------------------------------------------------------- #
def test_rolling_rule_fires_with_window_evidence() -> None:
    engine = _armed()
    result = engine.evaluate(_snap(), _permission(), now=NOW, indicators=_ctx())
    assert result.status is TriggerStatus.TRIGGERED
    assert result.state is TriggerState.LOCKED
    assert result.signal is not None
    assert result.signal.reference_price == Decimal("121")  # BUY → ask
    obs = result.signal.condition_values
    assert len(obs) == 1
    assert obs[0].metric is Metric.SMA_PRICE
    assert obs[0].value == Decimal("110")
    assert obs[0].window_id == SPEC.window_id


def test_rolling_rule_condition_false_keeps_armed() -> None:
    plan = _plan(rules=(_rolling_clause(threshold="200"),))  # SMA 110 < 200
    engine = _armed(plan=plan)
    result = engine.evaluate(_snap(), _permission(), now=NOW, indicators=_ctx())
    assert result.status is TriggerStatus.CONDITION_NOT_MET
    assert result.state is TriggerState.ARMED
    assert result.reason is TriggerReason.CONDITION_FALSE


# --------------------------------------------------------------------------- #
# readiness gating (8 reasons, isolated) — state must not advance on suppress
# --------------------------------------------------------------------------- #
def _suppress(engine: TriggerEngine, indicators: IndicatorContext | None) -> object:
    return engine.evaluate(_snap(), _permission(), now=NOW, indicators=indicators)


def test_missing_indicator_when_context_none() -> None:
    engine = _armed()
    result = _suppress(engine, None)
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.MISSING_INDICATOR
    assert result.state is TriggerState.ARMED


def test_missing_indicator_when_window_absent() -> None:
    engine = _armed()
    # context has a window, but not the one this rule requires.
    other = _window(window_id="some-other-window-id")
    result = _suppress(engine, _ctx(other))
    assert result.reason is TriggerReason.MISSING_INDICATOR


def test_indicator_warming() -> None:
    engine = _armed()
    result = _suppress(engine, _ctx(_window(IndicatorReadiness.WARMING)))
    assert result.reason is TriggerReason.INDICATOR_WARMING


def test_indicator_discontinuous() -> None:
    engine = _armed()
    result = _suppress(engine, _ctx(_window(IndicatorReadiness.DISCONTINUOUS)))
    assert result.reason is TriggerReason.INDICATOR_DISCONTINUOUS


def test_indicator_stale() -> None:
    engine = _armed()
    result = _suppress(engine, _ctx(_window(IndicatorReadiness.STALE)))
    assert result.reason is TriggerReason.INDICATOR_STALE


def test_indicator_future() -> None:
    engine = _armed()
    result = _suppress(engine, _ctx(_window(IndicatorReadiness.FUTURE)))
    assert result.reason is TriggerReason.INDICATOR_FUTURE


def test_indicator_insufficient_retention() -> None:
    engine = _armed()
    result = _suppress(engine, _ctx(_window(IndicatorReadiness.INSUFFICIENT_RETENTION)))
    assert result.reason is TriggerReason.INDICATOR_INSUFFICIENT_RETENTION


def test_context_identity_mismatch() -> None:
    engine = _armed()
    # snapshot identity matches the plan, but the indicator context is for another symbol.
    ctx = _ctx(_window(symbol="000660"), symbol="000660")
    result = _suppress(engine, ctx)
    assert result.reason is TriggerReason.INDICATOR_IDENTITY_MISMATCH


# --------------------------------------------------------------------------- #
# latest↔indicator coherence (F5)
# --------------------------------------------------------------------------- #
def test_coherence_provider_mismatch() -> None:
    engine = _armed()
    result = _suppress(engine, _ctx(_window(provider="other")))
    assert result.reason is TriggerReason.INDICATOR_IDENTITY_MISMATCH


def test_coherence_channel_mismatch() -> None:
    engine = _armed()
    result = _suppress(engine, _ctx(_window(channel="other-channel")))
    assert result.reason is TriggerReason.INDICATOR_IDENTITY_MISMATCH


def test_coherence_sequence_lag() -> None:
    engine = _armed()
    # indicator latest_sequence behind latest trade sequence.
    result = _suppress(engine, _ctx(_window(seq=SEQ - 1)))
    assert result.reason is TriggerReason.INDICATOR_LAGGING


def test_coherence_event_time_lag() -> None:
    engine = _armed()
    result = _suppress(engine, _ctx(_window(event_time=BASE + timedelta(seconds=2))))
    assert result.reason is TriggerReason.INDICATOR_LAGGING


def test_coherence_received_at_mismatch_is_identity_mismatch() -> None:
    engine = _armed()
    # seq == and event_time ==, but received_at differs → not exact, not lagging.
    result = _suppress(engine, _ctx(_window(received_at=EVENT_TIME + timedelta(seconds=1))))
    assert result.reason is TriggerReason.INDICATOR_IDENTITY_MISMATCH


def test_coherence_indicator_ahead_is_identity_mismatch() -> None:
    engine = _armed()
    # indicator strictly ahead of latest trade → fail-closed.
    result = _suppress(engine, _ctx(_window(seq=SEQ + 1)))
    assert result.reason is TriggerReason.INDICATOR_IDENTITY_MISMATCH


def test_coherence_missing_metadata_is_identity_mismatch() -> None:
    engine = _armed()
    # READY but latest metadata absent (defensive) → identity mismatch.
    result = _suppress(engine, _ctx(_window(seq=None)))
    assert result.reason is TriggerReason.INDICATOR_IDENTITY_MISMATCH


def test_suppress_does_not_consume_fire_budget() -> None:
    engine = _armed()
    # a coherence failure must leave the engine ARMED and able to fire later.
    bad = engine.evaluate(_snap(), _permission(), now=NOW, indicators=_ctx(_window(seq=SEQ + 1)))
    assert bad.status is TriggerStatus.SUPPRESSED
    good = engine.evaluate(_snap(), _permission(), now=NOW, indicators=_ctx())
    assert good.status is TriggerStatus.TRIGGERED


# --------------------------------------------------------------------------- #
# price-only regression: indicators=None must behave like RTM-4a
# --------------------------------------------------------------------------- #
def test_price_only_plan_ignores_indicators() -> None:
    plan = _plan(rules=(ConditionClause(metric=Metric.LAST_TRADE_PRICE, comparator=Comparator.LTE, threshold="120"),))
    engine = _armed(plan=plan)
    result = engine.evaluate(_snap(), _permission(), now=NOW, indicators=None)
    assert result.status is TriggerStatus.TRIGGERED
    assert result.signal is not None
    assert result.signal.condition_values[0].window_id is None


def test_missing_trade_for_rolling_plan() -> None:
    engine = _armed()
    snap = LatestMarketStateSnapshot(
        market=Market.KR, symbol=SYMBOL, trade=None, quote=_quote(), trade_fresh=True,
        quote_fresh=True, evaluated_at=NOW,
    )
    result = engine.evaluate(snap, _permission(), now=NOW, indicators=_ctx())
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.MISSING_TRADE


def test_stale_trade_gated_before_indicator() -> None:
    engine = _armed()
    result = engine.evaluate(_snap(trade_fresh=False), _permission(), now=NOW, indicators=_ctx())
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.STALE_TRADE


# --------------------------------------------------------------------------- #
# reset_rules rolling gate (F2 extension) in COOLDOWN
# --------------------------------------------------------------------------- #
def _cooldown_engine() -> TriggerEngine:
    """fire once (max_fires=2) so the engine sits in COOLDOWN with reset pending."""
    plan = _plan(
        rules=(_rolling_clause(),),
        reset_rules=(_rolling_clause(metric=Metric.SMA_PRICE, comparator=Comparator.LTE, threshold="1000", window=SPEC_RESET),),
        max_fires_per_decision=2,
        cooldown_seconds=Decimal("0"),
        reset_events=1,
    )
    engine = _armed(plan=plan)
    first = engine.evaluate(_snap(), _permission(), now=NOW, indicators=_ctx())
    assert first.status is TriggerStatus.TRIGGERED
    assert first.state is TriggerState.COOLDOWN
    return engine


def test_cooldown_holds_when_reset_indicator_stale() -> None:
    engine = _cooldown_engine()
    ctx = _ctx(_window(), _window(IndicatorReadiness.STALE, window_id=SPEC_RESET.window_id))
    result = engine.evaluate(_snap(), _permission(), now=NOW, indicators=ctx)
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.INDICATOR_STALE
    assert result.state is TriggerState.COOLDOWN


def test_cooldown_holds_when_reset_indicator_missing() -> None:
    engine = _cooldown_engine()
    # reset window absent from context → MISSING_INDICATOR, still COOLDOWN.
    result = engine.evaluate(_snap(), _permission(), now=NOW, indicators=_ctx())
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.MISSING_INDICATOR
    assert result.state is TriggerState.COOLDOWN


def test_cooldown_holds_when_reset_indicator_lagging() -> None:
    engine = _cooldown_engine()
    reset = _window(window_id=SPEC_RESET.window_id, seq=SEQ - 1)
    result = engine.evaluate(_snap(), _permission(), now=NOW, indicators=_ctx(_window(), reset))
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.INDICATOR_LAGGING
    assert result.state is TriggerState.COOLDOWN


def test_cooldown_rearms_then_refires_with_coherent_reset() -> None:
    engine = _cooldown_engine()
    ctx = _ctx(_window(), _window(window_id=SPEC_RESET.window_id))
    result = engine.evaluate(_snap(), _permission(), now=NOW, indicators=ctx)
    # reset satisfied + cooldown elapsed → re-arm and fire the 2nd (final) time.
    assert result.status is TriggerStatus.TRIGGERED
    assert result.state is TriggerState.LOCKED


def test_initial_trigger_does_not_require_reset_indicator() -> None:
    # plan has rolling reset_rules, but the first fire must not require the reset window.
    plan = _plan(
        rules=(_rolling_clause(),),
        reset_rules=(_rolling_clause(window=SPEC_RESET),),
        max_fires_per_decision=2,
    )
    engine = _armed(plan=plan)
    # context only carries the main rule window, not the reset window.
    result = engine.evaluate(_snap(), _permission(), now=NOW, indicators=_ctx())
    assert result.status is TriggerStatus.TRIGGERED


# --------------------------------------------------------------------------- #
# canonicalization: window identity flows into rule_set_id / idempotency
# --------------------------------------------------------------------------- #
def _idempotency_key_for(window: IndicatorWindowSpec) -> str:
    plan = _plan(rules=(_rolling_clause(window=window),))
    engine = _armed(plan=plan)
    result = engine.evaluate(_snap(), _permission(), now=NOW, indicators=_ctx(_window(window_id=window.window_id)))
    assert result.signal is not None
    return result.signal.idempotency_key


def test_different_window_yields_different_idempotency() -> None:
    spec_other = IndicatorWindowSpec(lookback_events=10, min_events=2, freshness_max_age_seconds=Decimal("3600"))
    assert _idempotency_key_for(SPEC) != _idempotency_key_for(spec_other)


def test_equivalent_decimal_window_yields_same_idempotency() -> None:
    a = IndicatorWindowSpec(lookback_seconds=Decimal("60"), min_events=2, freshness_max_age_seconds=Decimal("3600"))
    b = IndicatorWindowSpec(lookback_seconds=Decimal("60.0"), min_events=2, freshness_max_age_seconds=Decimal("3600"))
    assert a.window_id == b.window_id
    assert _idempotency_key_for(a) == _idempotency_key_for(b)


# --------------------------------------------------------------------------- #
# RTM-4b.2 hardening: IndicatorContext as-of staleness (fail-closed)
# --------------------------------------------------------------------------- #
def test_reused_stale_indicator_context_is_suppressed() -> None:
    # snapshot is fresh (evaluated_at == now) so the snapshot gate passes, but the
    # injected IndicatorContext was built at an earlier tick. The context as-of gate
    # must suppress with INDICATOR_CONTEXT_STALE rather than fire on stale windows.
    engine = _armed()
    snap = _snap(evaluated_at=NOW)
    stale_ctx = _ctx(evaluated_at=BASE)  # built before NOW
    result = engine.evaluate(snap, _permission(), now=NOW, indicators=stale_ctx)
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.INDICATOR_CONTEXT_STALE
    assert engine.state is TriggerState.ARMED


def test_fresh_indicator_context_at_exact_now_fires() -> None:
    # control: context evaluated_at == now → as-of gate passes and the rolling plan fires.
    engine = _armed()
    result = engine.evaluate(_snap(evaluated_at=NOW), _permission(), now=NOW, indicators=_ctx(evaluated_at=NOW))
    assert result.status is TriggerStatus.TRIGGERED
