"""RTM-4b.1b — pure rolling-window indicators over a TradeHistorySnapshot.

이 모듈은 RTM-4b.1a `RollingTradeHistoryStore`가 만든 *불변* `TradeHistorySnapshot`만
입력으로 받아, caller가 명시한 `IndicatorWindowSpec`에 따라 (1) window를 선택하고
(2) readiness를 판정하고 (3) 순수 Decimal 지표(SMA/RETURN_BPS/ROLLING_VOLUME/VWAP)를
계산한다. store/monitor/trigger_engine/conditions를 건드리지 않으며 어떤 가변 상태도
보유하지 않는다(순수 함수 + 불변 결과).

핵심 안전 계약:
- 모든 정책값(lookback_events/lookback_seconds/min_events/freshness/max_gap)은 caller가
  명시한다. 숨은 기본값이 없다.
- window_id는 spec의 canonical JSON SHA-256이며, 수학적으로 같은 Decimal 정책(예:
  60 vs 60.0)은 같은 id를 만든다.
- readiness는 fail-closed다. 애매하거나 retention이 요청 window를 온전히 지원하지 못하면
  READY가 아니라 INSUFFICIENT_RETENTION/STALE/WARMING/DISCONTINUOUS로 떨어진다.
- lookback 기간과 freshness 기간은 절대 혼용하지 않는다(전자는 anchor=latest trade_at
  기준 표본 선택, 후자는 now 기준 최신 tick age).

network/broker/ledger/LLM/asyncio/threading import이 없다(import guard로 강제).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from domain._datetime import require_timezone_aware_datetime
from domain.enums import Market
from market_data.rolling_window import EpochStartReason, TradeHistorySnapshot, TradeSample

__all__ = [
    "IndicatorContext",
    "IndicatorKind",
    "IndicatorNotReadyError",
    "IndicatorReadiness",
    "IndicatorWindowSnapshot",
    "IndicatorWindowSpec",
    "build_indicator_context",
    "canonical_window_payload",
    "compute_indicator",
    "evaluate_window",
    "return_bps",
    "rolling_volume",
    "sma_price",
    "vwap",
]

_MICROS = Decimal(1_000_000)


def _validate_whole_micro_decimal(value: Decimal, *, field_name: str) -> Decimal:
    """양수·finite이고 정수 microseconds로 정확히 표현 가능한 Decimal만 통과시킨다.

    float 변환은 정밀도를 잃고 큰 값에서 overflow하므로 microsecond 정수 표현 가능
    범위만 허용한다(RollingRetentionPolicy와 동일 정책)."""
    if not isinstance(value, Decimal):
        raise ValueError(f"{field_name} must be a Decimal.")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be a finite Decimal > 0.")
    micros = value * _MICROS
    if micros != micros.to_integral_value():
        raise ValueError(f"{field_name} must be representable in whole microseconds.")
    try:
        timedelta(microseconds=int(micros))
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} is out of timedelta range.") from exc
    return value


def _canonical_decimal(value: Decimal | None) -> str | None:
    """수학적으로 같은 값이 같은 문자열이 되도록 정규화한다(60 == 60.0)."""
    if value is None:
        return None
    # normalize()는 후행 0을 제거해 60과 60.0을 동일 표현으로 만든다.
    normalized = value.normalize()
    # 음의 0/지수 표기를 안정적으로 직렬화하기 위해 to_eng_string 대신 표준 str 사용.
    return str(normalized)


def _to_timedelta(seconds: Decimal) -> timedelta:
    return timedelta(microseconds=int((seconds * _MICROS).to_integral_value()))


class IndicatorReadiness(StrEnum):
    """window readiness. READY가 아니면 지표 계산은 허용되지 않는다(fail-closed).

    STALE(과거로 오래됨)와 FUTURE(now보다 미래 timestamp)는 분리한다. latest store의
    정상 경로는 future tick을 차단하지만, indicator 계층은 독립적인 불변 입력을 받으므로
    clock skew/ordering bug를 단순 stale로 뭉개지 않고 별도로 진단한다(4b.2 reason 매핑용)."""

    MISSING = "missing"
    WARMING = "warming"
    DISCONTINUOUS = "discontinuous"
    READY = "ready"
    STALE = "stale"
    FUTURE = "future"
    INSUFFICIENT_RETENTION = "insufficient_retention"


class IndicatorKind(StrEnum):
    """RTM-4b.1b 순수 지표 종류. ConditionClause/Metric 통합은 RTM-4b.2의 책임이다."""

    SMA_PRICE = "sma_price"
    RETURN_BPS = "return_bps"
    ROLLING_VOLUME = "rolling_volume"
    VWAP = "vwap"


class IndicatorNotReadyError(Exception):
    """READY가 아닌 window에서 지표 계산을 시도했을 때 발생한다."""


class IndicatorWindowSpec(BaseModel):
    """rolling window 정책. 모든 값은 caller가 명시하며 숨은 기본값이 없다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lookback_events: int | None = None
    lookback_seconds: Decimal | None = None
    min_events: int
    freshness_max_age_seconds: Decimal
    max_gap_seconds: Decimal | None = None

    @field_validator("lookback_events", "min_events", mode="before")
    @classmethod
    def _reject_bool_int(cls, value: object, info: object) -> object:
        # bool은 int의 subclass이므로 명시적으로 거부한다.
        if isinstance(value, bool):
            raise ValueError(f"{info.field_name} must be an int, not bool.")  # type: ignore[attr-defined]
        return value

    @field_validator("min_events", mode="after")
    @classmethod
    def _validate_min_events(cls, value: int) -> int:
        if value < 1:
            raise ValueError("min_events must be >= 1.")
        return value

    @field_validator("lookback_events", mode="after")
    @classmethod
    def _validate_lookback_events(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("lookback_events must be >= 1 when provided.")
        return value

    @field_validator(
        "lookback_seconds", "freshness_max_age_seconds", "max_gap_seconds", mode="after"
    )
    @classmethod
    def _validate_durations(cls, value: Decimal | None, info: object) -> Decimal | None:
        if value is None:
            return None
        return _validate_whole_micro_decimal(value, field_name=info.field_name)  # type: ignore[attr-defined]

    @model_validator(mode="after")
    def _validate_window(self) -> IndicatorWindowSpec:
        if self.lookback_events is None and self.lookback_seconds is None:
            raise ValueError(
                "at least one of lookback_events or lookback_seconds is required."
            )
        if self.lookback_events is not None and self.lookback_events < self.min_events:
            raise ValueError("lookback_events must be >= min_events.")
        return self

    @property
    def window_id(self) -> str:
        """spec의 canonical JSON SHA-256(hex). 같은 의미의 정책은 같은 id를 만든다."""
        canonical = json.dumps(
            canonical_window_payload(self), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_window_payload(spec: IndicatorWindowSpec) -> dict[str, object | None]:
    """window_id 해시·rule-set canonicalization이 공유하는 정규화 payload.

    Decimal은 `_canonical_decimal`(normalize)로 직렬화하므로 60 ≡ 60.0이 동일 payload가
    된다. Python repr/str(spec) 같은 비결정 표현을 쓰지 않는다(rule_set_id 안정성)."""
    return {
        "lookback_events": spec.lookback_events,
        "lookback_seconds": _canonical_decimal(spec.lookback_seconds),
        "min_events": spec.min_events,
        "freshness_max_age_seconds": _canonical_decimal(spec.freshness_max_age_seconds),
        "max_gap_seconds": _canonical_decimal(spec.max_gap_seconds),
    }


@dataclass(frozen=True)
class IndicatorWindowSnapshot:
    """window 선택 + readiness 판정 결과(불변).

    READY일 때만 `selected`로 지표를 계산할 수 있다. 비-READY에서는 selected가 비어 있거나
    부분적일 수 있으므로 직접 계산하지 말고 compute_indicator()의 가드를 거친다.
    """

    window_id: str
    market: Market
    symbol: str
    readiness: IndicatorReadiness
    selected: tuple[TradeSample, ...]
    anchor_event_time: datetime | None
    latest_event_time: datetime | None
    oldest_selected_event_time: datetime | None
    age_seconds: Decimal | None
    continuity_epoch: int
    epoch_start_reason: EpochStartReason
    # RTM-4b.2 coherence: 결과만으로 어느 provider/channel/sequence/수신시각 기준인지
    # 자체 식별 가능하도록 source 메타데이터를 그대로 전달한다(판정에는 쓰이지 않음).
    provider: str | None
    channel: str | None
    latest_sequence: int | None
    latest_received_at: datetime | None

    @property
    def is_ready(self) -> bool:
        return self.readiness is IndicatorReadiness.READY


def _age_seconds(now: datetime, latest_event_time: datetime) -> Decimal:
    """now - latest_event_time을 정확한 Decimal 초로 환산한다(float 미사용)."""
    delta = now - latest_event_time
    micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return Decimal(micros) / _MICROS


def evaluate_window(
    history: TradeHistorySnapshot, spec: IndicatorWindowSpec, *, now: datetime
) -> IndicatorWindowSnapshot:
    """history와 spec으로 window를 선택하고 readiness를 판정한다.

    readiness 우선순위(fail-closed, 위에서부터):
      1. MISSING                 — 한 번도 관찰되지 않음
      2. INSUFFICIENT_RETENTION  — spec lookback이 store hard cap을 초과(정적)
      3. DISCONTINUOUS           — reset 직후 현재 epoch에 표본이 없음(suffix 0)
      4. INSUFFICIENT_RETENTION  — 요청 구간 내부 표본이 실제로 eviction됨(동적)
      5. WARMING / DISCONTINUOUS — 연속 suffix가 min_events 미달
      6. FUTURE                  — 최신 tick이 now보다 미래(age < 0; clock skew/ordering)
      7. STALE                   — 최신 tick age가 freshness 임계 초과
      8. READY
    """
    aware_now = require_timezone_aware_datetime(now, field_name="now")
    retention = history.retention
    base = IndicatorWindowSnapshot(
        window_id=spec.window_id,
        market=history.market,
        symbol=history.symbol,
        readiness=IndicatorReadiness.MISSING,
        selected=(),
        anchor_event_time=None,
        latest_event_time=history.latest_event_time,
        oldest_selected_event_time=None,
        age_seconds=None,
        continuity_epoch=history.continuity_epoch,
        epoch_start_reason=history.epoch_start_reason,
        provider=history.provider,
        channel=history.channel,
        latest_sequence=history.latest_sequence,
        latest_received_at=history.latest_received_at,
    )

    def _with(readiness: IndicatorReadiness, **over: object) -> IndicatorWindowSnapshot:
        from dataclasses import replace

        return replace(base, readiness=readiness, **over)  # type: ignore[arg-type]

    # 1) 한 번도 관찰되지 않음.
    if not history.was_ever_observed:
        return base  # MISSING

    # 2) 정적 retention 불일치: spec이 store hard cap보다 큰 lookback을 요구.
    if (
        spec.lookback_events is not None
        and spec.lookback_events > retention.hard_max_events
    ):
        return _with(IndicatorReadiness.INSUFFICIENT_RETENTION)
    if (
        spec.lookback_seconds is not None
        and spec.lookback_seconds > retention.hard_max_age_seconds
    ):
        return _with(IndicatorReadiness.INSUFFICIENT_RETENTION)

    samples = history.samples
    # 3) 관찰된 적은 있으나 현재 epoch이 비어 있음(명시적 reset 직후, 새 event 전).
    if not samples:
        # was_ever_observed=True인데 비어 있으면 reset epoch이다(초기 epoch은 항상 ≥1 표본).
        return _with(IndicatorReadiness.DISCONTINUOUS)

    anchor = samples[-1].trade_at

    # --- window 선택: count/time 교집합(둘 다 oldest→newest 리스트의 suffix) ---
    count_start = 0
    if spec.lookback_events is not None:
        count_start = max(0, len(samples) - spec.lookback_events)
    time_start = 0
    if spec.lookback_seconds is not None:
        old_edge = anchor - _to_timedelta(spec.lookback_seconds)  # 배타적 경계
        # trade_at > old_edge 인 최초 인덱스를 찾는다(같은 값은 제외=배타).
        time_start = 0
        for i, s in enumerate(samples):
            if s.trade_at > old_edge:
                time_start = i
                break
        else:
            time_start = len(samples)
    start = max(count_start, time_start)
    selected = samples[start:]

    # 4) 동적 retention coverage: 요청 구간 내부 표본이 실제로 eviction됐는지.
    #    애매하면 항상 INSUFFICIENT_RETENTION으로 fail-closed한다.
    #
    #    count+time 복합 window의 effective window는 두 경계의 *교집합*이다. 가장
    #    최근에 evict된 표본(evicted high-water mark)이 이 교집합 내부에 속했어야만
    #    coverage가 깨진 것이다. 교집합 membership = (time 경계 내부) AND (count 경계
    #    내부)이므로, 두 경계가 모두 있을 때는 두 조건이 *동시에* 참일 때만 INSUFFICIENT다.
    #    한쪽만 참이면 evict된 표본은 교집합 밖이라 요청 window를 훼손하지 않는다.
    #    단일 경계 spec은 그 경계의 조건만으로 판정한다(기존 count-only/time-only 동작 유지).
    evicted_time = history.evicted_through_event_time
    time_evicted = False
    if spec.lookback_seconds is not None and evicted_time is not None:
        old_edge = anchor - _to_timedelta(spec.lookback_seconds)
        # old edge가 배타적이므로 evicted high-water mark가 그보다 *크면* time 경계 내부가 잘렸다.
        time_evicted = evicted_time > old_edge
    count_evicted = False
    if spec.lookback_events is not None and len(samples) < spec.lookback_events:
        # 요청한 N개보다 적게 남았는데 eviction 이력이 있으면 count 경계가 잘린 것이다.
        # eviction이 없으면 아직 충분히 쌓이지 않은 것(WARMING/DISCONTINUOUS 단계로).
        count_evicted = history.evicted_event_count > 0

    if spec.lookback_events is not None and spec.lookback_seconds is not None:
        insufficient = time_evicted and count_evicted
    elif spec.lookback_seconds is not None:
        insufficient = time_evicted
    else:
        insufficient = count_evicted
    if insufficient:
        return _with(IndicatorReadiness.INSUFFICIENT_RETENTION)

    # --- gap 처리: 선택 구간에서 마지막 gap 이후 연속 suffix만 유효 ---
    suffix = selected
    gap_present = False
    if spec.max_gap_seconds is not None and len(selected) >= 2:
        max_gap = _to_timedelta(spec.max_gap_seconds)
        # newest에서 과거로 인접 delta를 검사, gap을 처음 만나면 그 지점에서 자른다.
        cut = 0
        for i in range(len(selected) - 1, 0, -1):
            if selected[i].trade_at - selected[i - 1].trade_at > max_gap:
                cut = i
                gap_present = True
                break
        suffix = selected[cut:]

    oldest_selected = suffix[0].trade_at if suffix else None

    # 5) 연속 suffix가 min_events 미달.
    if len(suffix) < spec.min_events:
        discontinuous = (
            gap_present or history.epoch_start_reason is EpochStartReason.EXPLICIT_RESET
        )
        readiness = (
            IndicatorReadiness.DISCONTINUOUS
            if discontinuous
            else IndicatorReadiness.WARMING
        )
        return _with(
            readiness,
            selected=suffix,
            anchor_event_time=anchor,
            oldest_selected_event_time=oldest_selected,
        )

    # 6) freshness: 최신 tick age. lookback 기간과 절대 혼용하지 않는다.
    age = _age_seconds(aware_now, anchor)
    # 미래 tick(age < 0)은 STALE(과거로 오래됨)과 원인이 다르므로 분리한다. latest store의
    # 정상 경로는 future tick을 막지만 indicator 계층은 독립 입력을 받으므로 clock skew/
    # ordering bug를 별도 readiness로 노출해 4b.2 reason 매핑이 정확해지도록 한다.
    if age < 0:
        return _with(
            IndicatorReadiness.FUTURE,
            selected=suffix,
            anchor_event_time=anchor,
            oldest_selected_event_time=oldest_selected,
            age_seconds=age,
        )
    if age > spec.freshness_max_age_seconds:
        return _with(
            IndicatorReadiness.STALE,
            selected=suffix,
            anchor_event_time=anchor,
            oldest_selected_event_time=oldest_selected,
            age_seconds=age,
        )

    # 7) READY.
    return _with(
        IndicatorReadiness.READY,
        selected=suffix,
        anchor_event_time=anchor,
        oldest_selected_event_time=oldest_selected,
        age_seconds=age,
    )


def _require_ready(window: IndicatorWindowSnapshot) -> tuple[TradeSample, ...]:
    if not window.is_ready:
        raise IndicatorNotReadyError(
            f"window not READY (readiness={window.readiness.value})."
        )
    if not window.selected:
        # READY는 min_events>=1을 보장하므로 정상 경로에서 도달하지 않는 방어 코드.
        raise IndicatorNotReadyError("READY window has no samples.")
    return window.selected


def sma_price(window: IndicatorWindowSnapshot) -> Decimal:
    samples = _require_ready(window)
    total = sum((s.price for s in samples), Decimal(0))
    return total / Decimal(len(samples))


def return_bps(window: IndicatorWindowSnapshot) -> Decimal:
    samples = _require_ready(window)
    oldest = samples[0].price
    latest = samples[-1].price
    return (latest - oldest) / oldest * Decimal(10000)


def rolling_volume(window: IndicatorWindowSnapshot) -> Decimal:
    samples = _require_ready(window)
    return sum((s.quantity for s in samples), Decimal(0))


def vwap(window: IndicatorWindowSnapshot) -> Decimal:
    samples = _require_ready(window)
    numerator = sum((s.price * s.quantity for s in samples), Decimal(0))
    denominator = sum((s.quantity for s in samples), Decimal(0))
    # NormalizedTradeTick.quantity > 0이므로 non-empty window에서 분모는 0이 될 수 없다.
    return numerator / denominator


_CALCULATORS = {
    IndicatorKind.SMA_PRICE: sma_price,
    IndicatorKind.RETURN_BPS: return_bps,
    IndicatorKind.ROLLING_VOLUME: rolling_volume,
    IndicatorKind.VWAP: vwap,
}


def compute_indicator(kind: IndicatorKind, window: IndicatorWindowSnapshot) -> Decimal:
    """READY window에서 지표를 계산한다. 비-READY면 IndicatorNotReadyError."""
    return _CALCULATORS[kind](window)


@dataclass(frozen=True)
class IndicatorContext:
    """TriggerEngine에 주입하는 불변 rolling-indicator 입력.

    엔진이 store/monitor/network를 직접 읽지 않도록, evaluate된 window snapshot들만
    frozen tuple로 보관한다. 모든 snapshot은 동일한 (market, symbol)에 속해야 하며
    window_id는 유일해야 한다. 순서는 window_id 기준으로 결정론적으로 정규화된다.
    """

    market: Market
    symbol: str
    windows: tuple[IndicatorWindowSnapshot, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for w in self.windows:
            if w.window_id in seen:
                raise ValueError(f"duplicate window_id in IndicatorContext: {w.window_id}.")
            seen.add(w.window_id)
            if w.market != self.market or w.symbol != self.symbol:
                raise ValueError(
                    "IndicatorContext window market/symbol must match the context."
                )
        ordered = tuple(sorted(self.windows, key=lambda w: w.window_id))
        object.__setattr__(self, "windows", ordered)

    def get(self, window_id: str) -> IndicatorWindowSnapshot | None:
        """window_id로 snapshot을 조회한다. 없으면 None(엔진이 fail-closed 처리)."""
        for w in self.windows:
            if w.window_id == window_id:
                return w
        return None


def build_indicator_context(
    history: TradeHistorySnapshot,
    specs: tuple[IndicatorWindowSpec, ...],
    *,
    now: datetime,
) -> IndicatorContext:
    """history와 spec들로 IndicatorContext를 만든다(store/network 재조회 없음).

    동일 window_id를 가진 spec은 한 번만 evaluate한다. snapshot의 market/symbol은
    history에서 오므로 context identity와 항상 일치한다."""
    by_id: dict[str, IndicatorWindowSnapshot] = {}
    for spec in specs:
        wid = spec.window_id
        if wid in by_id:
            continue
        by_id[wid] = evaluate_window(history, spec, now=now)
    return IndicatorContext(
        market=history.market,
        symbol=history.symbol,
        windows=tuple(by_id.values()),
    )
