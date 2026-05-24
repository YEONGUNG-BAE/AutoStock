from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import DateIdValidator, SQLiteDateIdSourceStore
from decision.canonical_json import payload_sha256
from domain import DateId, DateIdSourceRecord, FactType, StalenessPolicy
from scout import (
    ScoutFactor,
    ScoutInputBuilder,
    ScoutReason,
    ScoutSummary,
    ScoutSummaryValidator,
)
from domain import DecisionId


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _sample_record(
    *,
    date_id: str,
    source_timestamp: datetime | None = None,
    symbol: str | None = "AAPL",
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.PRICE,
        source_name="yfinance",
        source_timestamp=source_timestamp or NOW,
        created_at=NOW,
        summary="sample",
        payload={"symbol": symbol},
        symbol=symbol,
    )


def _store_with_records(tmp_path: Path, *records: DateIdSourceRecord) -> SQLiteDateIdSourceStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        for record in records:
            store.save_record(record)
    return store


def test_replay_builder_output_is_stable_across_store_insertion_order(tmp_path: Path) -> None:
    older = _sample_record(
        date_id="260522-1",
        source_timestamp=NOW - timedelta(hours=2),
        symbol="AAPL",
    )
    newer = _sample_record(
        date_id="260522-2",
        source_timestamp=NOW - timedelta(hours=1),
        symbol="MSFT",
    )
    store_a = _store_with_records(tmp_path / "a", older, newer)
    store_b = _store_with_records(tmp_path / "b", newer, older)

    input_a = ScoutInputBuilder(store_a).build_input(universe="US", now=NOW)
    input_b = ScoutInputBuilder(store_b).build_input(universe="US", now=NOW)

    assert input_a.to_canonical_dict() == input_b.to_canonical_dict()
    assert payload_sha256(input_a.to_canonical_dict()) == payload_sha256(input_b.to_canonical_dict())
    store_a.close()
    store_b.close()


def test_replay_scout_summary_canonical_payload_is_key_order_independent() -> None:
    summary_a = ScoutSummary(
        summary_id=DecisionId("scout-replay-a"),
        created_at=NOW,
        universe="US",
        summary_one_liner="replay test",
        positive_factors=(
            ScoutFactor(
                name="factor",
                summary="summary",
                reasons=(ScoutReason(reason="reason", date_id=DateId("260522-1")),),
            ),
        ),
        metadata={"b": "2", "a": "1"},
    )
    raw_a = {"b": "2", "a": {"y": "2", "x": "1"}}
    raw_b = {"a": {"x": "1", "y": "2"}, "b": "2"}
    from decision.canonical_json import canonicalize_payload

    assert canonicalize_payload(raw_a) == canonicalize_payload(raw_b)

    canonical = summary_a.to_canonical_dict()
    assert canonical["metadata"] == {"a": "1", "b": "2"}


def test_replay_validate_payload_result_is_deterministic(tmp_path: Path) -> None:
    store = _store_with_records(tmp_path, _sample_record(date_id="260522-1"))
    validator = ScoutSummaryValidator(DateIdValidator(store, StalenessPolicy()))
    payload = {
        "summary_id": "scout-replay",
        "created_at": NOW.isoformat(),
        "universe": "US",
        "summary_one_liner": "replay",
        "positive_factors": [
            {
                "name": "factor",
                "summary": "summary",
                "reasons": [{"reason": "reason", "date_id": "260522-1"}],
            }
        ],
    }

    first_summary, first_result = validator.validate_payload(payload, now=NOW)
    second_summary, second_result = validator.validate_payload(payload, now=NOW)

    assert first_summary is not None
    assert second_summary is not None
    assert first_summary.to_canonical_dict() == second_summary.to_canonical_dict()
    assert first_result.to_canonical_dict() == second_result.to_canonical_dict()
    store.close()


def test_replay_scout_summary_preserves_reason_order_in_canonical_dict() -> None:
    summary = ScoutSummary(
        summary_id=DecisionId("scout-replay-order"),
        created_at=NOW,
        universe="US",
        summary_one_liner="order test",
        positive_factors=(
            ScoutFactor(
                name="factor",
                summary="summary",
                reasons=(
                    ScoutReason(reason="first", date_id=DateId("260522-1")),
                    ScoutReason(reason="second", date_id=DateId("260522-2")),
                ),
            ),
        ),
    )

    canonical = summary.to_canonical_dict()
    reasons = canonical["positive_factors"][0]["reasons"]
    assert [item["reason"] for item in reasons] == ["first", "second"]
