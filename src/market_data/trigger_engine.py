"""RTM-4a — deterministic trigger engine.

목적 한 줄: 최신 시장 상태와 검증된 의사결정을 받아, 조건이 충족됐을 때 단 한 번의
결정론적 TriggerSignal을 만든다. 주문/수량/PaperBroker/ledger는 호출하지 않는다(RTM-5).

핵심 계약:
  - HOLD는 절대 실행 후보가 아니다(가장 먼저 차단).
  - decision/plan/permission/quote가 stale·missing이면 fail-closed로 SUPPRESS한다.
  - debounce/cooldown/reset/idempotency를 상태기계로 구현한다.
  - active decision 교체와 trigger state는 동일 threading.Lock으로 원자화한다.
  - TriggerSignal만 반환하고 broker/ledger/paper_loop/LLM/network는 호출하지 않는다.
  - idempotency key는 wall-clock/price/raw payload를 포함하지 않는다(동일 activation은 동일 key).

market-hours 권한은 여기서 계산하지 않는다(RTM-7 calendar 유보). 외부에서 생성된
TradingPermission을 주입받아 gate만 한다. permission이 없으면 기본 deny다.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from typing_extensions import Self

from analysis.models import AnalysisAction, AnalysisDecision
from domain._datetime import (
    parse_timezone_aware_datetime,
    require_timezone_aware_datetime,
)
from domain._strings import normalize_required_string
from domain.enums import Market
from domain.identifiers import DecisionId
from market_data.conditions import (
    ConditionClause,
    Metric,
    evaluate_all,
    metric_value,
    rule_required_slots,
    rule_required_windows,
)
from market_data.indicators import (
    IndicatorContext,
    IndicatorReadiness,
    IndicatorWindowSnapshot,
    canonical_window_payload,
)
from market_data.latest_state import LatestMarketStateSnapshot
from market_data.models import NormalizedTradeTick

__all__ = [
    "TriggerPlan",
    "TradingPermission",
    "TriggerSignal",
    "ConditionObservation",
    "DecisionTriggerBundle",
    "TriggerState",
    "TriggerStatus",
    "TriggerReason",
    "TriggerEvaluation",
    "ReplaceStatus",
    "ReplaceResult",
    "TriggerEngine",
]

_EXECUTABLE_ACTIONS = frozenset({AnalysisAction.BUY, AnalysisAction.SELL})


class TriggerPlan(BaseModel):
    """장중 진입 조건을 담는 구조화 schema. AnalysisDecision의 prose에서 조건을
    추론하지 않고, 조건은 반드시 이 별도 schema로 제공한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    decision_id: DecisionId
    created_at: datetime
    valid_from: datetime
    expires_at: datetime
    universe: str
    market: Market
    symbol: str
    action: AnalysisAction
    rules: tuple[ConditionClause, ...]
    reset_rules: tuple[ConditionClause, ...] = ()
    debounce_events: int = 1
    debounce_seconds: Decimal = Decimal("0")
    reset_events: int = 1
    cooldown_seconds: Decimal = Decimal("0")
    max_fires_per_decision: int = 1

    @field_validator("plan_id", "universe", "symbol", mode="before")
    @classmethod
    def _normalize_strings(cls, value: object, info: object) -> str:
        return normalize_required_string(value, field_name=info.field_name)  # type: ignore[attr-defined]

    @field_validator("created_at", "valid_from", "expires_at", mode="before")
    @classmethod
    def _parse_times(cls, value: object, info: object) -> datetime:
        return parse_timezone_aware_datetime(value, field_name=info.field_name)  # type: ignore[attr-defined]

    @field_validator("debounce_seconds", "cooldown_seconds", mode="before")
    @classmethod
    def _coerce_decimal(cls, value: object) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (str, int)):
            return Decimal(value)
        raise ValueError("seconds must be a Decimal, str, or int.")

    @model_validator(mode="after")
    def _validate_invariants(self) -> Self:
        if self.action not in _EXECUTABLE_ACTIONS:
            raise ValueError("TriggerPlan.action must be BUY or SELL (HOLD is non-executable).")
        if not self.rules:
            raise ValueError("TriggerPlan.rules must not be empty.")
        if self.created_at > self.valid_from:
            raise ValueError("created_at must be <= valid_from.")
        if self.valid_from > self.expires_at:
            raise ValueError("valid_from must be <= expires_at.")
        if self.debounce_events < 1:
            raise ValueError("debounce_events must be >= 1.")
        if self.reset_events < 1:
            raise ValueError("reset_events must be >= 1.")
        if self.debounce_seconds < 0:
            raise ValueError("debounce_seconds must be >= 0.")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0.")
        if self.max_fires_per_decision < 1:
            raise ValueError("max_fires_per_decision must be >= 1.")
        return self


