from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from analysis import (
    AnalysisAction,
    AnalysisDecision,
    AnalysisReason,
    BearPerspective,
    BullPerspective,
    FundManagerDecision,
    RiskManagerEvaluation,
)
from domain import DateId, DecisionId, Percent, Currency
from domain.enums import Market
from market_data.conditions import Comparator, ConditionClause, Metric
from market_data.latest_state import LatestMarketStateSnapshot
from market_data.models import (
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)
from market_data.trigger_engine import (
    DecisionTriggerBundle,
    ReplaceStatus,
    TradingPermission,
    TriggerEngine,
    TriggerPlan,
    TriggerReason,
    TriggerState,
    TriggerStatus,
)

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
_TRIGGER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "market_data" / "triggers"


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _reason(date_id: str = "260522-1") -> AnalysisReason:
    return AnalysisReason(reason="근거", date_id=DateId(date_id))


def _decision(
    *,
    action: AnalysisAction = AnalysisAction.BUY,
    decision_id: str = "analysis-260522-001",
    created_at: datetime = NOW,
    symbol: str = "005930",
    market: str = "KR",
    universe: str = "KR_LARGE",
) -> AnalysisDecision:
    return AnalysisDecision(
        decision_id=DecisionId(decision_id),
        created_at=created_at,
        universe=universe,
        symbol=symbol,
        market=market,
        summary_one_liner="요약",
        bear=BearPerspective(summary="하방", risks=("리스크",), reasons=(_reason(),)),
        bull=BullPerspective(summary="상방", catalysts=("촉매",), reasons=(_reason("260522-2"),)),
        risk_manager=RiskManagerEvaluation(summary="중립", reasons=(_reason("260522-3"),)),
        fund_manager=FundManagerDecision(
            action=action,
            target_weight_percent=Percent("5"),
            rationale="근거",
            reasons=(_reason("260522-4"),),
        ),
        reasons=(_reason("260522-5"),),
    )


def _clause(metric: Metric, comparator: Comparator, threshold: str) -> ConditionClause:
    return ConditionClause(metric=metric, comparator=comparator, threshold=threshold)


def _plan(
    *,
    action: AnalysisAction = AnalysisAction.BUY,
    decision_id: str = "analysis-260522-001",
    rules: tuple[ConditionClause, ...] | None = None,
    reset_rules: tuple[ConditionClause, ...] = (),
    created_at: datetime = NOW,
    valid_from: datetime = NOW,
    expires_at: datetime | None = None,
    **over: object,
) -> TriggerPlan:
    if rules is None:
        rules = (_clause(Metric.LAST_TRADE_PRICE, Comparator.LTE, "100"),)
    return TriggerPlan(
        plan_id="plan-1",
        decision_id=DecisionId(decision_id),
        created_at=created_at,
        valid_from=valid_from,
        expires_at=expires_at if expires_at is not None else NOW + DAY,
        universe="KR_LARGE",
        market=Market.KR,
        symbol="005930",
        action=action,
        rules=rules,
        reset_rules=reset_rules,
        **over,
    )


def _bundle(
    *, action: AnalysisAction = AnalysisAction.BUY, decision_id: str = "analysis-260522-001",
    created_at: datetime = NOW, plan: TriggerPlan | None = None,
) -> DecisionTriggerBundle:
    decision = _decision(action=action, decision_id=decision_id, created_at=created_at)
    if action is AnalysisAction.HOLD:
        return DecisionTriggerBundle(decision=decision, plan=None)
    if plan is None:
        plan = _plan(
            action=action,
            decision_id=decision_id,
            created_at=created_at,
            valid_from=created_at,
            expires_at=created_at + DAY,
        )
    return DecisionTriggerBundle(decision=decision, plan=plan)


def _permission(
    *,
    allowed: bool = True,
    market: Market = Market.KR,
    checked_at: datetime = NOW,
    valid_until: datetime | None = None,
) -> TradingPermission:
    return TradingPermission(
        market=market,
        allowed=allowed,
        checked_at=checked_at,
        valid_until=valid_until if valid_until is not None else NOW + DAY,
        reason_code="open",
    )


def _quote(*, bid: str = "99", ask: str = "101") -> NormalizedBestBidAsk:
    return NormalizedBestBidAsk(
        provider="kis", symbol="005930", market=Market.KR, currency=Currency.KRW,
        bid_price=bid, ask_price=ask, bid_quantity="10", ask_quantity="10",
        quote_at=NOW, received_at=NOW,
        provider_sequence=ProviderSequence(provider="kis", channel="q", sequence=1, received_at=NOW),
    )


