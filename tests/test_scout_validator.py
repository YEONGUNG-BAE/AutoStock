from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import DateIdValidator, SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, FactType, StalenessPolicy
from scout import (
    SCOUT_SCHEMA_INVALID,
    SCOUT_SUMMARY_SCHEMA,
    SCOUT_SUMMARY_VALIDATOR_VERSION,
    ScoutFactor,
    ScoutReason,
    ScoutSummary,
    ScoutSummaryValidator,
    extract_date_ids_from_scout_summary,
)
from domain import DecisionId


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def _sample_record(
    *,
    date_id: str = "260522-1",
    fact_type: FactType = FactType.PRICE,
    source_timestamp: datetime | None = None,
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=fact_type,
        source_name="yfinance",
        source_timestamp=source_timestamp or NOW,
        created_at=NOW,
        summary="sample fact",
        payload={"symbol": "AAPL"},
    )


def _store_with_records(tmp_path: Path, *records: DateIdSourceRecord) -> SQLiteDateIdSourceStore:
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        for record in records:
            store.save_record(record)
    return store


def _sample_summary(**overrides: object) -> ScoutSummary:
    base = {
        "summary_id": DecisionId("scout-260522-001"),
        "created_at": NOW,
        "universe": "US",
        "summary_one_liner": "매크로 불확실성이 높아진 상태다.",
        "positive_factors": (
            ScoutFactor(
                name="positive",
                summary="positive summary",
                reasons=(
                    ScoutReason(
                        reason="fresh evidence",
                        date_id=DateId("260522-1"),
                    ),
                ),
            ),
        ),
    }
    base.update(overrides)
    return ScoutSummary(**base)


def _validator(tmp_path: Path, *records: DateIdSourceRecord) -> ScoutSummaryValidator:
    store = _store_with_records(tmp_path, *records) if records else SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    return ScoutSummaryValidator(DateIdValidator(store, StalenessPolicy()))


def test_validator_passes_for_valid_summary_with_fresh_date_id(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())

    result = validator.validate(_sample_summary(), now=NOW)

    assert result.passed is True
    assert result.issues == ()
    assert result.schema_name == SCOUT_SUMMARY_SCHEMA
    assert result.validator_version == SCOUT_SUMMARY_VALIDATOR_VERSION


def test_validator_reports_missing_store_date_id(tmp_path: Path) -> None:
    validator = _validator(tmp_path)

    result = validator.validate(_sample_summary(), now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_MISSING"
    assert "260522-1" in result.issues[0].message


def test_validator_reports_stale_date_id(tmp_path: Path) -> None:
    stale = _sample_record(source_timestamp=NOW - timedelta(hours=25))
    validator = _validator(tmp_path, stale)

    result = validator.validate(_sample_summary(), now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_STALE"


def test_validator_reports_future_source_timestamp(tmp_path: Path) -> None:
    future = _sample_record(source_timestamp=NOW + timedelta(hours=1))
    validator = _validator(tmp_path, future)

    result = validator.validate(_sample_summary(), now=NOW)

    assert result.passed is False
    assert result.issues[0].code == "DATE_ID_FUTURE_SOURCE"


def test_validate_payload_schema_invalid_returns_none_summary(tmp_path: Path) -> None:
    validator = _validator(tmp_path)

    summary, result = validator.validate_payload({"summary_id": "bad"}, now=NOW)

    assert summary is None
    assert result.passed is False
    assert len(result.issues) == 1
    assert result.issues[0].code == SCOUT_SCHEMA_INVALID


def test_validate_payload_rejects_extra_trading_action_field(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())
    payload = _sample_summary().model_dump(mode="json")
    payload["order_intent"] = {"side": "BUY"}

    summary, result = validator.validate_payload(payload, now=NOW)

    assert summary is None
    assert result.issues[0].code == SCOUT_SCHEMA_INVALID


def test_validate_payload_invalid_date_id_is_schema_invalid(tmp_path: Path) -> None:
    validator = _validator(tmp_path)
    payload = _sample_summary().model_dump(mode="json")
    payload["positive_factors"][0]["reasons"][0]["date_id"] = "bad-id"

    summary, result = validator.validate_payload(payload, now=NOW)

    assert summary is None
    assert result.issues[0].code == SCOUT_SCHEMA_INVALID


def test_validate_payload_passes_for_valid_payload(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())

    summary, result = validator.validate_payload(_sample_summary().model_dump(mode="json"), now=NOW)

    assert summary is not None
    assert result.passed is True


def test_validator_issue_ordering_is_deterministic(tmp_path: Path) -> None:
    fresh = _sample_record(date_id="260522-1")
    stale = _sample_record(date_id="260522-2", source_timestamp=NOW - timedelta(hours=25))
    validator = _validator(tmp_path, fresh, stale)
    summary = _sample_summary(
        positive_factors=(
            ScoutFactor(
                name="mixed",
                summary="mixed",
                reasons=(
                    ScoutReason(reason="missing", date_id=DateId("260522-99")),
                    ScoutReason(reason="stale", date_id=DateId("260522-2")),
                    ScoutReason(reason="fresh", date_id=DateId("260522-1")),
                ),
            ),
        ),
    )

    result = validator.validate(summary, now=NOW)

    assert result.passed is False
    assert [issue.code for issue in result.issues] == ["DATE_ID_MISSING", "DATE_ID_STALE"]


def test_validator_rejects_naive_now(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())

    with pytest.raises(ValueError, match="timezone-aware datetime"):
        validator.validate(_sample_summary(), now=NAIVE_NOW)


def test_extract_date_ids_preserves_group_and_reason_order() -> None:
    summary = _sample_summary(
        positive_factors=(
            ScoutFactor(
                name="pos",
                summary="pos",
                reasons=(
                    ScoutReason(reason="p1", date_id=DateId("260522-1")),
                    ScoutReason(reason="p2", date_id=DateId("260522-1")),
                ),
            ),
        ),
        negative_factors=(
            ScoutFactor(
                name="neg",
                summary="neg",
                reasons=(ScoutReason(reason="n1", date_id=DateId("260522-2")),),
            ),
        ),
        neutral_factors=(
            ScoutFactor(
                name="neu",
                summary="neu",
                reasons=(ScoutReason(reason="u1", date_id=DateId("260522-3")),),
            ),
        ),
    )

    extracted = extract_date_ids_from_scout_summary(summary)

    assert extracted == (
        DateId("260522-1"),
        DateId("260522-1"),
        DateId("260522-2"),
        DateId("260522-3"),
    )


def test_extract_date_ids_handles_empty_groups() -> None:
    summary = _sample_summary(negative_factors=(), neutral_factors=())
    extracted = extract_date_ids_from_scout_summary(summary)
    assert extracted == (DateId("260522-1"),)