class TradingPermission(BaseModel):
    """외부(RTM-7 scheduler 등)가 생성하는 거래 허가. 엔진은 계산하지 않고 gate만 한다.
    기본값은 deny이며, permission이 없으면 실행하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: Market
    allowed: bool
    checked_at: datetime
    valid_until: datetime
    reason_code: str

    @field_validator("reason_code", mode="before")
    @classmethod
    def _normalize_reason(cls, value: object) -> str:
        return normalize_required_string(value, field_name="reason_code")

    @field_validator("checked_at", "valid_until", mode="before")
    @classmethod
    def _parse_times(cls, value: object, info: object) -> datetime:
        return parse_timezone_aware_datetime(value, field_name=info.field_name)  # type: ignore[attr-defined]

    @model_validator(mode="after")
    def _validate_window(self) -> Self:
        if self.checked_at > self.valid_until:
            raise ValueError("checked_at must be <= valid_until.")
        return self


class ConditionObservation(BaseModel):
    """trigger 시점에 각 rule metric이 가졌던 값. evidence/감사용이며 raw payload가 아니다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: Metric
    value: Decimal
    # rolling metric은 어느 window의 값인지 evidence만으로 구분 가능해야 한다.
    # latest metric은 None(후방호환).
    window_id: str | None = None


class TriggerSignal(BaseModel):
    """조건 충족 결과. 주문이 아니다. quantity/side/account/broker/credential을 담지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger_id: str
    idempotency_key: str
    decision_id: DecisionId
    plan_id: str
    market: Market
    symbol: str
    action: AnalysisAction
    reference_price: Decimal
    triggered_at: datetime
    condition_values: tuple[ConditionObservation, ...]


class DecisionTriggerBundle(BaseModel):
    """검증된 AnalysisDecision + (BUY/SELL이면) TriggerPlan 묶음. HOLD면 plan은 None이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: AnalysisDecision
    plan: TriggerPlan | None

    @property
    def action(self) -> AnalysisAction:
        return self.decision.fund_manager.action

    @model_validator(mode="after")
    def _validate_match(self) -> Self:
        action = self.decision.fund_manager.action
        if action is AnalysisAction.HOLD:
            if self.plan is not None:
                raise ValueError("HOLD decision must not carry a TriggerPlan.")
            return self
        # BUY/SELL: plan required and must match the decision identity.
        plan = self.plan
        if plan is None:
            raise ValueError("BUY/SELL decision requires a TriggerPlan.")
        if plan.decision_id != self.decision.decision_id:
            raise ValueError("plan.decision_id must equal decision.decision_id.")
        if plan.market.value != self.decision.market:
            raise ValueError("plan.market must equal decision.market.")
        if plan.symbol != self.decision.symbol:
            raise ValueError("plan.symbol must equal decision.symbol.")
        if plan.universe != self.decision.universe:
            raise ValueError("plan.universe must equal decision.universe.")
        if plan.action != action:
            raise ValueError("plan.action must equal decision.fund_manager.action.")
        # a plan cannot predate the decision it executes (time binding).
        if plan.created_at < self.decision.created_at:
            raise ValueError("plan.created_at must be >= decision.created_at.")
        if plan.valid_from < self.decision.created_at:
            raise ValueError("plan.valid_from must be >= decision.created_at.")
        return self


