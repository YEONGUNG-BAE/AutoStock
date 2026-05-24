from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data import DateIdValidator, SQLiteDateIdSourceStore
from decision.canonical_json import payload_sha256
from domain import DateId, DateIdSourceRecord, DecisionId, FactType, Percent, StalenessPolicy
from analysis import (
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


def _sample_reason(**overrides: object) -> AnalysisReason:
    base = {
        "reason": "실적 둔화 우려",
        "date_id": DateId("260522-1"),
    }
    base.update(overrides)
    return AnalysisReason(**base)


def _sample_decision(**overrides: object) -> AnalysisDecision:
    base = {
        "decision_id": DecisionId("analysis-260522-001"),
        "created_at": NOW,
        "universe": "KR_LARGE",
        "symbol": "005930",
        "market": "KR",
        "summary_one_liner": "방어적 HOLD",
        "bear": BearPerspective(
            summary="하방 리스크",
            risks=("수요 둔화",),
            reasons=(_sample_reason(),),
        ),
        "bull": BullPerspective(
            summary="성장 모멘텀",
            catalysts=("신제품",),
            reasons=(_sample_reason(date_id="260522-2"),),
        ),
        "risk_manager": RiskManagerEvaluation(
            summary="중립 평가",
            reasons=(_sample_reason(date_id="260522-3"),),
        ),
        "fund_manager": FundManagerDecision(
            action=AnalysisAction.HOLD,
            target_weight_percent=Percent("5"),
            rationale="유지",
            reasons=(_sample_reason(date_id="260522-4"),),
        ),
        "reasons": (_sample_reason(date_id="260522-5"),),
    }
    base.update(overrides)
    return AnalysisDecision(**base)


def _sample_record(*, date_id: str = "260522-1") -> DateIdSourceRecord:
    return DateIdSourceRecord(
        date_id=DateId(date_id),
        fact_type=FactType.PRICE,
        source_name="yfinance",
        source_timestamp=NOW,
        created_at=NOW,
        summary="sample",
        payload={"symbol": "005930"},
    )


def _store_with_records(tmp_path: Path, *records: DateIdSourceRecord) -> SQLiteDateIdSourceStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    with store.transaction():
        for record in records:
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
    decision = _sample_decision()
    unique_ids = {date_id.value for date_id in extract_date_ids_from_analysis_decision(decision)}
    store = _store_with_records(
        tmp_path,
        *(_sample_record(date_id=date_id) for date_id in sorted(unique_ids)),
    )
    validator = AnalysisDecisionValidator(DateIdValidator(store, StalenessPolicy()))
    payload = decision.model_dump(mode="json")

    first_decision, first_result = validator.validate_payload(payload, now=NOW)
    second_decision, second_result = validator.validate_payload(payload, now=NOW)

    assert first_decision is not None
    assert second_decision is not None
    assert first_decision.to_canonical_dict() == second_decision.to_canonical_dict()
    assert first_result.to_canonical_dict() == second_result.to_canonical_dict()
    store.close()


def test_replay_reason_date_id_order_preserved_in_canonical_dict() -> None:
    reasons = (
        AnalysisReason(reason="first", date_id=DateId("260522-1")),
        AnalysisReason(reason="second", date_id=DateId("260522-2")),
    )
    decision = AnalysisDecision(
        decision_id=DecisionId("analysis-replay-order"),
        created_at=NOW,
        universe="KR_LARGE",
        symbol="005930",
        market="KR",
        summary_one_liner="order test",
        bear=BearPerspective(summary="bear", risks=("risk",), reasons=reasons),
        bull=BullPerspective(summary="bull", catalysts=("cat",), reasons=reasons),
        risk_manager=RiskManagerEvaluation(summary="risk", reasons=reasons),
        fund_manager=FundManagerDecision(
            action=AnalysisAction.HOLD,
            target_weight_percent=Percent("5"),
            rationale="hold",
            reasons=reasons,
        ),
        reasons=reasons,
    )

    canonical = decision.to_canonical_dict()
    assert [item["reason"] for item in canonical["reasons"]] == ["first", "second"]


def test_replay_issue_ordering_is_deterministic(tmp_path: Path) -> None:
    decision = _sample_decision(
        fund_manager=FundManagerDecision(
            action=AnalysisAction.HOLD,
            target_weight_percent=Percent("20"),
            rationale="out of band",
            reasons=(_sample_reason(date_id="260522-99"),),
        ),
    )
    store = SQLiteDateIdSourceStore(tmp_path / "date_ids.db")
    validator = AnalysisDecisionValidator(DateIdValidator(store, StalenessPolicy()))

    first = validator.validate(
        decision,
        now=NOW,
        allocator_target_weight=Percent("5"),
        tolerance_percent=Percent("2"),
    )
    second = validator.validate(
        decision,
        now=NOW,
        allocator_target_weight=Percent("5"),
        tolerance_percent=Percent("2"),
    )

    assert first.to_canonical_dict() == second.to_canonical_dict()
    store.close()