def _trade(*, price: str = "100") -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider="kis", symbol="005930", market=Market.KR, currency=Currency.KRW,
        price=price, quantity="1", trade_at=NOW, received_at=NOW,
        provider_sequence=ProviderSequence(provider="kis", channel="t", sequence=1, received_at=NOW),
    )


def _snap(
    *, trade: NormalizedTradeTick | None = None, quote: NormalizedBestBidAsk | None = None,
    trade_fresh: bool = True, quote_fresh: bool = True,
    evaluated_at: datetime = NOW,
) -> LatestMarketStateSnapshot:
    return LatestMarketStateSnapshot(
        market=Market.KR, symbol="005930", trade=trade, quote=quote,
        trade_fresh=trade_fresh, quote_fresh=quote_fresh, evaluated_at=evaluated_at,
    )


def _fireable_snap(*, price: str = "100", at: datetime = NOW) -> LatestMarketStateSnapshot:
    return _snap(trade=_trade(price=price), quote=_quote(), evaluated_at=at)


def _armed_engine(**plan_over: object) -> TriggerEngine:
    engine = TriggerEngine()
    engine.replace_bundle(_bundle(plan=_plan(**plan_over)), now=NOW)
    return engine


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_buy_fires_and_locks_with_ask_reference_price() -> None:
    engine = _armed_engine()
    result = engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)
    assert result.status is TriggerStatus.TRIGGERED
    assert result.state is TriggerState.LOCKED
    assert result.signal is not None
    assert result.signal.action is AnalysisAction.BUY
    assert result.signal.reference_price == Decimal("101")  # ask
    assert result.signal.symbol == "005930"
    assert tuple(o.metric for o in result.signal.condition_values) == (Metric.LAST_TRADE_PRICE,)


def test_sell_uses_bid_reference_price() -> None:
    engine = TriggerEngine()
    plan = _plan(
        action=AnalysisAction.SELL,
        rules=(_clause(Metric.LAST_TRADE_PRICE, Comparator.GTE, "100"),),
    )
    engine.replace_bundle(_bundle(action=AnalysisAction.SELL, plan=plan), now=NOW)
    result = engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)
    assert result.status is TriggerStatus.TRIGGERED
    assert result.signal is not None
    assert result.signal.reference_price == Decimal("99")  # bid


def test_condition_not_met_keeps_armed() -> None:
    engine = _armed_engine()
    result = engine.evaluate(_fireable_snap(price="101"), _permission(), now=NOW)
    assert result.status is TriggerStatus.CONDITION_NOT_MET
    assert result.state is TriggerState.ARMED
    assert result.reason is TriggerReason.CONDITION_FALSE
    assert result.signal is None


# --------------------------------------------------------------------------- #
# HOLD / no decision
# --------------------------------------------------------------------------- #
def test_hold_is_always_suppressed() -> None:
    engine = TriggerEngine()
    engine.replace_bundle(_bundle(action=AnalysisAction.HOLD), now=NOW)
    result = engine.evaluate(_fireable_snap(), _permission(), now=NOW)
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.state is TriggerState.DISARMED
    assert result.reason is TriggerReason.HOLD_ACTION


def test_no_active_decision_suppressed() -> None:
    engine = TriggerEngine()
    result = engine.evaluate(_fireable_snap(), _permission(), now=NOW)
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.NO_ACTIVE_DECISION


# --------------------------------------------------------------------------- #
# decision freshness window
# --------------------------------------------------------------------------- #
def test_decision_not_yet_valid() -> None:
    engine = TriggerEngine()
    plan = _plan(created_at=NOW, valid_from=NOW + timedelta(hours=1), expires_at=NOW + DAY)
    engine.replace_bundle(_bundle(plan=plan), now=NOW)
    result = engine.evaluate(_fireable_snap(), _permission(), now=NOW)
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.DECISION_NOT_YET_VALID


def test_stale_decision() -> None:
    engine = TriggerEngine()
    plan = _plan(created_at=NOW - 2 * DAY, valid_from=NOW - 2 * DAY, expires_at=NOW - DAY)
    engine.replace_bundle(_bundle(plan=plan, created_at=NOW - 2 * DAY), now=NOW)
    result = engine.evaluate(_fireable_snap(), _permission(), now=NOW)
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.STALE_DECISION


# --------------------------------------------------------------------------- #
# permission (default deny)
# --------------------------------------------------------------------------- #
def test_permission_none_denies() -> None:
    engine = _armed_engine()
    result = engine.evaluate(_fireable_snap(), None, now=NOW)
    assert result.reason is TriggerReason.TRADING_NOT_ALLOWED