class TriggerState(StrEnum):
    DISARMED = "disarmed"
    ARMED = "armed"
    DEBOUNCING = "debouncing"
    TRIGGERED = "triggered"
    COOLDOWN = "cooldown"
    LOCKED = "locked"


class TriggerStatus(StrEnum):
    TRIGGERED = "triggered"
    CONDITION_NOT_MET = "condition_not_met"
    DEBOUNCING = "debouncing"
    COOLDOWN = "cooldown"
    ALREADY_FIRED = "already_fired"
    SUPPRESSED = "suppressed"


class TriggerReason(StrEnum):
    HOLD_ACTION = "hold_action"
    NO_ACTIVE_DECISION = "no_active_decision"
    DECISION_NOT_YET_VALID = "decision_not_yet_valid"
    STALE_DECISION = "stale_decision"
    TRADING_NOT_ALLOWED = "trading_not_allowed"
    STALE_PERMISSION = "stale_permission"
    PERMISSION_NOT_YET_VALID = "permission_not_yet_valid"
    PERMISSION_MARKET_MISMATCH = "permission_market_mismatch"
    SNAPSHOT_MARKET_MISMATCH = "snapshot_market_mismatch"
    SNAPSHOT_SYMBOL_MISMATCH = "snapshot_symbol_mismatch"
    MISSING_TRADE = "missing_trade"
    STALE_TRADE = "stale_trade"
    MISSING_QUOTE = "missing_quote"
    STALE_QUOTE = "stale_quote"
    CONDITION_FALSE = "condition_false"
    DEBOUNCE_PENDING = "debounce_pending"
    COOLDOWN_ACTIVE = "cooldown_active"
    MAX_FIRES_REACHED = "max_fires_reached"
    # RTM-4b.2 rolling-indicator gating (fail-closed).
    MISSING_INDICATOR = "missing_indicator"
    INDICATOR_WARMING = "indicator_warming"
    INDICATOR_DISCONTINUOUS = "indicator_discontinuous"
    INDICATOR_STALE = "indicator_stale"
    INDICATOR_FUTURE = "indicator_future"
    INDICATOR_INSUFFICIENT_RETENTION = "indicator_insufficient_retention"
    INDICATOR_IDENTITY_MISMATCH = "indicator_identity_mismatch"
    INDICATOR_LAGGING = "indicator_lagging"


@dataclass(frozen=True)
class TriggerEvaluation:
    status: TriggerStatus
    state: TriggerState
    reason: TriggerReason | None = None
    signal: TriggerSignal | None = None


class ReplaceStatus(StrEnum):
    REPLACED = "replaced"
    UNCHANGED = "unchanged"
    REJECTED_OLDER = "rejected_older"
    REJECTED_CONFLICT = "rejected_conflict"


@dataclass(frozen=True)
class ReplaceResult:
    status: ReplaceStatus
    state: TriggerState


def _canonical_rule(clause: ConditionClause) -> dict[str, object | None]:
    return {
        "metric": clause.metric.value,
        "comparator": clause.comparator.value,
        "threshold": str(clause.threshold),
        # rolling window는 정규화 payload(60≡60.0)를 포함해 rule_set_id에 반영한다.
        # SMA 20틱 vs 60틱 vs 60초가 서로 다른 rule_set_id/idempotency를 갖게 한다.
        "window": canonical_window_payload(clause.window) if clause.window is not None else None,
    }


