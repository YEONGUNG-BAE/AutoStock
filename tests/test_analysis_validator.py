from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import DateIdValidator, SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, DecisionId, FactType, Percent, StalenessPolicy
from analysis import (
    ANALYSIS_ALLOCATOR_TOLERANCE_VIOLATION,
    ANALYSIS_DATE_ID_FUTURE_SOURCE,
    ANALYSIS_DATE_ID_MISSING,
    ANALYSIS_DATE_ID_STALE,
    ANALYSIS_DECISION_SCHEMA,
    ANALYSIS_SCHEMA_INVALID,
    ANALYSIS_VALIDATOR_VERSION,
    AnalysisAction,
    AnalysisDecision,
    AnalysisDecisionValidator,
    AnalysisReason,
    BearPerspective,
    BullPerspective,
    FundManagerDecision,
    RiskManagerEvaluation,
    extract_date_ids_from_analysis_decision,
)


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def _sample_reason(**overrides: object) -> AnalysisReason:
    base = {
        "reason": "실적 둔화 우려",
        "date_id": DateId("260522-1"),
    }
    base.update(overrides)
    return AnalysisReason(**base)


def _sample_bear(**overrides: object) -> BearPerspective:
    base = {
        "summary": "하방 리스크 우세",
        "risks": ("수요 둔화",),
        "reasons": (_sample_reason(),),
    }
    base.update(overrides)
    return BearPerspective(**base)


def _sample_bull(**overrides: object) -> BullPerspective:
    base = {
        "summary": "성장 모멘텀 유지",
        "catalysts": ("신제품 출시",),
        "reasons": (_sample_reason(date_id="260522-2"),),
    }
    base.update(overrides)
    return BullPerspective(**base)


def _sample_risk_manager(**overrides: object) -> RiskManagerEvaluation:
    base = {
        "summary": "중립적 리스크 평가",
        "risk_flags": ("변동성 확대",),
        "max_weight_percent": Percent("5"),
        "reasons": (_sample_reason(date_id="260522-3"),),
    }
    base.update(overrides)
    return RiskManagerEvaluation(**base)


def _sample_fund_manager(**overrides: object) -> FundManagerDecision:
    base = {
        "action": AnalysisAction.HOLD,
        "target_weight_percent": Percent("5"),
        "rationale": "현재 비중 유지",
        "reasons": (_sample_reason(date_id="260522-4"),),
    }
    base.update(overrides)
    return FundManagerDecision(**base)


def _sample_decision(**overrides: object) -> AnalysisDecision:
    base = {
        "decision_id": DecisionId("analysis-260522-001"),
        "created_at": NOW,
        "universe": "KR_LARGE",
        "symbol": "005930",
        "market": "KR",
        "summary_one_liner": "방어적 HOLD",
        "bear": _sample_bear(),
        "bull": _sample_bull(),
        "risk_manager": _sample_risk_manager(),
        "fund_manager": _sample_fund_manager(),
        "reasons": (_sample_reason(date_id="260522-5"),),
    }
    base.update(overrides)
    return AnalysisDecision(**base)


def _sample_record(
    *,
    date_id: str = "260522-1",
    source_timestamp: datetime | None = None,
) -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.PRICE,
        source_name="yfinance",
        source_timestamp=source_timestamp or NOW,
        created_at=NOW,
        summary="sample fact",
        payload={"symbol": "005930"},
    )


def _store_with_records(tmp_path: Path, *records: DateIdSourceRecord) -> SQLiteDateIdSourceStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        for record in records:
            store.save_record(record)
    return store


def _validator(tmp_path: Path, *records: DateIdSourceRecord) -> AnalysisDecisionValidator:
    store = (
        _store_with_records(tmp_path, *records)
        if records
        else SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    )
    return AnalysisDecisionValidator(DateIdValidator(store, StalenessPolicy()))


def _records_for_decision(decision: AnalysisDecision) -> tuple[DateIdSourceRecord, ...]:
    unique_ids = {date_id.value for date_id in extract_date_ids_from_analysis_decision(decision)}
    return tuple(_sample_record(date_id=date_id) for date_id in sorted(unique_ids))


def test_validator_passes_for_valid_decision_with_fresh_date_id(tmp_path: Path) -> None:
    decision = _sample_decision()
    validator = _validator(tmp_path, *_records_for_decision(decision))

    result = validator.validate(decision, now=NOW)

    assert result.passed is True
    assert result.issues == ()
    assert result.schema_name == ANALYSIS_DECISION_SCHEMA
    assert result.validator_version == ANALYSIS_VALIDATOR_VERSION


