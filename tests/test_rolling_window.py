from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domain import Currency
from domain.enums import Market
from market_data.models import NormalizedTradeTick, ProviderSequence
from market_data.rolling_window import (
    EpochStartReason,
    RollingObserveStatus,
    RollingRetentionPolicy,
    RollingTradeHistoryStore,
)

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
SEC = timedelta(seconds=1)


def _policy(*, max_events: int = 100, max_age_seconds: str = "3600") -> RollingRetentionPolicy:
    return RollingRetentionPolicy(
        hard_max_events=max_events, hard_max_age_seconds=Decimal(max_age_seconds)
    )


def _store(*, max_events: int = 100, max_age_seconds: str = "3600") -> RollingTradeHistoryStore:
    return RollingTradeHistoryStore(
        retention=_policy(max_events=max_events, max_age_seconds=max_age_seconds)
    )


def _trade(
    *,
    price: str = "100",
    quantity: str = "1",
    sequence: int = 1,
    trade_at: datetime = NOW,
    received_at: datetime | None = None,
    provider: str = "kis",
    channel: str = "trade",
    symbol: str = "005930",
    market: Market = Market.KR,
) -> NormalizedTradeTick:
    return NormalizedTradeTick(
        provider=provider,
        symbol=symbol,
        market=market,
        currency=Currency.KRW,
        price=price,
        quantity=quantity,
        trade_at=trade_at,
        received_at=received_at or trade_at,
        provider_sequence=ProviderSequence(
            provider=provider, channel=channel, sequence=sequence, received_at=received_at or trade_at
        ),
    )


# --- RollingRetentionPolicy validation ---


@pytest.mark.parametrize("bad", [0, -1])
def test_retention_rejects_non_positive_max_events(bad: int) -> None:
    with pytest.raises(ValueError):
        RollingRetentionPolicy(hard_max_events=bad, hard_max_age_seconds=Decimal("10"))


def test_retention_rejects_bool_max_events() -> None:
    with pytest.raises(TypeError):
        RollingRetentionPolicy(hard_max_events=True, hard_max_age_seconds=Decimal("10"))