def _rule_set_id(plan: TriggerPlan) -> str:
    payload = {
        "rules": [_canonical_rule(c) for c in plan.rules],
        "reset_rules": [_canonical_rule(c) for c in plan.reset_rules],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _bundle_fingerprint(bundle: DecisionTriggerBundle) -> str:
    """decision + plan의 canonical 내용 해시. 동일 identity(decision_id+created_at)인데
    내용이 다른 교체를 silent UNCHANGED로 삼키지 않고 충돌로 감지하기 위함이다."""
    payload = {
        "decision": bundle.decision.model_dump(mode="json"),
        "plan": bundle.plan.model_dump(mode="json") if bundle.plan is not None else None,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _idempotency_key(
    *,
    decision_id: DecisionId,
    plan_id: str,
    rule_set_id: str,
    market: Market,
    symbol: str,
    action: AnalysisAction,
    activation_epoch: int,
) -> str:
    payload = {
        "decision_id": decision_id.value,
        "plan_id": plan_id,
        "rule_set_id": rule_set_id,
        "market": market.value,
        "symbol": symbol,
        "action": action.value,
        "activation_epoch": activation_epoch,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class TriggerEngine:
    """active decision 교체와 trigger 평가를 동일 lock으로 원자화하는 인메모리 엔진.

    lock 안에서는 network/file I/O/callback/await/broker를 호출하지 않는다(순수 계산).
    reader가 old decision + new state의 혼합을 보지 않도록 replace/evaluate가 같은 lock을
    공유한다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: DecisionTriggerBundle | None = None
        self._state = TriggerState.DISARMED
        self._rule_set_id = ""
        self._fingerprint = ""
        self._activation_epoch = 0
        self._debounce_count = 0
        self._first_true_at: datetime | None = None
        self._reset_count = 0
        self._cooldown_until: datetime | None = None
        self._fires = 0

    @property
    def state(self) -> TriggerState:
        with self._lock:
            return self._state

    def replace_bundle(self, bundle: DecisionTriggerBundle, *, now: datetime) -> ReplaceResult:
        """active decision을 원자적으로 교체한다.

        규칙(fail-closed): 동일 decision_id+created_at이면 내용 fingerprint가 같을 때만
        UNCHANGED, 다르면 REJECTED_CONFLICT(같은 정체성에 다른 plan/payload를 silent로
        삼키지 않는다). created_at이 같은데 decision_id가 다르면 newer/older 판단이
        불가능하므로 REJECTED_CONFLICT. older(created_at <)는 REJECTED_OLDER. newer만
        atomic replace한다. HOLD는 DISARMED, BUY/SELL은 ARMED로 두고 debounce/cooldown/
        reset/fires 상태를 모두 초기화한다(reader가 혼합 상태를 보지 않게 lock 안에서 수행)."""
        require_timezone_aware_datetime(now, field_name="now")
        incoming_fingerprint = _bundle_fingerprint(bundle)
        with self._lock:
            current = self._active
            incoming = bundle.decision
            if current is not None:
                existing = current.decision
                same_id = incoming.decision_id == existing.decision_id
                same_time = incoming.created_at == existing.created_at
                if same_id and same_time:
                    if incoming_fingerprint == self._fingerprint:
                        return ReplaceResult(ReplaceStatus.UNCHANGED, self._state)
                    return ReplaceResult(ReplaceStatus.REJECTED_CONFLICT, self._state)
                if same_time and not same_id:
                    # 동일 시각의 서로 다른 결정 — 순서를 정할 수 없어 fail-closed 거부.
                    return ReplaceResult(ReplaceStatus.REJECTED_CONFLICT, self._state)
                if incoming.created_at < existing.created_at:
                    return ReplaceResult(ReplaceStatus.REJECTED_OLDER, self._state)
            self._install(bundle, incoming_fingerprint)
            return ReplaceResult(ReplaceStatus.REPLACED, self._state)

    def _install(self, bundle: DecisionTriggerBundle, fingerprint: str) -> None:
        self._active = bundle
        self._fingerprint = fingerprint
        self._activation_epoch += 1
        self._debounce_count = 0
        self._first_true_at = None
        self._reset_count = 0
        self._cooldown_until = None
        self._fires = 0
        if bundle.plan is None:  # HOLD
            self._state = TriggerState.DISARMED
            self._rule_set_id = ""
        else:
            self._state = TriggerState.ARMED
            self._rule_set_id = _rule_set_id(bundle.plan)

    def evaluate(
        self,
        snapshot: LatestMarketStateSnapshot,
        permission: TradingPermission | None,
        *,
        now: datetime,
        indicators: IndicatorContext | None = None,
    ) -> TriggerEvaluation:
        """현재 상태/스냅샷/권한으로 단 한 번의 결정론적 평가를 수행한다.

        price-only plan은 indicators 없이 RTM-4a와 동일하게 동작한다. rolling rule이 있는
        plan은 IndicatorContext가 필수이며, 엔진은 store를 직접 읽지 않는다(주입 입력만 사용).
        정상적인 suppression은 예외가 아니라 typed result로 반환한다(fail-closed)."""
        require_timezone_aware_datetime(now, field_name="now")
        with self._lock:
            return self._evaluate_locked(snapshot, permission, now, indicators)

    def _evaluate_locked(
        self,
        snapshot: LatestMarketStateSnapshot,
        permission: TradingPermission | None,
        now: datetime,
        indicators: IndicatorContext | None,
    ) -> TriggerEvaluation:
        bundle = self._active
        if bundle is None:
            return self._suppressed(TriggerReason.NO_ACTIVE_DECISION)
        if bundle.plan is None:  # HOLD: never executable
            return TriggerEvaluation(
                TriggerStatus.SUPPRESSED, TriggerState.DISARMED, TriggerReason.HOLD_ACTION
            )
        plan = bundle.plan

        if self._state is TriggerState.LOCKED:
            return TriggerEvaluation(
                TriggerStatus.ALREADY_FIRED,
                TriggerState.LOCKED,
                TriggerReason.MAX_FIRES_REACHED,
            )

        # --- decision freshness window ---
        if now < plan.valid_from:
            return self._suppress_break(TriggerReason.DECISION_NOT_YET_VALID)
        if now > plan.expires_at:
            return self._suppress_break(TriggerReason.STALE_DECISION)

        # --- snapshot identity gate: never evaluate another instrument's prices ---
        if snapshot.market != plan.market:
            return self._suppress_break(TriggerReason.SNAPSHOT_MARKET_MISMATCH)
        if snapshot.symbol != plan.symbol:
            return self._suppress_break(TriggerReason.SNAPSHOT_SYMBOL_MISMATCH)

        # --- trading permission (default deny) ---
        if permission is None:
            return self._suppress_break(TriggerReason.TRADING_NOT_ALLOWED)
        if permission.market != plan.market:
            return self._suppress_break(TriggerReason.PERMISSION_MARKET_MISMATCH)
        if not permission.allowed:
            return self._suppress_break(TriggerReason.TRADING_NOT_ALLOWED)
        if now < permission.checked_at:
            return self._suppress_break(TriggerReason.PERMISSION_NOT_YET_VALID)
        if now > permission.valid_until:
            return self._suppress_break(TriggerReason.STALE_PERMISSION)

        # --- market-state freshness (BUY/SELL always need a fresh quote) ---
        needs_trade, _ = rule_required_slots(plan.rules)
        if snapshot.quote is None:
            return self._suppress_break(TriggerReason.MISSING_QUOTE)
        # inner-slot identity: the quote/trade must belong to the planned instrument.
        if snapshot.quote.market != plan.market or snapshot.quote.symbol != plan.symbol:
            return self._suppress_break(TriggerReason.SNAPSHOT_SYMBOL_MISMATCH)
        if not snapshot.quote_fresh:
            return self._suppress_break(TriggerReason.STALE_QUOTE)
        if needs_trade:
            if snapshot.trade is None:
                return self._suppress_break(TriggerReason.MISSING_TRADE)
            if snapshot.trade.market != plan.market or snapshot.trade.symbol != plan.symbol:
                return self._suppress_break(TriggerReason.SNAPSHOT_SYMBOL_MISMATCH)
            if not snapshot.trade_fresh:
                return self._suppress_break(TriggerReason.STALE_TRADE)

        # --- rolling indicator readiness + latest/indicator coherence (F4/F5) ---
        indicator_gate = self._indicator_gate(plan, plan.rules, snapshot, indicators)
        if indicator_gate is not None:
            return self._suppress_break(indicator_gate)

        condition_true = evaluate_all(plan.rules, snapshot, indicators=indicators)

        # --- cooldown: re-arm only after cooldown elapsed AND reset satisfied ---
        if self._state is TriggerState.COOLDOWN:
            # reset_rules freshness gate (F2): re-arm은 신선한 reset 입력에서만 허용한다.
            # plan.rules의 slot은 위에서 이미 검증했지만, reset_rules가 추가로 읽는
            # trade/quote slot(예: quote-only rules + trade-only reset_rules)은 거기서
            # 검증되지 않는다. missing/stale/identity-mismatch면 reset을 평가하지 않고
            # _suppress_break로 빠진다 — stale 입력으로 같은 tick에 re-arm/fire하지 않게.
            # _suppress_break는 COOLDOWN에서 누적 reset_count를 0으로 되돌린다(스트림
            # 단절을 사이에 둔 비연속 reset이 연속으로 오인되지 않게).
            gate = self._reset_slot_gate(plan, snapshot)
            if gate is not None:
                return self._suppress_break(gate)
            # reset_rules의 rolling indicator도 readiness/coherence를 gate한다(F2 확장).
            # missing/warming/discontinuous/stale/future/insufficient/identity/lag면
            # _suppress_break로 빠지며 _reset_count가 0으로 리셋된다(stale 입력 re-arm 방지).
            reset_indicator_gate = self._indicator_gate(
                plan, plan.reset_rules, snapshot, indicators
            )
            if reset_indicator_gate is not None:
                return self._suppress_break(reset_indicator_gate)
            reset_true = (
                evaluate_all(plan.reset_rules, snapshot, indicators=indicators)
                if plan.reset_rules
                else not condition_true
            )
            self._reset_count = self._reset_count + 1 if reset_true else 0
            cooldown_done = self._cooldown_until is None or now >= self._cooldown_until
            if (
                cooldown_done
                and self._reset_count >= plan.reset_events
                and self._fires < plan.max_fires_per_decision
            ):
                self._state = TriggerState.ARMED
                self._activation_epoch += 1
                self._debounce_count = 0
                self._first_true_at = None
                self._reset_count = 0
                self._cooldown_until = None
                # fall through to ARMED handling on this same tick
            else:
                return TriggerEvaluation(
                    TriggerStatus.COOLDOWN, TriggerState.COOLDOWN, TriggerReason.COOLDOWN_ACTIVE
                )

        if self._state is TriggerState.ARMED:
            if not condition_true:
                return TriggerEvaluation(
                    TriggerStatus.CONDITION_NOT_MET,
                    TriggerState.ARMED,
                    TriggerReason.CONDITION_FALSE,
                )
            self._state = TriggerState.DEBOUNCING
            self._debounce_count = 1
            self._first_true_at = now
        elif self._state is TriggerState.DEBOUNCING:
            if not condition_true:
                self._state = TriggerState.ARMED
                self._debounce_count = 0
                self._first_true_at = None
                return TriggerEvaluation(
                    TriggerStatus.CONDITION_NOT_MET,
                    TriggerState.ARMED,
                    TriggerReason.CONDITION_FALSE,
                )
            self._debounce_count += 1

        # --- debounce satisfaction (events AND seconds) ---
        events_ok = self._debounce_count >= plan.debounce_events
        seconds_ok = self._first_true_at is not None and Decimal(
            str((now - self._first_true_at).total_seconds())
        ) >= plan.debounce_seconds
        if not (events_ok and seconds_ok):
            return TriggerEvaluation(
                TriggerStatus.DEBOUNCING, TriggerState.DEBOUNCING, TriggerReason.DEBOUNCE_PENDING
            )

        return self._fire(plan, snapshot, now, indicators)

    def _reset_slot_gate(
        self, plan: TriggerPlan, snapshot: LatestMarketStateSnapshot
    ) -> TriggerReason | None:
        """reset_rules가 요구하는 trade/quote slot의 존재·정체성·freshness를 검사한다.
        통과하면 None, 아니면 suppress 사유를 반환한다. reset_rules가 없으면(=기본
        not-condition reset) gate할 입력이 없으므로 None. quote는 _fire의 reference
        price 때문에 항상 위에서 검증되지만, reset_rules가 trade를 추가로 요구하면
        그 trade slot은 여기서 처음 검증된다."""
        if not plan.reset_rules:
            return None
        needs_trade, needs_quote = rule_required_slots(plan.reset_rules)
        if needs_quote:
            if snapshot.quote is None:
                return TriggerReason.MISSING_QUOTE
            if snapshot.quote.market != plan.market or snapshot.quote.symbol != plan.symbol:
                return TriggerReason.SNAPSHOT_SYMBOL_MISMATCH
            if not snapshot.quote_fresh:
                return TriggerReason.STALE_QUOTE
        if needs_trade:
            if snapshot.trade is None:
                return TriggerReason.MISSING_TRADE
            if snapshot.trade.market != plan.market or snapshot.trade.symbol != plan.symbol:
                return TriggerReason.SNAPSHOT_SYMBOL_MISMATCH
            if not snapshot.trade_fresh:
                return TriggerReason.STALE_TRADE
        return None

    def _indicator_gate(
        self,
        plan: TriggerPlan,
        rules: tuple[ConditionClause, ...],
        snapshot: LatestMarketStateSnapshot,
        indicators: IndicatorContext | None,
    ) -> TriggerReason | None:
        """rules가 요구하는 rolling window의 존재·readiness·최신 trade와의 coherence를
        검사한다. 통과하면 None, 아니면 fail-closed suppress 사유를 반환한다.

        price-only rules면 요구 window가 없어 None. rolling rule이 있으면 IndicatorContext가
        필수이며, context identity·각 window readiness·latest trade와의 정확한 coherence를
        모두 만족해야 한다. 엔진은 store를 읽지 않고 주입된 context만 본다."""
        required = rule_required_windows(rules)
        if not required:
            return None
        if indicators is None:
            return TriggerReason.MISSING_INDICATOR
        if indicators.market != plan.market or indicators.symbol != plan.symbol:
            return TriggerReason.INDICATOR_IDENTITY_MISMATCH
        # rolling metric은 rule_required_slots에서 needs_trade를 강제하므로 정상 경로에서는
        # 최신 trade가 이미 검증됐다. 방어적으로 한 번 더 확인한다.
        trade = snapshot.trade
        if trade is None:
            return TriggerReason.MISSING_TRADE
        for spec in required:
            window = indicators.get(spec.window_id)
            if window is None:
                return TriggerReason.MISSING_INDICATOR
            readiness_reason = _READINESS_REASON.get(window.readiness)
            if readiness_reason is not None:
                return readiness_reason
            coherence_reason = _coherence_reason(window, trade)
            if coherence_reason is not None:
                return coherence_reason
        return None

    def _fire(
        self,
        plan: TriggerPlan,
        snapshot: LatestMarketStateSnapshot,
        now: datetime,
        indicators: IndicatorContext | None,
    ) -> TriggerEvaluation:
        assert snapshot.quote is not None
        reference_price = (
            snapshot.quote.ask_price
            if plan.action is AnalysisAction.BUY
            else snapshot.quote.bid_price
        )
        observations = tuple(
            ConditionObservation(
                metric=c.metric,
                value=_observed(c.metric, snapshot, indicators, c.window),
                window_id=c.window.window_id if c.window is not None else None,
            )
            for c in plan.rules
        )
        key = _idempotency_key(
            decision_id=plan.decision_id,
            plan_id=plan.plan_id,
            rule_set_id=self._rule_set_id,
            market=plan.market,
            symbol=plan.symbol,
            action=plan.action,
            activation_epoch=self._activation_epoch,
        )
        signal = TriggerSignal(
            trigger_id=key,
            idempotency_key=key,
            decision_id=plan.decision_id,
            plan_id=plan.plan_id,
            market=plan.market,
            symbol=plan.symbol,
            action=plan.action,
            reference_price=reference_price,
            triggered_at=now,
            condition_values=observations,
        )
        self._fires += 1
        if self._fires >= plan.max_fires_per_decision:
            self._state = TriggerState.LOCKED
        else:
            self._state = TriggerState.COOLDOWN
            self._cooldown_until = now + _seconds(plan.cooldown_seconds)
            self._reset_count = 0
            self._debounce_count = 0
            self._first_true_at = None
        return TriggerEvaluation(TriggerStatus.TRIGGERED, self._state, None, signal)

    def _suppressed(self, reason: TriggerReason) -> TriggerEvaluation:
        return TriggerEvaluation(TriggerStatus.SUPPRESSED, self._state, reason)

    def _suppress_break(self, reason: TriggerReason) -> TriggerEvaluation:
        """조건/데이터 스트림이 끊긴 suppress(stale/missing/permission/identity/not-yet-valid).
        debounce 진행 중이었다면 counter를 리셋하고 ARMED로 되돌린다(§9). COOLDOWN 중이면
        rearm은 '연속' reset event를 요구하므로 끊긴 동안 누적된 reset_count도 0으로 리셋한다
        (스트림 단절을 사이에 두고 비연속 reset이 연속으로 오인되지 않게). locked는 유지."""
        if self._state is TriggerState.DEBOUNCING:
            self._state = TriggerState.ARMED
            self._debounce_count = 0
            self._first_true_at = None
        elif self._state is TriggerState.COOLDOWN:
            self._reset_count = 0
        return TriggerEvaluation(TriggerStatus.SUPPRESSED, self._state, reason)


_READINESS_REASON: dict[IndicatorReadiness, TriggerReason] = {
    IndicatorReadiness.MISSING: TriggerReason.MISSING_INDICATOR,
    IndicatorReadiness.WARMING: TriggerReason.INDICATOR_WARMING,
    IndicatorReadiness.DISCONTINUOUS: TriggerReason.INDICATOR_DISCONTINUOUS,
    IndicatorReadiness.STALE: TriggerReason.INDICATOR_STALE,
    IndicatorReadiness.FUTURE: TriggerReason.INDICATOR_FUTURE,
    IndicatorReadiness.INSUFFICIENT_RETENTION: TriggerReason.INDICATOR_INSUFFICIENT_RETENTION,
    # READY는 매핑하지 않는다(coherence 단계로 진행).
}


def _coherence_reason(
    window: IndicatorWindowSnapshot, trade: NormalizedTradeTick
) -> TriggerReason | None:
    """READY indicator window와 최신 trade tick의 정확한 coherence를 판정한다.

    None이면 정확히 일치(같은 stream·같은 최신 체결). 불일치면 fail-closed 사유를 반환한다.
      - market/symbol/provider/channel 불일치 → INDICATOR_IDENTITY_MISMATCH
      - indicator가 명백히 뒤처짐(sequence< 또는 event_time<) → INDICATOR_LAGGING
      - 정확 일치(sequence/event_time/received_at 모두 ==) → None
      - 그 외(indicator가 앞서거나, 부분적으로 모순) → INDICATOR_IDENTITY_MISMATCH
    """
    seq = trade.provider_sequence
    if window.market != trade.market or window.symbol != trade.symbol:
        return TriggerReason.INDICATOR_IDENTITY_MISMATCH
    if window.provider != seq.provider or window.channel != seq.channel:
        return TriggerReason.INDICATOR_IDENTITY_MISMATCH
    # 이 시점 이후 비교를 위해 indicator의 최신 메타데이터가 존재해야 한다(READY 보장).
    if (
        window.latest_sequence is None
        or window.latest_event_time is None
        or window.latest_received_at is None
    ):
        return TriggerReason.INDICATOR_IDENTITY_MISMATCH
    if window.latest_sequence < seq.sequence or window.latest_event_time < trade.trade_at:
        return TriggerReason.INDICATOR_LAGGING
    if (
        window.latest_sequence == seq.sequence
        and window.latest_event_time == trade.trade_at
        and window.latest_received_at == trade.received_at
    ):
        return None
    # indicator가 latest보다 앞서거나 sequence/time/received_at이 부분적으로 모순 → fail-closed.
    return TriggerReason.INDICATOR_IDENTITY_MISMATCH


def _observed(
    metric: Metric,
    snapshot: LatestMarketStateSnapshot,
    indicators: IndicatorContext | None,
    window,
) -> Decimal:
    value = metric_value(metric, snapshot, indicators=indicators, window=window)
    assert value is not None  # engine guarantees freshness/readiness/coherence before firing
    return value


def _seconds(value: Decimal):
    from datetime import timedelta

    return timedelta(seconds=float(value))