def test_permission_market_mismatch() -> None:
    engine = _armed_engine()
    result = engine.evaluate(_fireable_snap(), _permission(market=Market.US), now=NOW)
    assert result.reason is TriggerReason.PERMISSION_MARKET_MISMATCH


def test_permission_not_allowed() -> None:
    engine = _armed_engine()
    result = engine.evaluate(_fireable_snap(), _permission(allowed=False), now=NOW)
    assert result.reason is TriggerReason.TRADING_NOT_ALLOWED


def test_stale_permission() -> None:
    engine = _armed_engine()
    expired = _permission(checked_at=NOW - 2 * DAY, valid_until=NOW - timedelta(seconds=1))
    result = engine.evaluate(_fireable_snap(), expired, now=NOW)
    assert result.reason is TriggerReason.STALE_PERMISSION


# --------------------------------------------------------------------------- #
# market-state freshness
# --------------------------------------------------------------------------- #
def test_missing_quote() -> None:
    engine = _armed_engine()
    result = engine.evaluate(_snap(trade=_trade()), _permission(), now=NOW)
    assert result.reason is TriggerReason.MISSING_QUOTE


def test_stale_quote() -> None:
    engine = _armed_engine()
    snap = _snap(trade=_trade(), quote=_quote(), quote_fresh=False)
    result = engine.evaluate(snap, _permission(), now=NOW)
    assert result.reason is TriggerReason.STALE_QUOTE


def test_missing_trade_when_rule_needs_trade() -> None:
    engine = _armed_engine()
    result = engine.evaluate(_snap(quote=_quote()), _permission(), now=NOW)
    assert result.reason is TriggerReason.MISSING_TRADE


def test_stale_trade_when_rule_needs_trade() -> None:
    engine = _armed_engine()
    snap = _snap(trade=_trade(), quote=_quote(), trade_fresh=False)
    result = engine.evaluate(snap, _permission(), now=NOW)
    assert result.reason is TriggerReason.STALE_TRADE


def test_quote_only_rule_does_not_require_trade() -> None:
    engine = _armed_engine(rules=(_clause(Metric.SPREAD_BPS, Comparator.LTE, "300"),))
    result = engine.evaluate(_snap(quote=_quote()), _permission(), now=NOW)  # no trade slot
    assert result.status is TriggerStatus.TRIGGERED


# --------------------------------------------------------------------------- #
# debounce
# --------------------------------------------------------------------------- #
def test_debounce_events_requires_n_consecutive() -> None:
    engine = _armed_engine(debounce_events=2)
    first = engine.evaluate(_fireable_snap(), _permission(), now=NOW)
    assert first.status is TriggerStatus.DEBOUNCING
    assert first.state is TriggerState.DEBOUNCING
    second = engine.evaluate(_fireable_snap(), _permission(), now=NOW)
    assert second.status is TriggerStatus.TRIGGERED


def test_debounce_counter_resets_on_condition_break() -> None:
    engine = _armed_engine(debounce_events=3)
    engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)  # count 1
    broke = engine.evaluate(_fireable_snap(price="101"), _permission(), now=NOW)  # false
    assert broke.status is TriggerStatus.CONDITION_NOT_MET
    assert broke.state is TriggerState.ARMED
    # counter restarts: need 3 again
    engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)  # 1
    engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)  # 2
    third = engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)  # 3
    assert third.status is TriggerStatus.TRIGGERED


def test_debounce_counter_resets_on_suppression() -> None:
    engine = _armed_engine(debounce_events=2)
    engine.evaluate(_fireable_snap(), _permission(), now=NOW)  # DEBOUNCING count 1
    stale = engine.evaluate(
        _snap(trade=_trade(), quote=_quote(), quote_fresh=False), _permission(), now=NOW
    )
    assert stale.status is TriggerStatus.SUPPRESSED
    assert stale.state is TriggerState.ARMED  # break reset


def test_debounce_seconds_boundary() -> None:
    engine = _armed_engine(debounce_events=1, debounce_seconds="2")
    early = engine.evaluate(_fireable_snap(), _permission(), now=NOW)
    assert early.status is TriggerStatus.DEBOUNCING  # events ok, seconds not
    late = engine.evaluate(_fireable_snap(at=NOW + timedelta(seconds=2)), _permission(), now=NOW + timedelta(seconds=2))
    assert late.status is TriggerStatus.TRIGGERED


