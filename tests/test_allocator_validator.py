from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import DateIdValidator, SQLiteDateIdSourceStore
from domain import DateId, DateIdSourceRecord, DecisionId, FactType, Percent, StalenessPolicy
from allocator import (
    ALLOCATOR_CASH_TARGET_BAND_VIOLATION,
    ALLOCATOR_CASH_TARGET_MISMATCH,
    ALLOCATOR_CONSISTENCY_CHECK_FAILED,
    ALLOCATOR_DATE_ID_FUTURE_SOURCE,
    ALLOCATOR_DATE_ID_MISSING,
    ALLOCATOR_DATE_ID_STALE,
    ALLOCATOR_DECISION_SCHEMA,
    ALLOCATOR_GOLD_BAND_VIOLATION,
    ALLOCATOR_SCHEMA_INVALID,
    ALLOCATOR_TARGET_WEIGHTS_MISMATCH,
    ALLOCATOR_TARGET_WEIGHTS_SUM_INVALID,
    ALLOCATOR_VALIDATOR_VERSION,
    AllocatorDecision,
    AllocatorDecisionValidator,
    AllocatorReason,
    AssetAllocatorView,
    CashManagerView,
    CashPolicy,
    ConsistencyCheckerView,
    GoldPolicyMode,
    SignalSummary,
    TargetWeights,
    extract_date_ids_from_allocator_decision,
)


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 5, 22, 12, 0)


def _sample_reason(**overrides: object) -> AllocatorReason:
    base = {
        "reason": "VIX 상승으로 방어적 현금 비중 유지",
        "date_id": DateId("260522-1"),
    }
    base.update(overrides)
    return AllocatorReason(**base)


def _weights_with_gold(gold: str, us: str = "30") -> TargetWeights:
    """gold 값을 주면 kr/us/gold 합계가 100이 되도록 TargetWeights를 생성한다."""
    gold_p = Percent(gold)
    us_p = Percent(us)
    kr_value = Decimal("100") - gold_p.value - us_p.value
    return TargetWeights(kr=Percent(str(kr_value)), us=us_p, gold=gold_p)


def _sample_weights(**overrides: object) -> TargetWeights:
    base = {"kr": Percent("50"), "us": Percent("30"), "gold": Percent("20")}
    base.update(overrides)
    return TargetWeights(**base)


def _sample_decision(**overrides: object) -> AllocatorDecision:
    weights = _sample_weights()
    cash = Percent("20")
    reasons = (_sample_reason(),)
    base = {
        "decision_id": DecisionId("allocator-260522-001"),
        "created_at": NOW,
        "universe": "ALL",
        "summary_one_liner": "균형 배분 유지",
        "gold_policy_mode": GoldPolicyMode.NORMAL,
        "signal_summary": SignalSummary(summary="시장 신호 요약", reasons=reasons),
        "cash_manager": CashManagerView(
            summary="현금 20% 유지",
            recommended_cash_percent=cash,
            reasons=reasons,
        ),
        "asset_allocator": AssetAllocatorView(
            summary="KR/US/Gold 균형",
            target_weights=weights,
            reasons=reasons,
        ),
        "consistency_checker": ConsistencyCheckerView(
            passed=True,
            summary="일관성 확인 완료",
            reasons=reasons,
        ),
        "cash_policy": CashPolicy(
            cash_target_percent=cash,
            rationale="유동성 확보",
            reasons=reasons,
        ),
        "target_weights": weights,
        "reasons": reasons,
    }
    base.update(overrides)
    return AllocatorDecision(**base)


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
        payload={"symbol": "AAPL"},
    )


def _store_with_records(tmp_path: Path, *records: DateIdSourceRecord) -> SQLiteDateIdSourceStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        for record in records:
            store.save_record(record)
    return store


def _validator(tmp_path: Path, *records: DateIdSourceRecord) -> AllocatorDecisionValidator:
    store = (
        _store_with_records(tmp_path, *records)
        if records
        else SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    )
    return AllocatorDecisionValidator(DateIdValidator(store, StalenessPolicy()))


def test_validator_passes_for_valid_decision_with_fresh_date_id(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())

    result = validator.validate(_sample_decision(), now=NOW)

    assert result.passed is True
    assert result.issues == ()
    assert result.schema_name == ALLOCATOR_DECISION_SCHEMA
    assert result.validator_version == ALLOCATOR_VALIDATOR_VERSION


