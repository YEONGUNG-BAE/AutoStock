from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain import DateId, DateIdSourceRecord, FactType, StalenessPolicy


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def _sample_record(**overrides: object) -> DateIdSourceRecord:
    base = {
        "date_id": DateId("260522-1"),
        "fact_type": FactType.PRICE,
        "source_name": "yfinance",
        "source_timestamp": NOW,
        "created_at": NOW,
        "summary": "AAPL close price",
        "payload": {"symbol": "AAPL", "close": "190.25"},
    }
    base.update(overrides)
    return DateIdSourceRecord(**base)


def test_fact_type_values() -> None:
    assert FactType.PRICE.value == "price"
    assert FactType.FLOW.value == "flow"
    assert FactType.FX.value == "fx"
    assert FactType.NEWS.value == "news"
    assert FactType.DISCLOSURE.value == "disclosure"
    assert FactType.MACRO.value == "macro"
    assert FactType.MANUAL.value == "manual"
    assert len(FactType) == 7


def test_fact_type_rejects_invalid_value() -> None:
    with pytest.raises(ValueError):
        FactType("invalid")


def test_date_id_source_record_accepts_valid_record() -> None:
    record = _sample_record(symbol="AAPL", market="US", source_url="https://example.com")
    assert record.date_id.value == "260522-1"
    assert record.fact_type == FactType.PRICE
    assert record.payload["symbol"] == "AAPL"


@pytest.mark.parametrize("field_name", ["source_name", "summary"])
def test_date_id_source_record_rejects_blank_required_fields(field_name: str) -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_record(**{field_name: " "})


@pytest.mark.parametrize("field_name", ["symbol", "market", "source_url"])
def test_date_id_source_record_rejects_blank_optional_fields(field_name: str) -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        _sample_record(**{field_name: " "})


def test_date_id_source_record_rejects_naive_source_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        _sample_record(source_timestamp=NAIVE_NOW)


def test_date_id_source_record_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware datetime"):
        _sample_record(created_at=NAIVE_NOW)


def test_date_id_source_record_rejects_invalid_payload() -> None:
    with pytest.raises(ValidationError, match="float values are not allowed"):
        _sample_record(payload={"bad": 1.5})


def test_date_id_source_record_canonical_payload_roundtrip() -> None:
    record = _sample_record(payload={"b": 2, "a": {"y": "2", "x": "1"}})
    assert record.payload == {"a": {"x": "1", "y": "2"}, "b": 2}


def test_staleness_policy_default_ages() -> None:
    policy = StalenessPolicy()
    assert policy.allowed_age_for(FactType.PRICE) == timedelta(hours=24)
    assert policy.allowed_age_for(FactType.NEWS) == timedelta(days=7)
    assert policy.allowed_age_for(FactType.DISCLOSURE) == timedelta(days=90)
    assert policy.allowed_age_for(FactType.MACRO) == timedelta(days=30)
    assert policy.allowed_age_for(FactType.MANUAL) == timedelta(days=365)


@pytest.mark.parametrize(
    "fact_type, age_delta, expected_stale",
    [
        (FactType.PRICE, timedelta(hours=23), False),
        (FactType.PRICE, timedelta(hours=25), True),
        (FactType.NEWS, timedelta(days=6), False),
        (FactType.NEWS, timedelta(days=8), True),
        (FactType.DISCLOSURE, timedelta(days=89), False),
        (FactType.DISCLOSURE, timedelta(days=91), True),
        (FactType.MACRO, timedelta(days=31), True),
    ],
)
def test_staleness_policy_is_stale(
    fact_type: FactType,
    age_delta: timedelta,
    expected_stale: bool,
) -> None:
    policy = StalenessPolicy()
    record = _sample_record(
        fact_type=fact_type,
        source_timestamp=NOW - age_delta,
    )
    assert policy.is_stale(record, now=NOW) is expected_stale


def test_staleness_policy_boundary_age_equals_allowed_age_is_fresh() -> None:
    policy = StalenessPolicy()
    record = _sample_record(source_timestamp=NOW - timedelta(hours=24))
    assert policy.is_stale(record, now=NOW) is False


def test_staleness_policy_rejects_naive_now() -> None:
    policy = StalenessPolicy()
    record = _sample_record()
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        policy.is_stale(record, now=NAIVE_NOW)


def test_staleness_policy_rejects_future_source_timestamp() -> None:
    policy = StalenessPolicy()
    record = _sample_record(source_timestamp=NOW + timedelta(hours=1))
    with pytest.raises(ValueError, match="future"):
        policy.age(record, NOW)