# --------------------------------------------------------------------------- #
# dedup / idempotency
# --------------------------------------------------------------------------- #
def test_locked_after_fire_reports_already_fired() -> None:
    engine = _armed_engine()
    engine.evaluate(_fireable_snap(), _permission(), now=NOW)  # fire → LOCKED
    again = engine.evaluate(_fireable_snap(), _permission(), now=NOW)
    assert again.status is TriggerStatus.ALREADY_FIRED
    assert again.reason is TriggerReason.MAX_FIRES_REACHED
    assert again.signal is None


def test_same_activation_yields_identical_idempotency_key() -> None:
    a = _armed_engine()
    b = _armed_engine()
    ra = a.evaluate(_fireable_snap(), _permission(), now=NOW)
    rb = b.evaluate(_fireable_snap(at=NOW + timedelta(seconds=5)), _permission(), now=NOW + timedelta(seconds=5))
    assert ra.signal is not None and rb.signal is not None
    # key excludes wall-clock/price → identical across engines for same activation
    assert ra.signal.idempotency_key == rb.signal.idempotency_key


# --------------------------------------------------------------------------- #
# cooldown / reset / max_fires
# --------------------------------------------------------------------------- #
def test_cooldown_blocks_until_elapsed_then_rearms() -> None:
    engine = _armed_engine(max_fires_per_decision=2, cooldown_seconds="10", reset_events=1)
    fire1 = engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)
    assert fire1.status is TriggerStatus.TRIGGERED
    assert fire1.state is TriggerState.COOLDOWN
    # within cooldown, even with reset condition true → still cooling
    cooling = engine.evaluate(_fireable_snap(price="101", at=NOW + timedelta(seconds=5)), _permission(), now=NOW + timedelta(seconds=5))
    assert cooling.status is TriggerStatus.COOLDOWN
    assert cooling.reason is TriggerReason.COOLDOWN_ACTIVE
    # cooldown elapsed + reset satisfied (price 101 → condition false) → rearm this tick
    rearm = engine.evaluate(_fireable_snap(price="101", at=NOW + timedelta(seconds=11)), _permission(), now=NOW + timedelta(seconds=11))
    assert rearm.status is TriggerStatus.CONDITION_NOT_MET
    assert rearm.state is TriggerState.ARMED
    fire2 = engine.evaluate(_fireable_snap(price="100", at=NOW + timedelta(seconds=12)), _permission(), now=NOW + timedelta(seconds=12))
    assert fire2.status is TriggerStatus.TRIGGERED
    assert fire2.state is TriggerState.LOCKED  # max_fires=2 reached


def test_rearm_requires_reset_events_consecutive() -> None:
    engine = _armed_engine(max_fires_per_decision=2, cooldown_seconds="0", reset_events=2)
    engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)  # fire1 → COOLDOWN
    # one reset-true tick is not enough (need 2 consecutive)
    one = engine.evaluate(_fireable_snap(price="101"), _permission(), now=NOW)
    assert one.status is TriggerStatus.COOLDOWN
    two = engine.evaluate(_fireable_snap(price="101"), _permission(), now=NOW)
    # second consecutive reset → rearm, condition false → CONDITION_NOT_MET/ARMED
    assert two.state is TriggerState.ARMED


# --------------------------------------------------------------------------- #
# F2: reset_rules freshness gate (stale reset input must never re-arm/fire)
# --------------------------------------------------------------------------- #
# quote-only trigger rules + trade-only reset_rules. plan.rules의 slot 검증은
# trade를 요구하지 않으므로, reset_rules가 추가로 읽는 trade slot은 COOLDOWN의
# reset gate에서 처음 검증된다.
def _reset_gate_engine(**over: object) -> TriggerEngine:
    base: dict[str, object] = dict(
        rules=(_clause(Metric.BEST_ASK_PRICE, Comparator.LTE, "101"),),
        reset_rules=(_clause(Metric.LAST_TRADE_PRICE, Comparator.GTE, "100"),),
        max_fires_per_decision=2,
        cooldown_seconds="0",
        reset_events=1,
    )
    base.update(over)
    return _armed_engine(**base)


def test_quote_only_rules_fire_without_trade_present() -> None:
    # reset gate가 초기 무장/발화를 막지 않음을 먼저 못박는다: trade 없이도 발화한다.
    engine = _reset_gate_engine()
    res = engine.evaluate(_snap(trade=None, quote=_quote(ask="101")), _permission(), now=NOW)
    assert res.status is TriggerStatus.TRIGGERED
    assert res.state is TriggerState.COOLDOWN