def test_validator_reports_missing_date_id(tmp_path: Path) -> None:
    validator = _validator(tmp_path)

    result = validator.validate(_sample_decision(), now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ALLOCATOR_DATE_ID_MISSING


def test_validator_reports_stale_date_id(tmp_path: Path) -> None:
    stale = _sample_record(source_timestamp=NOW - timedelta(hours=25))
    validator = _validator(tmp_path, stale)

    result = validator.validate(_sample_decision(), now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ALLOCATOR_DATE_ID_STALE


def test_validator_reports_future_source_timestamp(tmp_path: Path) -> None:
    future = _sample_record(source_timestamp=NOW + timedelta(hours=1))
    validator = _validator(tmp_path, future)

    result = validator.validate(_sample_decision(), now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ALLOCATOR_DATE_ID_FUTURE_SOURCE


def test_validator_reports_target_weights_sum_invalid(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())
    bad_weights = _sample_weights(kr=Percent("50"), us=Percent("30"), gold=Percent("19"))
    decision = _sample_decision(target_weights=bad_weights, asset_allocator=AssetAllocatorView(
        summary="alloc",
        target_weights=bad_weights,
        reasons=(_sample_reason(),),
    ))

    result = validator.validate(decision, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ALLOCATOR_TARGET_WEIGHTS_SUM_INVALID


def test_validator_normal_gold_18_and_22_are_valid(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())
    for gold_value in ("18", "22"):
        weights = _weights_with_gold(gold_value)
        decision = _sample_decision(
            target_weights=weights,
            asset_allocator=AssetAllocatorView(
                summary="alloc",
                target_weights=weights,
                reasons=(_sample_reason(),),
            ),
        )
        result = validator.validate(decision, now=NOW)
        assert result.passed is True, f"gold={gold_value} should pass normal band"


@pytest.mark.parametrize("gold_value", ["17", "23"])
def test_validator_normal_gold_outside_18_22_is_invalid(tmp_path: Path, gold_value: str) -> None:
    validator = _validator(tmp_path, _sample_record())
    weights = _weights_with_gold(gold_value)
    decision = _sample_decision(
        target_weights=weights,
        asset_allocator=AssetAllocatorView(
            summary="alloc",
            target_weights=weights,
            reasons=(_sample_reason(),),
        ),
    )

    result = validator.validate(decision, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ALLOCATOR_GOLD_BAND_VIOLATION


@pytest.mark.parametrize("gold_value", ["15", "25"])
def test_validator_exception_gold_15_and_25_are_valid(tmp_path: Path, gold_value: str) -> None:
    validator = _validator(tmp_path, _sample_record())
    weights = _weights_with_gold(gold_value)
    decision = _sample_decision(
        gold_policy_mode=GoldPolicyMode.EXCEPTION,
        target_weights=weights,
        asset_allocator=AssetAllocatorView(
            summary="alloc",
            target_weights=weights,
            reasons=(_sample_reason(),),
        ),
    )

    result = validator.validate(decision, now=NOW)

    assert result.passed is True


@pytest.mark.parametrize("gold_value", ["14", "26"])
def test_validator_exception_gold_outside_15_25_is_invalid(tmp_path: Path, gold_value: str) -> None:
    validator = _validator(tmp_path, _sample_record())
    weights = _weights_with_gold(gold_value)
    decision = _sample_decision(
        gold_policy_mode=GoldPolicyMode.EXCEPTION,
        target_weights=weights,
        asset_allocator=AssetAllocatorView(
            summary="alloc",
            target_weights=weights,
            reasons=(_sample_reason(),),
        ),
    )

    result = validator.validate(decision, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ALLOCATOR_GOLD_BAND_VIOLATION


@pytest.mark.parametrize("cash_value", ["9", "31"])
def test_validator_cash_target_outside_10_30_is_invalid(tmp_path: Path, cash_value: str) -> None:
    validator = _validator(tmp_path, _sample_record())
    cash = Percent(cash_value)
    decision = _sample_decision(
        cash_policy=CashPolicy(
            cash_target_percent=cash,
            rationale="bad cash",
            reasons=(_sample_reason(),),
        ),
        cash_manager=CashManagerView(
            summary="cash",
            recommended_cash_percent=cash,
            reasons=(_sample_reason(),),
        ),
    )

    result = validator.validate(decision, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ALLOCATOR_CASH_TARGET_BAND_VIOLATION


def test_validator_reports_target_weights_mismatch(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())
    top = _sample_weights()
    inner = _sample_weights(kr=Percent("40"))
    decision = _sample_decision(
        target_weights=top,
        asset_allocator=AssetAllocatorView(
            summary="alloc",
            target_weights=inner,
            reasons=(_sample_reason(),),
        ),
    )

    result = validator.validate(decision, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ALLOCATOR_TARGET_WEIGHTS_MISMATCH


def test_validator_reports_cash_target_mismatch(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())
    decision = _sample_decision(
        cash_policy=CashPolicy(
            cash_target_percent=Percent("20"),
            rationale="cash policy",
            reasons=(_sample_reason(),),
        ),
        cash_manager=CashManagerView(
            summary="cash",
            recommended_cash_percent=Percent("25"),
            reasons=(_sample_reason(),),
        ),
    )

    result = validator.validate(decision, now=NOW)

    assert result.passed is False
    assert result.issues[0].code == ALLOCATOR_CASH_TARGET_MISMATCH


def test_validator_reports_consistency_checker_failed(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())
    decision = _sample_decision(
        consistency_checker=ConsistencyCheckerView(
            passed=False,
            summary="inconsistent",
            reasons=(_sample_reason(),),
        ),
    )

    result = validator.validate(decision, now=NOW)

    assert result.passed is False
    assert any(issue.code == ALLOCATOR_CONSISTENCY_CHECK_FAILED for issue in result.issues)


def test_validate_payload_schema_invalid_returns_none_decision(tmp_path: Path) -> None:
    validator = _validator(tmp_path)

    decision, result = validator.validate_payload({"decision_id": "bad"}, now=NOW)

    assert decision is None
    assert result.passed is False
    assert result.issues[0].code == ALLOCATOR_SCHEMA_INVALID


def test_validate_payload_rejects_extra_trading_field(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())
    payload = _sample_decision().model_dump(mode="json")
    payload["side"] = "BUY"

    decision, result = validator.validate_payload(payload, now=NOW)

    assert decision is None
    assert result.issues[0].code == ALLOCATOR_SCHEMA_INVALID


def test_validate_payload_invalid_date_id_is_schema_invalid(tmp_path: Path) -> None:
    validator = _validator(tmp_path)
    payload = _sample_decision().model_dump(mode="json")
    payload["reasons"][0]["date_id"] = "bad-id"

    decision, result = validator.validate_payload(payload, now=NOW)

    assert decision is None
    assert result.issues[0].code == ALLOCATOR_SCHEMA_INVALID


def test_validate_payload_passes_for_valid_payload(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())

    decision, result = validator.validate_payload(_sample_decision().model_dump(mode="json"), now=NOW)

    assert decision is not None
    assert result.passed is True


def test_validator_rejects_naive_now(tmp_path: Path) -> None:
    validator = _validator(tmp_path, _sample_record())

    with pytest.raises(ValueError, match="timezone-aware datetime"):
        validator.validate(_sample_decision(), now=NAIVE_NOW)


def test_validator_issue_ordering_is_deterministic(tmp_path: Path) -> None:
    validator = _validator(tmp_path)
    bad_weights = _weights_with_gold("17")
    decision = _sample_decision(
        target_weights=bad_weights,
        asset_allocator=AssetAllocatorView(
            summary="alloc",
            target_weights=bad_weights,
            reasons=(_sample_reason(date_id="260522-99"),),
        ),
        cash_policy=CashPolicy(
            cash_target_percent=Percent("5"),
            rationale="bad",
            reasons=(_sample_reason(date_id="260522-99"),),
        ),
        cash_manager=CashManagerView(
            summary="cash",
            recommended_cash_percent=Percent("25"),
            reasons=(_sample_reason(date_id="260522-99"),),
        ),
        consistency_checker=ConsistencyCheckerView(
            passed=False,
            summary="failed",
            reasons=(_sample_reason(date_id="260522-99"),),
        ),
    )

    result = validator.validate(decision, now=NOW)

    assert result.passed is False
    codes = [issue.code for issue in result.issues]
    business_codes = [
        ALLOCATOR_GOLD_BAND_VIOLATION,
        ALLOCATOR_CASH_TARGET_BAND_VIOLATION,
        ALLOCATOR_CASH_TARGET_MISMATCH,
        ALLOCATOR_CONSISTENCY_CHECK_FAILED,
    ]
    assert codes[: len(business_codes)] == business_codes
    assert all(code == ALLOCATOR_DATE_ID_MISSING for code in codes[len(business_codes) :])


def test_extract_date_ids_preserves_order_and_duplicates() -> None:
    reason_a = _sample_reason(date_id="260522-1")
    reason_b = _sample_reason(date_id="260522-2")
    reason_c = _sample_reason(date_id="260522-1")
    reasons_ab = (reason_a, reason_b)
    reasons_c = (reason_c,)
    decision = _sample_decision(
        reasons=reasons_ab,
        signal_summary=SignalSummary(summary="signal", reasons=reasons_c),
        cash_manager=CashManagerView(
            summary="cash",
            recommended_cash_percent=Percent("20"),
            reasons=reasons_ab,
        ),
        asset_allocator=AssetAllocatorView(
            summary="alloc",
            target_weights=_sample_weights(),
            reasons=reasons_c,
        ),
        consistency_checker=ConsistencyCheckerView(
            passed=True,
            summary="ok",
            reasons=reasons_ab,
        ),
        cash_policy=CashPolicy(
            cash_target_percent=Percent("20"),
            rationale="rationale",
            reasons=reasons_c,
        ),
    )

    extracted = extract_date_ids_from_allocator_decision(decision)

    assert extracted == (
        DateId("260522-1"),
        DateId("260522-2"),
        DateId("260522-1"),
        DateId("260522-1"),
        DateId("260522-2"),
        DateId("260522-1"),
        DateId("260522-1"),
        DateId("260522-2"),
        DateId("260522-1"),
    )
