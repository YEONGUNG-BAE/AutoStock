from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import DateIdValidator, SQLiteDateIdSourceStore
from decision.canonical_json import payload_sha256
from domain import DateId, DateIdSourceRecord, FactType, StalenessPolicy, DecisionId, Percent
from allocator import (
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
)


NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)


def _sample_reason(**overrides: object) -> AllocatorReason:
    base = {
        "reason": "VIX 상승으로 방어적 현금 비중 유지",
        "date_id": DateId("260522-1"),
    }
    base.update(overrides)
    return AllocatorReason(**base)


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


def _sample_record(*, date_id: str = "260522-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.PRICE,
        source_name="yfinance",
        source_timestamp=NOW,
        created_at=NOW,
        summary="sample",
        payload={"symbol": "AAPL"},
    )


def _store_with_record(tmp_path: Path, record: DateIdSourceRecord) -> SQLiteDateIdSourceStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        store.save_record(record)
    return store


def test_replay_canonical_payload_is_key_order_independent() -> None:
    decision_a = _sample_decision(metadata={"b": "2", "a": "1"})
    decision_b = _sample_decision(metadata={"a": "1", "b": "2"})

    canonical_a = decision_a.to_canonical_dict()
    canonical_b = decision_b.to_canonical_dict()

    assert canonical_a == canonical_b
    assert payload_sha256(canonical_a) == payload_sha256(canonical_b)


def test_replay_validate_payload_result_is_deterministic(tmp_path: Path) -> None:
    store = _store_with_record(tmp_path, _sample_record())
    validator = AllocatorDecisionValidator(DateIdValidator(store, StalenessPolicy()))
    payload = _sample_decision().model_dump(mode="json")

    first_decision, first_result = validator.validate_payload(payload, now=NOW)
    second_decision, second_result = validator.validate_payload(payload, now=NOW)

    assert first_decision is not None
    assert second_decision is not None
    assert first_decision.to_canonical_dict() == second_decision.to_canonical_dict()
    assert first_result.to_canonical_dict() == second_result.to_canonical_dict()
    store.close()


def test_replay_reason_date_id_order_preserved_in_canonical_dict() -> None:
    reasons = (
        AllocatorReason(reason="first", date_id=DateId("260522-1")),
        AllocatorReason(reason="second", date_id=DateId("260522-2")),
    )
    weights = TargetWeights(kr=Percent("50"), us=Percent("30"), gold=Percent("20"))
    cash = Percent("20")
    decision = AllocatorDecision(
        decision_id=DecisionId("allocator-replay-order"),
        created_at=NOW,
        universe="ALL",
        summary_one_liner="order test",
        gold_policy_mode=GoldPolicyMode.NORMAL,
        signal_summary=SignalSummary(summary="signal", reasons=reasons),
        cash_manager=CashManagerView(summary="cash", recommended_cash_percent=cash, reasons=reasons),
        asset_allocator=AssetAllocatorView(summary="alloc", target_weights=weights, reasons=reasons),
        consistency_checker=ConsistencyCheckerView(passed=True, summary="ok", reasons=reasons),
        cash_policy=CashPolicy(cash_target_percent=cash, rationale="rationale", reasons=reasons),
        target_weights=weights,
        reasons=reasons,
    )

    canonical = decision.to_canonical_dict()
    assert [item["reason"] for item in canonical["reasons"]] == ["first", "second"]