def test_reset_with_stale_trade_input_holds_cooldown() -> None:
    engine = _reset_gate_engine()
    fire1 = engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)
    assert fire1.state is TriggerState.COOLDOWN
    # reset 조건 자체는 충족(trade 100 >= 100)이지만 trade가 stale → re-arm 금지.
    held = engine.evaluate(
        _snap(trade=_trade(price="100"), quote=_quote(), trade_fresh=False),
        _permission(),
        now=NOW,
    )
    assert held.status is TriggerStatus.SUPPRESSED
    assert held.state is TriggerState.COOLDOWN
    assert held.reason is TriggerReason.STALE_TRADE


def test_reset_with_missing_trade_input_holds_cooldown() -> None:
    engine = _reset_gate_engine()
    engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)  # COOLDOWN
    held = engine.evaluate(_snap(trade=None, quote=_quote()), _permission(), now=NOW)
    assert held.status is TriggerStatus.SUPPRESSED
    assert held.state is TriggerState.COOLDOWN
    assert held.reason is TriggerReason.MISSING_TRADE


def test_reset_count_zeroed_after_stale_suppression() -> None:
    # reset_events=2: 하나의 fresh reset 뒤 stale가 끼어들면 누적 count가 0으로 리셋돼
    # 이후 단일 fresh reset로는 re-arm되지 않아야 한다(비연속 reset 오인 방지).
    engine = _reset_gate_engine(reset_events=2)
    engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)  # COOLDOWN
    one = engine.evaluate(_snap(trade=_trade(price="100"), quote=_quote()), _permission(), now=NOW)
    assert one.status is TriggerStatus.COOLDOWN
    # stale trade가 끼어듦 → suppressed, reset_count 0으로
    engine.evaluate(
        _snap(trade=_trade(price="100"), quote=_quote(), trade_fresh=False),
        _permission(),
        now=NOW,
    )
    after = engine.evaluate(_snap(trade=_trade(price="100"), quote=_quote()), _permission(), now=NOW)
    assert after.status is TriggerStatus.COOLDOWN  # count restarted → still cooling
    assert after.state is TriggerState.COOLDOWN


def test_reset_with_fresh_trade_rearms_normally() -> None:
    # fresh reset 입력이면 정상 re-arm 한다(게이트가 정상 경로를 막지 않음).
    engine = _reset_gate_engine(reset_events=1)
    engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)  # COOLDOWN
    rearm = engine.evaluate(
        _snap(trade=_trade(price="100"), quote=_quote(ask="102")),  # condition false → ARMED
        _permission(),
        now=NOW,
    )
    assert rearm.status is TriggerStatus.CONDITION_NOT_MET
    assert rearm.state is TriggerState.ARMED


def test_reset_gate_does_not_block_initial_arm() -> None:
    # 초기 ARMED에서는 reset gate가 적용되지 않는다: trade가 없어도 suppress되지 않고
    # plan.rules(quote-only)만으로 판정된다.
    engine = _reset_gate_engine()
    res = engine.evaluate(_snap(trade=None, quote=_quote(ask="102")), _permission(), now=NOW)
    assert res.status is TriggerStatus.CONDITION_NOT_MET
    assert res.state is TriggerState.ARMED


def test_reset_rules_quote_metric_stale_is_fail_closed() -> None:
    # reset_rules가 quote metric을 읽어도 stale quote면 fail-closed(재무장 금지).
    engine = _reset_gate_engine(
        reset_rules=(_clause(Metric.BEST_BID_PRICE, Comparator.GTE, "1"),),
    )
    engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)  # COOLDOWN
    held = engine.evaluate(
        _snap(trade=_trade(price="100"), quote=_quote(), quote_fresh=False),
        _permission(),
        now=NOW,
    )
    assert held.status is TriggerStatus.SUPPRESSED
    assert held.state is TriggerState.COOLDOWN
    assert held.reason is TriggerReason.STALE_QUOTE


# --------------------------------------------------------------------------- #
# decision replacement (atomic)
# --------------------------------------------------------------------------- #
def test_replace_older_decision_rejected() -> None:
    engine = TriggerEngine()
    engine.replace_bundle(_bundle(created_at=NOW), now=NOW)
    result = engine.replace_bundle(
        _bundle(decision_id="analysis-260522-002", created_at=NOW - DAY), now=NOW
    )
    assert result.status.value == "rejected_older"


def test_replace_same_decision_unchanged() -> None:
    engine = TriggerEngine()
    engine.replace_bundle(_bundle(created_at=NOW), now=NOW)
    result = engine.replace_bundle(_bundle(created_at=NOW), now=NOW)
    assert result.status.value == "unchanged"