def test_validator_reports_missing_date_id(tmp_path: Path) -> None:
    validator = _validator(tmp_path)

    result = validator.validate(_sample_decision(), now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ANALYSIS_DATE_ID_MISSING


def test_validator_reports_stale_date_id(tmp_path: Path) -> None:
    decision = _sample_decision()
    stale_records = tuple(
        _sample_record(
            date_id=date_id,
            source_timestamp=NOW - timedelta(hours=25),
        )
        for date_id in sorted(
            {item.value for item in extract_date_ids_from_analysis_decision(decision)}
        )
    )
    validator = _validator(tmp_path, *stale_records)

    result = validator.validate(decision, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ANALYSIS_DATE_ID_STALE


def test_validator_reports_future_source_timestamp(tmp_path: Path) -> None:
    future = _sample_record(source_timestamp=NOW + timedelta(hours=1))
    validator = _validator(tmp_path, future)

    result = validator.validate(_sample_decision(), now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ANALYSIS_DATE_ID_FUTURE_SOURCE


def test_validator_passes_within_allocator_tolerance_band(tmp_path: Path) -> None:
    decision = _sample_decision(
        fund_manager=_sample_fund_manager(target_weight_percent=Percent("7")),
    )
    validator = _validator(tmp_path, *_records_for_decision(decision))

    result = validator.validate(
        decision,
        now=NOW,
        allocator_target_weight=Percent("5"),
        tolerance_percent=Percent("5"),
    )

    assert result.passed is True


@pytest.mark.parametrize("target", ["0", "10"])
def test_validator_reports_allocator_tolerance_violation(tmp_path: Path, target: str) -> None:
    decision = _sample_decision(
        fund_manager=_sample_fund_manager(target_weight_percent=Percent(target)),
    )
    validator = _validator(tmp_path, *_records_for_decision(decision))

    result = validator.validate(
        decision,
        now=NOW,
        allocator_target_weight=Percent("5"),
        tolerance_percent=Percent("2"),
    )

    assert result.passed is False
    assert result.issues[0].code == ANALYSIS_ALLOCATOR_TOLERANCE_VIOLATION


def test_validator_rejects_partial_tolerance_context(tmp_path: Path) -> None:
    decision = _sample_decision()
    validator = _validator(tmp_path, *_records_for_decision(decision))

    with pytest.raises(ValueError, match="both be provided or both omitted"):
        validator.validate(
            decision,
            now=NOW,
            allocator_target_weight=Percent("5"),
        )


def test_validate_payload_schema_invalid_returns_none_decision(tmp_path: Path) -> None:
    validator = _validator(tmp_path)

    decision, result = validator.validate_payload({"decision_id": "bad"}, now=NOW)

    assert decision is None
    assert result.passed is False
    assert result.issues[0].code == ANALYSIS_SCHEMA_INVALID


def test_validate_payload_rejects_extra_trading_field(tmp_path: Path) -> None:
    decision = _sample_decision()
    validator = _validator(tmp_path, *_records_for_decision(decision))
    payload = decision.model_dump(mode="json")
    payload["side"] = "BUY"

    parsed, result = validator.validate_payload(payload, now=NOW)

    assert parsed is None
    assert result.issues[0].code == ANALYSIS_SCHEMA_INVALID


def test_validate_payload_invalid_date_id_is_schema_invalid(tmp_path: Path) -> None:
    validator = _validator(tmp_path)
    payload = _sample_decision().model_dump(mode="json")
    payload["reasons"][0]["date_id"] = "bad-id"

    decision, result = validator.validate_payload(payload, now=NOW)

    assert decision is None
    assert result.issues[0].code == ANALYSIS_SCHEMA_INVALID


def test_validate_payload_passes_for_valid_payload(tmp_path: Path) -> None:
    decision = _sample_decision()
    validator = _validator(tmp_path, *_records_for_decision(decision))

    parsed, result = validator.validate_payload(decision.model_dump(mode="json"), now=NOW)

    assert parsed is not None
    assert result.passed is True


def test_validator_rejects_naive_now(tmp_path: Path) -> None:
    decision = _sample_decision()
    validator = _validator(tmp_path, *_records_for_decision(decision))

    with pytest.raises(ValueError, match="timezone-aware datetime"):
        validator.validate(decision, now=NAIVE_NOW)


def test_validator_issue_ordering_is_deterministic(tmp_path: Path) -> None:
    validator = _validator(tmp_path)
    decision = _sample_decision(
        fund_manager=_sample_fund_manager(
            target_weight_percent=Percent("20"),
            reasons=(_sample_reason(date_id="260522-99"),),
        ),
        reasons=(_sample_reason(date_id="260522-99"),),
        bear=_sample_bear(reasons=(_sample_reason(date_id="260522-99"),)),
        bull=_sample_bull(reasons=(_sample_reason(date_id="260522-99"),)),
        risk_manager=_sample_risk_manager(reasons=(_sample_reason(date_id="260522-99"),)),
    )

    result = validator.validate(
        decision,
        now=NOW,
        allocator_target_weight=Percent("5"),
        tolerance_percent=Percent("2"),
    )

    assert result.passed is False
    codes = [issue.code for issue in result.issues]
    assert codes[0] == ANALYSIS_ALLOCATOR_TOLERANCE_VIOLATION
    assert all(code == ANALYSIS_DATE_ID_MISSING for code in codes[1:])


def test_extract_date_ids_preserves_order_and_duplicates() -> None:
    reason_a = _sample_reason(date_id="260522-1")
    reason_b = _sample_reason(date_id="260522-2")
    reason_c = _sample_reason(date_id="260522-1")
    reasons_ab = (reason_a, reason_b)
    reasons_c = (reason_c,)
    decision = _sample_decision(
        reasons=reasons_ab,
        bear=_sample_bear(reasons=reasons_c),
        bull=_sample_bull(reasons=reasons_ab),
        risk_manager=_sample_risk_manager(reasons=reasons_c),
        fund_manager=_sample_fund_manager(reasons=reasons_ab),
    )

    extracted = extract_date_ids_from_analysis_decision(decision)

    assert extracted == (
        DateId("260522-1"),
        DateId("260522-2"),
        DateId("260522-1"),
        DateId("260522-1"),
        DateId("260522-2"),
        DateId("260522-1"),
        DateId("260522-1"),
        DateId("260522-2"),
    )
