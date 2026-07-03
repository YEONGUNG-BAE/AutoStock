from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backtest_data import AsOfFilteredSourceView
from domain import DateId, DateIdSourceRecord, FactType
from scout import ScoutInputBuilder

DECISION_TIME = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _record(
    *,
    date_id: str,
    source_timestamp: datetime,
    fact_type: FactType = FactType.PRICE,
    symbol: str | None = "SYN_US_PROXY",
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=fact_type,
        source_name="synthetic_fixture_v1",
        source_timestamp=source_timestamp,
        created_at=source_timestamp,
        summary="synthetic record",
        payload={},
        symbol=symbol,
        market="US",
    )


def _five_records_around_decision_time() -> tuple[DateIdSourceRecord, ...]:
    return (
        _record(date_id="260520-1", source_timestamp=DECISION_TIME - timedelta(days=2)),
        _record(date_id="260521-1", source_timestamp=DECISION_TIME - timedelta(days=1)),
        _record(date_id="260522-1", source_timestamp=DECISION_TIME),
        _record(date_id="260523-1", source_timestamp=DECISION_TIME + timedelta(days=1)),
        _record(date_id="260524-1", source_timestamp=DECISION_TIME + timedelta(days=2)),
    )


class _FakeStore:
    """list_records()만 노출하는 read-only store shape."""

    def __init__(self, records: tuple[DateIdSourceRecord, ...]) -> None:
        self.records = records

    def list_records(self, fact_type: FactType | None = None) -> tuple[DateIdSourceRecord, ...]:
        if fact_type is None:
            return self.records
        return tuple(record for record in self.records if record.fact_type == fact_type)


# 1. d-2, d-1, d, d+1, d+2 with cutoff d → only d-2, d-1, d.
def test_guard_returns_only_records_at_or_before_decision_time() -> None:
    view = AsOfFilteredSourceView(_five_records_around_decision_time(), decision_time=DECISION_TIME)

    filtered = view.list_records()

    assert [record.date_id.value for record in filtered] == ["260520-1", "260521-1", "260522-1"]


# 2. Record exactly at source_timestamp == d is included (inclusive boundary).
def test_boundary_record_at_decision_time_is_included() -> None:
    exact = _record(date_id="260522-1", source_timestamp=DECISION_TIME)
    view = AsOfFilteredSourceView((exact,), decision_time=DECISION_TIME)

    assert view.list_records() == (exact,)


# 3. Record one instant after d is excluded.
def test_record_one_instant_after_decision_time_is_excluded() -> None:
    just_after = _record(
        date_id="260522-1",
        source_timestamp=DECISION_TIME + timedelta(microseconds=1),
    )
    view = AsOfFilteredSourceView((just_after,), decision_time=DECISION_TIME)

    assert view.list_records() == ()


# 4. All-future-dated input returns empty result, no crash.
def test_all_future_records_yield_empty_result() -> None:
    futures = tuple(
        _record(date_id=f"26052{index}-1", source_timestamp=DECISION_TIME + timedelta(days=index))
        for index in range(3, 6)
    )
    view = AsOfFilteredSourceView(futures, decision_time=DECISION_TIME)

    assert view.list_records() == ()


# 5. Original store/list is not mutated.
def test_guard_does_not_mutate_original_store_or_list() -> None:
    records = _five_records_around_decision_time()
    records_list = list(records)
    store = _FakeStore(records)

    AsOfFilteredSourceView(records_list, decision_time=DECISION_TIME).list_records()
    AsOfFilteredSourceView(store, decision_time=DECISION_TIME).list_records()

    assert tuple(records_list) == records
    assert store.records == records
    assert store.list_records() == records


# 6. Guard exposes a list_records()-style interface compatible with
#    ScoutInputBuilder (accepts a store shape too).
def test_guard_accepts_store_shape_and_exposes_reader_protocol() -> None:
    store = _FakeStore(_five_records_around_decision_time())
    view = AsOfFilteredSourceView(store, decision_time=DECISION_TIME)

    assert hasattr(view, "list_records")
    assert [record.date_id.value for record in view.list_records()] == [
        "260520-1",
        "260521-1",
        "260522-1",
    ]


# 7. Feeding the guard's view into the unmodified ScoutInputBuilder yields a
#    ScoutInput with no future-dated records.
def test_guard_composes_with_unmodified_scout_input_builder() -> None:
    view = AsOfFilteredSourceView(_five_records_around_decision_time(), decision_time=DECISION_TIME)
    builder = ScoutInputBuilder(view)

    scout_input = builder.build_input(universe="US", now=DECISION_TIME)

    assert len(scout_input.records) == 3
    assert all(record.source_timestamp <= DECISION_TIME for record in scout_input.records)
    # without the guard, the builder would include future-dated records
    unguarded = ScoutInputBuilder(_FakeStore(_five_records_around_decision_time()))
    unguarded_input = unguarded.build_input(universe="US", now=DECISION_TIME)
    assert any(record.source_timestamp > DECISION_TIME for record in unguarded_input.records)


# 8. Determinism: same inputs produce same filtered output.
def test_guard_is_deterministic() -> None:
    records = _five_records_around_decision_time()
    first = AsOfFilteredSourceView(records, decision_time=DECISION_TIME).list_records()
    second = AsOfFilteredSourceView(records, decision_time=DECISION_TIME).list_records()
    shuffled = AsOfFilteredSourceView(tuple(reversed(records)), decision_time=DECISION_TIME)

    assert first == second
    assert shuffled.list_records() == first


# 9. fact_type filter preserves the existing reader protocol expectation.
def test_guard_fact_type_filter_matches_reader_protocol() -> None:
    price = _record(date_id="260521-1", source_timestamp=DECISION_TIME - timedelta(days=1))
    fx = _record(
        date_id="260521-2",
        source_timestamp=DECISION_TIME - timedelta(hours=1),
        fact_type=FactType.FX,
    )
    future_fx = _record(
        date_id="260523-1",
        source_timestamp=DECISION_TIME + timedelta(days=1),
        fact_type=FactType.FX,
    )
    view = AsOfFilteredSourceView((price, fx, future_fx), decision_time=DECISION_TIME)

    fx_records = view.list_records(fact_type=FactType.FX)

    assert [record.date_id.value for record in fx_records] == ["260521-2"]
    assert view.list_records(fact_type=None) == (price, fx)


def test_guard_rejects_naive_decision_time() -> None:
    with pytest.raises(ValueError, match="decision_time"):
        AsOfFilteredSourceView((), decision_time=datetime(2026, 5, 22, 12, 0))


def test_guard_rejects_non_record_items() -> None:
    with pytest.raises(ValueError, match="DateIdSourceRecord"):
        AsOfFilteredSourceView(("not-a-record",), decision_time=DECISION_TIME)  # type: ignore[arg-type]