@pytest.mark.parametrize("bad", ["0", "-1", "NaN", "Infinity"])
def test_retention_rejects_invalid_age(bad: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        RollingRetentionPolicy(hard_max_events=10, hard_max_age_seconds=Decimal(bad))


def test_retention_rejects_non_decimal_age() -> None:
    with pytest.raises(TypeError):
        RollingRetentionPolicy(hard_max_events=10, hard_max_age_seconds=10)  # type: ignore[arg-type]


# --- peek before observe ---


def test_peek_before_observe_is_missing() -> None:
    snap = _store().peek_history(Market.KR, "005930", now=NOW)
    assert snap.was_ever_observed is False
    assert snap.samples == ()
    assert snap.continuity_epoch == 0
    assert snap.epoch_start_reason is EpochStartReason.INITIAL
    assert snap.latest_sequence is None
    assert snap.retention_truncated is False


# --- initial observe ---


def test_first_observe_starts_initial_epoch() -> None:
    store = _store()
    result = store.observe(_trade(sequence=5), now=NOW)
    assert result.status is RollingObserveStatus.APPLIED
    snap = store.peek_history(Market.KR, "005930", now=NOW)
    assert snap.was_ever_observed is True
    assert snap.continuity_epoch == 1
    assert snap.epoch_start_reason is EpochStartReason.INITIAL
    assert len(snap.samples) == 1
    assert snap.latest_sequence == 5
    assert snap.provider == "kis"
    assert snap.channel == "trade"


# --- ordering contract (mirrors RTM-2 latest store) ---


def test_duplicate_sequence_is_rejected_without_change() -> None:
    store = _store()
    store.observe(_trade(sequence=1), now=NOW)
    result = store.observe(_trade(sequence=1, trade_at=NOW + SEC), now=NOW + SEC)
    assert result.status is RollingObserveStatus.DUPLICATE
    assert len(store.peek_history(Market.KR, "005930", now=NOW + SEC).samples) == 1


def test_decreasing_sequence_is_out_of_order() -> None:
    store = _store()
    store.observe(_trade(sequence=5), now=NOW)
    result = store.observe(_trade(sequence=4, trade_at=NOW + SEC), now=NOW + SEC)
    assert result.status is RollingObserveStatus.OUT_OF_ORDER


def test_received_at_regression_is_out_of_order() -> None:
    store = _store()
    store.observe(_trade(sequence=1, trade_at=NOW, received_at=NOW + 5 * SEC), now=NOW + 5 * SEC)
    result = store.observe(
        _trade(sequence=2, trade_at=NOW + SEC, received_at=NOW + SEC), now=NOW + 5 * SEC
    )
    assert result.status is RollingObserveStatus.OUT_OF_ORDER


def test_event_time_regression_is_out_of_order() -> None:
    store = _store()
    store.observe(_trade(sequence=1, trade_at=NOW + 5 * SEC), now=NOW + 5 * SEC)
    result = store.observe(_trade(sequence=2, trade_at=NOW + SEC), now=NOW + 5 * SEC)
    assert result.status is RollingObserveStatus.OUT_OF_ORDER


def test_same_event_time_with_increasing_sequence_is_applied() -> None:
    # 같은 거래소 timestamp의 연속 체결은 sequence가 증가하면 허용해야 한다(RTM-2 일치).
    store = _store()
    store.observe(_trade(sequence=1, trade_at=NOW), now=NOW)
    result = store.observe(_trade(sequence=2, trade_at=NOW), now=NOW)
    assert result.status is RollingObserveStatus.APPLIED
    assert len(store.peek_history(Market.KR, "005930", now=NOW).samples) == 2


def test_future_event_is_rejected() -> None:
    store = _store()
    result = store.observe(_trade(sequence=1, trade_at=NOW + 5 * SEC), now=NOW)
    assert result.status is RollingObserveStatus.OUT_OF_ORDER
    assert store.peek_history(Market.KR, "005930", now=NOW).was_ever_observed is False


# --- stream identity: no automatic discard (보정1) ---


def test_stream_mismatch_is_rejected_without_change() -> None:
    store = _store()
    store.observe(_trade(sequence=1, channel="trade"), now=NOW)
    result = store.observe(
        _trade(sequence=2, channel="trade2", trade_at=NOW + SEC), now=NOW + SEC
    )
    assert result.status is RollingObserveStatus.STREAM_MISMATCH
    snap = store.peek_history(Market.KR, "005930", now=NOW + SEC)
    assert len(snap.samples) == 1
    assert snap.channel == "trade"
    assert snap.continuity_epoch == 1  # 변화 없음


# --- explicit reset preserves continuity metadata (보정2) ---


def test_reset_stream_clears_samples_but_preserves_epoch_metadata() -> None:
    store = _store()
    store.observe(_trade(sequence=1), now=NOW)
    store.observe(_trade(sequence=2, trade_at=NOW + SEC), now=NOW + SEC)
    store.reset_stream("kis", "trade")
    snap = store.peek_history(Market.KR, "005930", now=NOW + 2 * SEC)
    assert snap.samples == ()
    assert snap.was_ever_observed is True
    assert snap.continuity_epoch == 2
    assert snap.epoch_start_reason is EpochStartReason.EXPLICIT_RESET
    assert snap.provider is None
    assert snap.channel is None
    assert snap.latest_sequence is None


def test_observe_after_reset_adopts_new_stream_in_same_epoch() -> None:
    store = _store()
    store.observe(_trade(sequence=9), now=NOW)
    store.reset_stream("kis", "trade")
    # 재접속 후 sequence가 리셋되어 다시 1부터 와도 새 epoch에서 수용된다.
    result = store.observe(_trade(sequence=1, trade_at=NOW + 2 * SEC), now=NOW + 2 * SEC)
    assert result.status is RollingObserveStatus.APPLIED
    snap = store.peek_history(Market.KR, "005930", now=NOW + 2 * SEC)
    assert snap.continuity_epoch == 2
    assert snap.epoch_start_reason is EpochStartReason.EXPLICIT_RESET
    assert snap.latest_sequence == 1
    assert len(snap.samples) == 1


def test_reset_only_affects_matching_stream_identity() -> None:
    store = _store()
    store.observe(_trade(sequence=1, symbol="005930", channel="trade"), now=NOW)
    store.observe(_trade(sequence=1, symbol="000660", channel="other"), now=NOW)
    store.reset_stream("kis", "trade")
    a = store.peek_history(Market.KR, "005930", now=NOW)
    b = store.peek_history(Market.KR, "000660", now=NOW)
    assert a.samples == ()  # reset
    assert len(b.samples) == 1  # untouched


# --- retention truncation metadata (보정3 기반) ---


def test_count_cap_evicts_oldest_and_flags_truncation() -> None:
    store = _store(max_events=3)
    for i in range(5):
        store.observe(_trade(sequence=i + 1, trade_at=NOW + i * SEC), now=NOW + i * SEC)
    snap = store.peek_history(Market.KR, "005930", now=NOW + 5 * SEC)
    assert len(snap.samples) == 3
    assert snap.samples[0].sequence == 3  # 1,2 evicted
    assert snap.evicted_event_count == 2
    assert snap.evicted_through_event_time == NOW + SEC  # seq=2 event time
    assert snap.retention_truncated is True


def test_age_cap_evicts_old_samples() -> None:
    store = _store(max_events=1000, max_age_seconds="5")
    store.observe(_trade(sequence=1, trade_at=NOW), now=NOW)
    store.observe(_trade(sequence=2, trade_at=NOW + 3 * SEC), now=NOW + 3 * SEC)
    # 최신 event_time=NOW+10s → cutoff=NOW+5s; NOW, NOW+3s 모두 evict.
    store.observe(_trade(sequence=3, trade_at=NOW + 10 * SEC), now=NOW + 10 * SEC)
    snap = store.peek_history(Market.KR, "005930", now=NOW + 10 * SEC)
    assert len(snap.samples) == 1
    assert snap.samples[0].sequence == 3
    assert snap.evicted_event_count == 2
    assert snap.retention_truncated is True


def test_age_cap_keeps_at_least_latest_sample() -> None:
    store = _store(max_events=1000, max_age_seconds="1")
    store.observe(_trade(sequence=1, trade_at=NOW), now=NOW)
    store.observe(_trade(sequence=2, trade_at=NOW + 100 * SEC), now=NOW + 100 * SEC)
    snap = store.peek_history(Market.KR, "005930", now=NOW + 100 * SEC)
    assert len(snap.samples) == 1
    assert snap.samples[0].sequence == 2


# --- immutability of snapshot ---


def test_peek_returns_immutable_copy() -> None:
    store = _store()
    store.observe(_trade(sequence=1), now=NOW)
    snap = store.peek_history(Market.KR, "005930", now=NOW)
    assert isinstance(snap.samples, tuple)
    store.observe(_trade(sequence=2, trade_at=NOW + SEC), now=NOW + SEC)
    # 이전 스냅샷은 영향받지 않는다.
    assert len(snap.samples) == 1


# --- concurrency: lock atomicity ---


def test_concurrent_observe_is_consistent() -> None:
    store = _store(max_events=10_000)
    n = 200
    barrier = threading.Barrier(8)
    seqs = list(range(1, n + 1))
    chunks = [seqs[i::8] for i in range(8)]

    def worker(my: list[int]) -> None:
        barrier.wait()
        for s in my:
            store.observe(
                _trade(sequence=s, trade_at=NOW + s * SEC, received_at=NOW + s * SEC),
                now=NOW + (n + 1) * SEC,
            )

    threads = [threading.Thread(target=worker, args=(c,)) for c in chunks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = store.peek_history(Market.KR, "005930", now=NOW + (n + 1) * SEC)
    # 동시 삽입 순서는 비결정적이나, 손상 없이 단조 증가 sequence만 남아야 한다.
    seqs_kept = [s.sequence for s in snap.samples]
    assert seqs_kept == sorted(seqs_kept)
    assert len(set(seqs_kept)) == len(seqs_kept)  # 중복 없음
    assert snap.latest_sequence == seqs_kept[-1]