def test_replace_newer_decision_resets_state() -> None:
    engine = _armed_engine()
    engine.evaluate(_fireable_snap(), _permission(), now=NOW)  # LOCKED
    result = engine.replace_bundle(
        _bundle(decision_id="analysis-260522-002", created_at=NOW + DAY), now=NOW + DAY
    )
    assert result.status.value == "replaced"
    assert engine.state is TriggerState.ARMED


def test_buy_replaced_by_hold_disarms() -> None:
    engine = _armed_engine()
    engine.replace_bundle(
        _bundle(action=AnalysisAction.HOLD, decision_id="analysis-260522-002", created_at=NOW + DAY),
        now=NOW + DAY,
    )
    assert engine.state is TriggerState.DISARMED


def test_hold_replaced_by_buy_arms() -> None:
    engine = TriggerEngine()
    engine.replace_bundle(_bundle(action=AnalysisAction.HOLD), now=NOW)
    assert engine.state is TriggerState.DISARMED
    engine.replace_bundle(
        _bundle(action=AnalysisAction.BUY, decision_id="analysis-260522-002", created_at=NOW + DAY),
        now=NOW + DAY,
    )
    assert engine.state is TriggerState.ARMED


# --------------------------------------------------------------------------- #
# bundle / plan validation
# --------------------------------------------------------------------------- #
def test_bundle_rejects_hold_with_plan() -> None:
    decision = _decision(action=AnalysisAction.HOLD)
    with pytest.raises(ValidationError):
        DecisionTriggerBundle(decision=decision, plan=_plan())


def test_bundle_rejects_plan_action_mismatch() -> None:
    decision = _decision(action=AnalysisAction.BUY)
    sell_plan = _plan(action=AnalysisAction.SELL, rules=(_clause(Metric.LAST_TRADE_PRICE, Comparator.GTE, "100"),))
    with pytest.raises(ValidationError):
        DecisionTriggerBundle(decision=decision, plan=sell_plan)


def test_plan_rejects_hold_action() -> None:
    with pytest.raises(ValidationError):
        _plan(action=AnalysisAction.HOLD)


def test_plan_rejects_empty_rules() -> None:
    with pytest.raises(ValidationError):
        _plan(rules=())


def test_plan_rejects_inverted_window() -> None:
    with pytest.raises(ValidationError):
        _plan(created_at=NOW, valid_from=NOW, expires_at=NOW - DAY)


def test_plan_loads_from_json_fixture() -> None:
    payload = json.loads((_TRIGGER_FIXTURES / "buy_dip_plan.json").read_text(encoding="utf-8"))
    plan = TriggerPlan.model_validate(payload)
    assert plan.action is AnalysisAction.BUY
    assert plan.market is Market.KR
    assert len(plan.rules) == 2
    assert plan.debounce_events == 2
    assert plan.debounce_seconds == Decimal("1.5")
    assert plan.cooldown_seconds == Decimal("30")


# --------------------------------------------------------------------------- #
# hardening: snapshot identity gate (#1)
# --------------------------------------------------------------------------- #
def _other_snap(*, market: Market, symbol: str, price: str = "100") -> LatestMarketStateSnapshot:
    quote = NormalizedBestBidAsk(
        provider="kis", symbol=symbol, market=market, currency=Currency.KRW,
        bid_price="99", ask_price="101", bid_quantity="10", ask_quantity="10",
        quote_at=NOW, received_at=NOW,
        provider_sequence=ProviderSequence(provider="kis", channel="q", sequence=1, received_at=NOW),
    )
    trade = NormalizedTradeTick(
        provider="kis", symbol=symbol, market=market, currency=Currency.KRW,
        price=price, quantity="1", trade_at=NOW, received_at=NOW,
        provider_sequence=ProviderSequence(provider="kis", channel="t", sequence=1, received_at=NOW),
    )
    return LatestMarketStateSnapshot(
        market=market, symbol=symbol, trade=trade, quote=quote, trade_fresh=True,
        quote_fresh=True, evaluated_at=NOW,
    )


def test_wrong_symbol_snapshot_is_suppressed_no_state_change() -> None:
    engine = _armed_engine()
    snap = _other_snap(market=Market.KR, symbol="000660")
    result = engine.evaluate(snap, _permission(), now=NOW)
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.SNAPSHOT_SYMBOL_MISMATCH
    assert engine.state is TriggerState.ARMED  # never advanced to DEBOUNCING/LOCKED
    # the correct instrument still fires afterwards
    ok = engine.evaluate(_fireable_snap(), _permission(), now=NOW)
    assert ok.status is TriggerStatus.TRIGGERED


