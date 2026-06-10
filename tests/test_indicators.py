"""RTM-4b.1b — pure rolling-window indicators tests (network/broker/ledger-free).

`IndicatorWindowSpec` 검증·canonical window_id·window 선택·readiness 판정·순수 Decimal
지표 계산을 fixture만으로 검증한다. retention coverage는 구현 전에 확정한 truth table을
그대로 테스트로 고정한다(애매하면 항상 INSUFFICIENT_RETENTION으로 fail-closed).

Retention coverage truth table (anchor=latest trade_at, old edge 배타적):
| 케이스                                            | 기대 readiness          |
| ------------------------------------------------ | ----------------------- |
| lookback_events > hard_max_events (정적)          | INSUFFICIENT_RETENTION  |
| lookback_seconds > hard_max_age_seconds (정적)    | INSUFFICIENT_RETENTION  |
| time-only: evicted_through > anchor-lookback_sec | INSUFFICIENT_RETENTION  |
| time-only: evicted_through <= anchor-lookback_sec| (다음 단계로) READY 가능 |
| count-only: len<lookback_events & evicted_count>0| INSUFFICIENT_RETENTION  |
| count-only: len<lookback_events & evicted_count==0| (다음 단계로; WARMING)  |
| count+time: time_evicted AND count_evicted       | INSUFFICIENT_RETENTION  |
| count+time: 둘 중 하나만(교집합 밖)               | (다음 단계로) READY 가능 |

count+time 복합 window는 두 경계의 *교집합*이 effective window다. 가장 최근에 evict된
표본이 그 교집합 내부에 속했을 때(=time 경계 내부 AND count 경계 내부)만 coverage가 깨진다.

freshness: age<0(미래 tick)→FUTURE, age>freshness_max_age_seconds→STALE(분리).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domain.enums import Market
from market_data.indicators import (
    IndicatorKind,
    IndicatorNotReadyError,
    IndicatorReadiness,
    IndicatorWindowSpec,
    compute_indicator,
    evaluate_window,
    return_bps,
    rolling_volume,
    sma_price,
    vwap,
)
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


def _retention(*, events: int = 1000, age: str = "86400") -> RollingRetentionPolicy:
    return RollingRetentionPolicy(hard_max_events=events, hard_max_age_seconds=Decimal(age))


def _history(
    samples: tuple[TradeSample, ...],
    *,
    retention: RollingRetentionPolicy | None = None,
    was_ever_observed: bool = True,
    continuity_epoch: int = 1,
    epoch_start_reason: EpochStartReason = EpochStartReason.INITIAL,
    evicted_event_count: int = 0,
    evicted_through_event_time: datetime | None = None,
) -> TradeHistorySnapshot:
    retention = retention or _retention()
    latest = samples[-1] if samples else None
    oldest = samples[0] if samples else None
    return TradeHistorySnapshot(
        market=Market.KR,
        symbol="005930",
        samples=samples,
        retention=retention,
        provider="kis" if samples else None,
        channel="H0STCNT0|005930" if samples else None,
        was_ever_observed=was_ever_observed,
        continuity_epoch=continuity_epoch,
        epoch_start_reason=epoch_start_reason,
        latest_sequence=latest.sequence if latest else None,
        latest_event_time=latest.trade_at if latest else None,
        latest_received_at=latest.received_at if latest else None,
        oldest_event_time=oldest.trade_at if oldest else None,
        evicted_event_count=evicted_event_count,
        evicted_through_event_time=evicted_through_event_time,
        retention_truncated=evicted_event_count > 0,
    )


def _spec(**over: object) -> IndicatorWindowSpec:
    base: dict[str, object] = {
        "lookback_events": 3,
        "min_events": 2,
        "freshness_max_age_seconds": Decimal("60"),
    }
    base.update(over)
    return IndicatorWindowSpec(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Spec validation + canonical window_id
# --------------------------------------------------------------------------- #


def test_count_only_spec_is_valid() -> None:
    spec = IndicatorWindowSpec(
        lookback_events=5, min_events=3, freshness_max_age_seconds=Decimal("30")
    )
    assert spec.lookback_events == 5
    assert spec.lookback_seconds is None


def test_time_only_spec_is_valid() -> None:
    spec = IndicatorWindowSpec(
        lookback_seconds=Decimal("60"), min_events=3, freshness_max_age_seconds=Decimal("30")
    )
    assert spec.lookback_events is None
    assert spec.lookback_seconds == Decimal("60")


def test_count_and_time_spec_is_valid() -> None:
    spec = IndicatorWindowSpec(
        lookback_events=5,
        lookback_seconds=Decimal("60"),
        min_events=3,
        freshness_max_age_seconds=Decimal("30"),
    )
    assert spec.lookback_events == 5
    assert spec.lookback_seconds == Decimal("60")


def test_neither_lookback_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one of lookback"):
        IndicatorWindowSpec(min_events=2, freshness_max_age_seconds=Decimal("30"))


def test_min_events_below_one_rejected() -> None:
    with pytest.raises(ValueError, match="min_events must be >= 1"):
        IndicatorWindowSpec(
            lookback_events=3, min_events=0, freshness_max_age_seconds=Decimal("30")
        )


def test_lookback_events_below_min_events_rejected() -> None:
    with pytest.raises(ValueError, match="lookback_events must be >= min_events"):
        IndicatorWindowSpec(
            lookback_events=2, min_events=3, freshness_max_age_seconds=Decimal("30")
        )


@pytest.mark.parametrize("bad", ["-1", "0", "NaN", "Infinity"])
def test_non_positive_or_non_finite_durations_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        IndicatorWindowSpec(
            lookback_events=3, min_events=2, freshness_max_age_seconds=Decimal(bad)
        )


def test_sub_microsecond_duration_rejected() -> None:
    with pytest.raises(ValueError, match="whole microseconds"):
        IndicatorWindowSpec(
            lookback_events=3,
            min_events=2,
            freshness_max_age_seconds=Decimal("0.0000001"),  # 100 ns
        )


def test_bool_lookback_events_rejected() -> None:
    with pytest.raises(ValueError, match="not bool"):
        IndicatorWindowSpec(
            lookback_events=True, min_events=1, freshness_max_age_seconds=Decimal("30")
        )


def test_extra_field_forbidden() -> None:
    with pytest.raises(ValueError):
        IndicatorWindowSpec(
            lookback_events=3,
            min_events=2,
            freshness_max_age_seconds=Decimal("30"),
            unexpected=1,  # type: ignore[call-arg]
        )


def test_equivalent_decimal_specs_share_window_id() -> None:
    a = IndicatorWindowSpec(
        lookback_seconds=Decimal("60"),
        min_events=2,
        freshness_max_age_seconds=Decimal("30"),
        max_gap_seconds=Decimal("5"),
    )
    b = IndicatorWindowSpec(
        lookback_seconds=Decimal("60.0"),
        min_events=2,
        freshness_max_age_seconds=Decimal("30.00"),
        max_gap_seconds=Decimal("5.000"),
    )
    assert a.window_id == b.window_id


def test_different_specs_have_different_window_id() -> None:
    a = _spec(lookback_events=3)
    b = _spec(lookback_events=4)
    c = _spec(lookback_events=3, max_gap_seconds=Decimal("5"))
    assert a.window_id != b.window_id
    assert a.window_id != c.window_id
    assert b.window_id != c.window_id


def test_window_id_is_sha256_hex() -> None:
    wid = _spec().window_id
    assert len(wid) == 64
    int(wid, 16)  # raises if not hex


# --------------------------------------------------------------------------- #
# Window selection
# --------------------------------------------------------------------------- #


def test_count_eviction_selects_newest_n() -> None:
    samples = tuple(
        _sample(seq=i, price=str(100 + i), qty="10", offset=i) for i in range(1, 6)
    )  # seq 1..5
    spec = _spec(lookback_events=3, min_events=1)
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=5))
    assert w.readiness is IndicatorReadiness.READY
    assert [s.sequence for s in w.selected] == [3, 4, 5]


def test_time_old_edge_is_exclusive() -> None:
    # offsets 0,10,20,30; anchor=30, lookback 20s → old edge = 10 (exclusive).
    samples = (
        _sample(seq=1, price="100", qty="10", offset=0),
        _sample(seq=2, price="100", qty="10", offset=10),
        _sample(seq=3, price="100", qty="10", offset=20),
        _sample(seq=4, price="100", qty="10", offset=30),
    )
    spec = _spec(lookback_events=None, lookback_seconds=Decimal("20"), min_events=1)
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=30))
    # offset==10 is the exclusive edge → excluded; only 20 and 30 remain.
    assert [s.sequence for s in w.selected] == [3, 4]


def test_newest_edge_is_included() -> None:
    samples = (
        _sample(seq=1, price="100", qty="10", offset=0),
        _sample(seq=2, price="100", qty="10", offset=30),
    )
    spec = _spec(lookback_events=None, lookback_seconds=Decimal("30"), min_events=1)
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=30))
    assert w.selected[-1].sequence == 2  # anchor itself included


def test_count_and_time_intersection_takes_tighter_bound() -> None:
    samples = tuple(
        _sample(seq=i, price="100", qty="10", offset=i) for i in range(1, 6)
    )  # offsets 1..5, anchor=5
    # count would keep newest 4 (seq 2..5); time 2s keeps offset>3 → seq 4,5. Intersection=seq 4,5.
    spec = _spec(
        lookback_events=4, lookback_seconds=Decimal("2"), min_events=1
    )
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=5))
    assert [s.sequence for s in w.selected] == [4, 5]


def test_same_timestamp_increasing_sequence_all_included() -> None:
    # three ticks share offset=10 with increasing sequence; all valid in window.
    samples = (
        _sample(seq=1, price="100", qty="10", offset=10),
        _sample(seq=2, price="110", qty="10", offset=10),
        _sample(seq=3, price="120", qty="10", offset=10),
    )
    spec = _spec(lookback_events=None, lookback_seconds=Decimal("5"), min_events=3)
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=10))
    assert w.readiness is IndicatorReadiness.READY
    assert [s.sequence for s in w.selected] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# Readiness states
# --------------------------------------------------------------------------- #


def test_never_observed_is_missing() -> None:
    snap = _history((), was_ever_observed=False, continuity_epoch=0)
    w = evaluate_window(snap, _spec(), now=_BASE + timedelta(seconds=5))
    assert w.readiness is IndicatorReadiness.MISSING
    assert w.selected == ()


def test_initial_epoch_insufficient_is_warming() -> None:
    samples = (_sample(seq=1, price="100", qty="10", offset=5),)
    spec = _spec(lookback_events=3, min_events=2)
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=5))
    assert w.readiness is IndicatorReadiness.WARMING


def test_empty_after_explicit_reset_is_discontinuous() -> None:
    snap = _history(
        (),
        was_ever_observed=True,
        continuity_epoch=2,
        epoch_start_reason=EpochStartReason.EXPLICIT_RESET,
    )
    w = evaluate_window(snap, _spec(), now=_BASE + timedelta(seconds=5))
    assert w.readiness is IndicatorReadiness.DISCONTINUOUS


def test_reset_epoch_insufficient_suffix_is_discontinuous() -> None:
    samples = (_sample(seq=1, price="100", qty="10", offset=5),)
    snap = _history(
        samples,
        continuity_epoch=2,
        epoch_start_reason=EpochStartReason.EXPLICIT_RESET,
    )
    spec = _spec(lookback_events=3, min_events=2)
    w = evaluate_window(snap, spec, now=_BASE + timedelta(seconds=5))
    assert w.readiness is IndicatorReadiness.DISCONTINUOUS


def test_gap_then_insufficient_suffix_is_discontinuous() -> None:
    # offsets 0,1,100; max_gap 5s → gap before offset=100 → suffix=[offset100] (1 < min 2).
    samples = (
        _sample(seq=1, price="100", qty="10", offset=0),
        _sample(seq=2, price="100", qty="10", offset=1),
        _sample(seq=3, price="100", qty="10", offset=100),
    )
    spec = _spec(
        lookback_events=None,
        lookback_seconds=Decimal("200"),
        min_events=2,
        max_gap_seconds=Decimal("5"),
    )
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=100))
    assert w.readiness is IndicatorReadiness.DISCONTINUOUS
    assert [s.sequence for s in w.selected] == [3]  # suffix after the gap


def test_gap_with_sufficient_suffix_is_ready() -> None:
    # suffix after gap has >= min_events.
    samples = (
        _sample(seq=1, price="100", qty="10", offset=0),
        _sample(seq=2, price="100", qty="10", offset=100),
        _sample(seq=3, price="100", qty="10", offset=101),
        _sample(seq=4, price="100", qty="10", offset=102),
    )
    spec = _spec(
        lookback_events=None,
        lookback_seconds=Decimal("200"),
        min_events=2,
        max_gap_seconds=Decimal("5"),
        freshness_max_age_seconds=Decimal("10"),
    )
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=102))
    assert w.readiness is IndicatorReadiness.READY
    assert [s.sequence for s in w.selected] == [2, 3, 4]


def test_sufficient_suffix_is_ready() -> None:
    samples = tuple(
        _sample(seq=i, price="100", qty="10", offset=i) for i in range(1, 4)
    )
    spec = _spec(lookback_events=3, min_events=2, freshness_max_age_seconds=Decimal("10"))
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=3))
    assert w.readiness is IndicatorReadiness.READY


def test_freshness_boundary_age_equal_max_is_ready() -> None:
    samples = tuple(
        _sample(seq=i, price="100", qty="10", offset=i) for i in range(1, 4)
    )  # anchor offset=3
    spec = _spec(lookback_events=3, min_events=2, freshness_max_age_seconds=Decimal("60"))
    # now exactly 60s after anchor (offset 3) → age == max → READY.
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=63))
    assert w.readiness is IndicatorReadiness.READY
    assert w.age_seconds == Decimal("60")


def test_age_beyond_max_is_stale() -> None:
    samples = tuple(
        _sample(seq=i, price="100", qty="10", offset=i) for i in range(1, 4)
    )
    spec = _spec(lookback_events=3, min_events=2, freshness_max_age_seconds=Decimal("60"))
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=64))
    assert w.readiness is IndicatorReadiness.STALE


def test_future_latest_tick_is_fail_closed_future_not_stale() -> None:
    # now is before the latest trade_at → negative age → FUTURE (distinct from STALE).
    samples = tuple(
        _sample(seq=i, price="100", qty="10", offset=i) for i in range(1, 4)
    )  # anchor offset=3
    spec = _spec(lookback_events=3, min_events=2, freshness_max_age_seconds=Decimal("60"))
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=1))
    assert w.readiness is IndicatorReadiness.FUTURE
    assert w.age_seconds is not None and w.age_seconds < 0


# --------------------------------------------------------------------------- #
# Retention coverage (truth table)
# --------------------------------------------------------------------------- #


def test_spec_exceeds_hard_count_cap_is_insufficient() -> None:
    samples = tuple(_sample(seq=i, price="100", qty="10", offset=i) for i in range(1, 4))
    spec = IndicatorWindowSpec(
        lookback_events=10, min_events=2, freshness_max_age_seconds=Decimal("60")
    )
    snap = _history(samples, retention=_retention(events=5))
    w = evaluate_window(snap, spec, now=_BASE + timedelta(seconds=3))
    assert w.readiness is IndicatorReadiness.INSUFFICIENT_RETENTION


def test_spec_exceeds_hard_age_cap_is_insufficient() -> None:
    samples = tuple(_sample(seq=i, price="100", qty="10", offset=i) for i in range(1, 4))
    spec = IndicatorWindowSpec(
        lookback_seconds=Decimal("7200"),
        min_events=2,
        freshness_max_age_seconds=Decimal("60"),
    )
    snap = _history(samples, retention=_retention(age="3600"))
    w = evaluate_window(snap, spec, now=_BASE + timedelta(seconds=3))
    assert w.readiness is IndicatorReadiness.INSUFFICIENT_RETENTION


def test_eviction_inside_requested_time_range_is_insufficient() -> None:
    # anchor offset=100, lookback 50s → old edge=50 (exclusive). An evicted sample at
    # offset=60 (> 50) means a sample inside the requested range was dropped.
    samples = (
        _sample(seq=8, price="100", qty="10", offset=70),
        _sample(seq=9, price="100", qty="10", offset=85),
        _sample(seq=10, price="100", qty="10", offset=100),
    )
    spec = _spec(
        lookback_events=None,
        lookback_seconds=Decimal("50"),
        min_events=2,
        freshness_max_age_seconds=Decimal("120"),
    )
    snap = _history(
        samples,
        evicted_event_count=7,
        evicted_through_event_time=_BASE + timedelta(seconds=60),
    )
    w = evaluate_window(snap, spec, now=_BASE + timedelta(seconds=100))
    assert w.readiness is IndicatorReadiness.INSUFFICIENT_RETENTION


def test_eviction_outside_requested_time_range_is_ready() -> None:
    # evicted high-water mark at offset=40 <= old edge 50 → request range intact → READY.
    samples = (
        _sample(seq=8, price="100", qty="10", offset=70),
        _sample(seq=9, price="100", qty="10", offset=85),
        _sample(seq=10, price="100", qty="10", offset=100),
    )
    spec = _spec(
        lookback_events=None,
        lookback_seconds=Decimal("50"),
        min_events=2,
        freshness_max_age_seconds=Decimal("120"),
    )
    snap = _history(
        samples,
        evicted_event_count=7,
        evicted_through_event_time=_BASE + timedelta(seconds=40),
    )
    w = evaluate_window(snap, spec, now=_BASE + timedelta(seconds=100))
    assert w.readiness is IndicatorReadiness.READY


def test_count_window_truncated_by_eviction_is_insufficient() -> None:
    # want newest 5, only 3 remain, and eviction happened → count window truncated.
    samples = tuple(_sample(seq=i, price="100", qty="10", offset=i) for i in range(8, 11))
    spec = IndicatorWindowSpec(
        lookback_events=5,
        min_events=2,
        freshness_max_age_seconds=Decimal("120"),
    )
    snap = _history(
        samples,
        retention=_retention(events=5, age="3600"),
        evicted_event_count=7,
        evicted_through_event_time=_BASE + timedelta(seconds=7),
    )
    w = evaluate_window(snap, spec, now=_BASE + timedelta(seconds=10))
    assert w.readiness is IndicatorReadiness.INSUFFICIENT_RETENTION


def test_count_shortfall_without_eviction_is_warming_not_insufficient() -> None:
    # want newest 5, only 3 exist, NO eviction → just not warmed up yet (WARMING).
    samples = tuple(_sample(seq=i, price="100", qty="10", offset=i) for i in range(1, 4))
    spec = IndicatorWindowSpec(
        lookback_events=5, min_events=5, freshness_max_age_seconds=Decimal("120")
    )
    snap = _history(samples, retention=_retention(events=100), evicted_event_count=0)
    w = evaluate_window(snap, spec, now=_BASE + timedelta(seconds=3))
    assert w.readiness is IndicatorReadiness.WARMING


def test_combined_count_binding_time_eviction_outside_count_is_ready() -> None:
    # count+time spec. count is the binding (narrower) bound: 5 samples remain, want
    # newest 3, so the count window is fully covered (count_evicted=False). An evicted
    # sample falls inside the *time* range (time_evicted=True), but it is OUTSIDE the
    # effective intersection (the latest-3 count window), so coverage is intact → READY.
    # Old independent-OR logic wrongly returned INSUFFICIENT here.
    samples = tuple(
        _sample(seq=i, price="100", qty="10", offset=o)
        for i, o in [(6, 80), (7, 85), (8, 90), (9, 95), (10, 100)]
    )
    spec = IndicatorWindowSpec(
        lookback_events=3,
        lookback_seconds=Decimal("200"),  # old edge = anchor-200 → far in the past
        min_events=2,
        freshness_max_age_seconds=Decimal("120"),
    )
    snap = _history(
        samples,
        retention=_retention(events=1000, age="3600"),
        evicted_event_count=4,
        evicted_through_event_time=_BASE + timedelta(seconds=60),  # inside time range
    )
    w = evaluate_window(snap, spec, now=_BASE + timedelta(seconds=100))
    assert w.readiness is IndicatorReadiness.READY


def test_combined_time_binding_count_eviction_outside_time_is_ready() -> None:
    # count+time spec. time is the binding (narrower) bound. count shows a shortfall
    # (3 < lookback_events=10) and eviction occurred (count_evicted=True), but the
    # evicted high-water mark is OUTSIDE the time range (time_evicted=False), so the
    # effective intersection is intact → READY. Old OR logic wrongly returned INSUFFICIENT.
    samples = tuple(
        _sample(seq=i, price="100", qty="10", offset=o)
        for i, o in [(8, 90), (9, 95), (10, 100)]
    )
    spec = IndicatorWindowSpec(
        lookback_events=10,
        lookback_seconds=Decimal("30"),  # old edge = 100-30 = 70 (exclusive)
        min_events=2,
        freshness_max_age_seconds=Decimal("120"),
    )
    snap = _history(
        samples,
        retention=_retention(events=1000, age="3600"),
        evicted_event_count=5,
        evicted_through_event_time=_BASE + timedelta(seconds=50),  # 50 <= 70 → outside
    )
    w = evaluate_window(snap, spec, now=_BASE + timedelta(seconds=100))
    assert w.readiness is IndicatorReadiness.READY


def test_combined_intersection_internal_eviction_is_insufficient() -> None:
    # count+time spec where BOTH bounds are truncated: count shortfall (2 < 3) with
    # eviction (count_evicted=True) AND evicted high-water mark inside the time range
    # (time_evicted=True). The evicted sample belongs to the effective intersection →
    # INSUFFICIENT (genuine coverage loss is still fail-closed).
    samples = tuple(
        _sample(seq=i, price="100", qty="10", offset=o)
        for i, o in [(9, 85), (10, 100)]
    )
    spec = IndicatorWindowSpec(
        lookback_events=3,
        lookback_seconds=Decimal("50"),  # old edge = 100-50 = 50 (exclusive)
        min_events=2,
        freshness_max_age_seconds=Decimal("120"),
    )
    snap = _history(
        samples,
        retention=_retention(events=1000, age="3600"),
        evicted_event_count=7,
        evicted_through_event_time=_BASE + timedelta(seconds=60),  # 60 > 50 → inside
    )
    w = evaluate_window(snap, spec, now=_BASE + timedelta(seconds=100))
    assert w.readiness is IndicatorReadiness.INSUFFICIENT_RETENTION


def test_ready_window_carries_source_coherence_metadata() -> None:
    # RTM-4b.2 coherence: a snapshot must self-identify its provider/channel/sequence/
    # received_at so downstream consumers need not re-read the store.
    samples = tuple(
        _sample(seq=i, price="100", qty="10", offset=i) for i in range(1, 4)
    )
    spec = _spec(lookback_events=3, min_events=2, freshness_max_age_seconds=Decimal("60"))
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=4))
    assert w.readiness is IndicatorReadiness.READY
    assert w.provider == "kis"
    assert w.channel == "H0STCNT0|005930"
    assert w.latest_sequence == 3
    assert w.latest_received_at == _BASE + timedelta(seconds=3)


# --------------------------------------------------------------------------- #
# Calculations (pure Decimal)
# --------------------------------------------------------------------------- #


def _ready_window(samples: tuple[TradeSample, ...]) -> object:
    spec = _spec(
        lookback_events=len(samples),
        min_events=1,
        freshness_max_age_seconds=Decimal("100000"),
    )
    w = evaluate_window(
        _history(samples), spec, now=samples[-1].trade_at + timedelta(seconds=1)
    )
    assert w.readiness is IndicatorReadiness.READY
    return w


def test_sma_exact_value() -> None:
    samples = (
        _sample(seq=1, price="100", qty="10", offset=1),
        _sample(seq=2, price="110", qty="10", offset=2),
        _sample(seq=3, price="120", qty="10", offset=3),
    )
    assert sma_price(_ready_window(samples)) == Decimal("110")


def test_return_bps_positive() -> None:
    samples = (
        _sample(seq=1, price="100", qty="10", offset=1),
        _sample(seq=2, price="120", qty="10", offset=2),
    )
    assert return_bps(_ready_window(samples)) == Decimal("2000")


def test_return_bps_negative() -> None:
    samples = (
        _sample(seq=1, price="100", qty="10", offset=1),
        _sample(seq=2, price="90", qty="10", offset=2),
    )
    assert return_bps(_ready_window(samples)) == Decimal("-1000")


def test_return_bps_zero() -> None:
    samples = (
        _sample(seq=1, price="100", qty="10", offset=1),
        _sample(seq=2, price="100", qty="10", offset=2),
    )
    assert return_bps(_ready_window(samples)) == Decimal("0")


def test_rolling_volume_sum() -> None:
    samples = (
        _sample(seq=1, price="100", qty="10", offset=1),
        _sample(seq=2, price="100", qty="25", offset=2),
        _sample(seq=3, price="100", qty="5", offset=3),
    )
    assert rolling_volume(_ready_window(samples)) == Decimal("40")


def test_vwap_exact_value() -> None:
    # (100*10 + 200*10) / 20 = 150
    samples = (
        _sample(seq=1, price="100", qty="10", offset=1),
        _sample(seq=2, price="200", qty="10", offset=2),
    )
    assert vwap(_ready_window(samples)) == Decimal("150")


def test_all_results_are_decimal() -> None:
    samples = (
        _sample(seq=1, price="100", qty="10", offset=1),
        _sample(seq=2, price="120", qty="20", offset=2),
    )
    w = _ready_window(samples)
    for kind in IndicatorKind:
        assert isinstance(compute_indicator(kind, w), Decimal)


def test_compute_on_non_ready_raises() -> None:
    samples = (_sample(seq=1, price="100", qty="10", offset=1),)
    spec = _spec(lookback_events=3, min_events=2)
    w = evaluate_window(_history(samples), spec, now=_BASE + timedelta(seconds=1))
    assert w.readiness is IndicatorReadiness.WARMING
    for fn in (sma_price, return_bps, rolling_volume, vwap):
        with pytest.raises(IndicatorNotReadyError):
            fn(w)


def test_evaluate_window_does_not_mutate_snapshot() -> None:
    samples = tuple(_sample(seq=i, price="100", qty="10", offset=i) for i in range(1, 4))
    snap = _history(samples)
    before = snap.samples
    evaluate_window(snap, _spec(min_events=1), now=_BASE + timedelta(seconds=3))
    assert snap.samples is before  # snapshot tuple untouched
    assert len(snap.samples) == 3