def test_wrong_market_snapshot_is_suppressed() -> None:
    engine = _armed_engine()
    snap = _other_snap(market=Market.US, symbol="005930")
    result = engine.evaluate(snap, _permission(), now=NOW)
    assert result.reason is TriggerReason.SNAPSHOT_MARKET_MISMATCH


def test_snapshot_key_matches_but_inner_quote_belongs_to_other_symbol() -> None:
    engine = _armed_engine()
    # snapshot key says 005930 but the quote slot carries a foreign symbol
    foreign_quote = NormalizedBestBidAsk(
        provider="kis", symbol="000660", market=Market.KR, currency=Currency.KRW,
        bid_price="99", ask_price="101", bid_quantity="10", ask_quantity="10",
        quote_at=NOW, received_at=NOW,
        provider_sequence=ProviderSequence(provider="kis", channel="q", sequence=1, received_at=NOW),
    )
    snap = _snap(trade=_trade(), quote=foreign_quote)
    result = engine.evaluate(snap, _permission(), now=NOW)
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.SNAPSHOT_SYMBOL_MISMATCH


# --------------------------------------------------------------------------- #
# hardening: bundle conflict under same identity (#2)
# --------------------------------------------------------------------------- #
def test_same_identity_different_plan_is_conflict_not_unchanged() -> None:
    engine = TriggerEngine()
    p1 = _plan(rules=(_clause(Metric.LAST_TRADE_PRICE, Comparator.LTE, "70000"),))
    p2 = _plan(rules=(_clause(Metric.LAST_TRADE_PRICE, Comparator.LTE, "65000"),))
    engine.replace_bundle(_bundle(plan=p1), now=NOW)
    result = engine.replace_bundle(_bundle(plan=p2), now=NOW)
    assert result.status is ReplaceStatus.REJECTED_CONFLICT


def test_same_identity_identical_content_is_unchanged() -> None:
    engine = TriggerEngine()
    engine.replace_bundle(_bundle(), now=NOW)
    result = engine.replace_bundle(_bundle(), now=NOW)
    assert result.status is ReplaceStatus.UNCHANGED


def test_same_created_at_different_id_is_conflict() -> None:
    engine = TriggerEngine()
    engine.replace_bundle(_bundle(decision_id="analysis-260522-001", created_at=NOW), now=NOW)
    result = engine.replace_bundle(
        _bundle(decision_id="analysis-260522-999", created_at=NOW), now=NOW
    )
    assert result.status is ReplaceStatus.REJECTED_CONFLICT


# --------------------------------------------------------------------------- #
# hardening: permission time window (#3)
# --------------------------------------------------------------------------- #
def test_future_permission_not_yet_valid() -> None:
    engine = _armed_engine()
    future = _permission(checked_at=NOW + DAY, valid_until=NOW + 2 * DAY)
    result = engine.evaluate(_fireable_snap(), future, now=NOW)
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.PERMISSION_NOT_YET_VALID


def test_permission_checked_after_valid_until_rejected() -> None:
    with pytest.raises(ValidationError):
        TradingPermission(
            market=Market.KR, allowed=True,
            checked_at=NOW + 2 * DAY, valid_until=NOW + DAY, reason_code="x",
        )


def test_permission_boundary_now_equals_checked_at_and_valid_until() -> None:
    engine = _armed_engine()
    at_open = _permission(checked_at=NOW, valid_until=NOW)
    result = engine.evaluate(_fireable_snap(), at_open, now=NOW)
    assert result.status is TriggerStatus.TRIGGERED


# --------------------------------------------------------------------------- #
# hardening: cooldown reset continuity across suppression (#5)
# --------------------------------------------------------------------------- #
def test_suppression_gap_resets_cooldown_reset_counter() -> None:
    engine = _armed_engine(max_fires_per_decision=2, cooldown_seconds="0", reset_events=2)
    engine.evaluate(_fireable_snap(price="100"), _permission(), now=NOW)  # fire1 → COOLDOWN
    engine.evaluate(_fireable_snap(price="101"), _permission(), now=NOW)  # reset-true #1
    # a data gap (stale quote) must break the consecutive-reset streak
    stale = _snap(trade=_trade(), quote=_quote(), quote_fresh=False)
    gap = engine.evaluate(stale, _permission(), now=NOW)
    assert gap.status is TriggerStatus.SUPPRESSED
    # one reset-true after the gap is NOT enough (counter restarted): still cooling
    after = engine.evaluate(_fireable_snap(price="101"), _permission(), now=NOW)
    assert after.status is TriggerStatus.COOLDOWN
    assert after.state is TriggerState.COOLDOWN


# --------------------------------------------------------------------------- #
# hardening: plan/decision time binding (#6)
# --------------------------------------------------------------------------- #
def test_plan_predating_decision_rejected() -> None:
    decision_created = NOW
    early_plan = _plan(
        created_at=NOW - DAY, valid_from=NOW - DAY, expires_at=NOW + DAY
    )
    with pytest.raises(ValidationError):
        DecisionTriggerBundle(decision=_decision(created_at=decision_created), plan=early_plan)


# --------------------------------------------------------------------------- #
# refuted (#4): non-finite Decimal already rejected by pydantic core
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_threshold_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        ConditionClause(metric=Metric.LAST_TRADE_PRICE, comparator=Comparator.LTE, threshold=bad)


@pytest.mark.parametrize("bad", ["NaN", "Infinity"])
def test_non_finite_seconds_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        _plan(debounce_seconds=bad)


# --------------------------------------------------------------------------- #
# concurrency: lock guarantees exactly one fire
# --------------------------------------------------------------------------- #
def test_concurrent_evaluations_fire_exactly_once() -> None:
    engine = _armed_engine()
    n = 16
    barrier = threading.Barrier(n)
    results: list[TriggerStatus] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        res = engine.evaluate(_fireable_snap(), _permission(), now=NOW)
        with results_lock:
            results.append(res.status)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(TriggerStatus.TRIGGERED) == 1
    assert results.count(TriggerStatus.ALREADY_FIRED) == n - 1


# --------------------------------------------------------------------------- #
# RTM-4b.2 hardening: as-of staleness (fail-closed) + bool rejection
# --------------------------------------------------------------------------- #
def test_reused_stale_snapshot_is_suppressed_not_fired() -> None:
    # snapshot peeked at NOW, but engine ticks at NOW+1h (still within permission/
    # decision validity). trade_fresh/quote_fresh were computed at NOW and would be
    # trusted blindly; the as-of gate must suppress instead of firing.
    engine = _armed_engine()
    stale = _fireable_snap(at=NOW)  # evaluated_at=NOW
    result = engine.evaluate(stale, _permission(), now=NOW + timedelta(hours=1))
    assert result.status is TriggerStatus.SUPPRESSED
    assert result.reason is TriggerReason.STALE_SNAPSHOT
    assert result.signal is None
    assert engine.state is TriggerState.ARMED  # never advanced


def test_fresh_snapshot_at_exact_now_still_fires() -> None:
    # control: same tick, snapshot evaluated_at == now → as-of gate passes.
    engine = _armed_engine()
    at = NOW + timedelta(hours=1)
    fresh = _fireable_snap(at=at)
    result = engine.evaluate(fresh, _permission(), now=at)
    assert result.status is TriggerStatus.TRIGGERED


@pytest.mark.parametrize("metric", list(Metric))
def test_bool_threshold_rejected_for_all_metrics(metric: Metric) -> None:
    for value in (True, False):
        with pytest.raises(ValidationError):
            ConditionClause(metric=metric, comparator=Comparator.GTE, threshold=value)


@pytest.mark.parametrize("field", ["debounce_seconds", "cooldown_seconds"])
def test_bool_seconds_rejected(field: str) -> None:
    for value in (True, False):
        with pytest.raises(ValidationError):
            _plan(**{field: value})


@pytest.mark.parametrize(
    "field", ["debounce_events", "reset_events", "max_fires_per_decision"]
)
def test_bool_count_fields_rejected(field: str) -> None:
    # bool is an int subclass; True/False must not coerce to 1/0 past the >= 1 gate.
    for value in (True, False):
        with pytest.raises(ValidationError):
            _plan(**{field: value})


@pytest.mark.parametrize(
    "field", ["debounce_events", "reset_events", "max_fires_per_decision"]
)
def test_int_count_fields_still_accept_valid_values(field: str) -> None:
    for value in (1, 2, 5):
        plan = _plan(**{field: value})
        assert getattr(plan, field) == value


@pytest.mark.parametrize(
    "field", ["debounce_events", "reset_events", "max_fires_per_decision"]
)
def test_int_count_fields_still_reject_zero_and_negative(field: str) -> None:
    for value in (0, -1):
        with pytest.raises(ValidationError):
            _plan(**{field: value})
