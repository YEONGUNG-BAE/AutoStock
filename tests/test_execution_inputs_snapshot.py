"""RTM-7c.4a — validated execution-inputs snapshot loader/provider tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from allocator import (
    AllocatorDecision,
    AllocatorReason,
    AssetAllocatorView,
    CashManagerView,
    CashPolicy,
    ConsistencyCheckerView,
    GoldPolicyMode,
    SignalSummary,
    TargetWeights,
)
from analysis import (
    AnalysisAction,
    AnalysisDecision,
    AnalysisReason,
    BearPerspective,
    BullPerspective,
    FundManagerDecision,
    RiskManagerEvaluation,
)
from domain import DateId, DecisionId, Percent
from market_data.decision_loader import load_decision_bundle
from domain.decision import DecisionSnapshot
from analysis.models import ANALYSIS_DECISION_SCHEMA
from domain.validation import ValidationResult
from orchestration.active_decision_store import ActiveDecisionStore, DecisionPublicationCandidate
from orchestration.execution_inputs_snapshot import (
    EXECUTION_INPUTS_SNAPSHOT_SCHEMA_VERSION,
    ExecutionInputsSnapshotError,
    ValidatedExecutionInputsProvider,
    compute_snapshot_payload_hash,
    load_execution_inputs_snapshot,
)

_NOW = datetime(2026, 6, 15, 0, 30, tzinfo=UTC)
_UNIVERSE = "KR_LARGE"


def _allocator(universe: str = _UNIVERSE, created_at: datetime = _NOW) -> AllocatorDecision:
    reasons = (AllocatorReason(reason="근거", date_id=DateId("260615-1")),)
    weights = TargetWeights(kr=Percent("50"), us=Percent("30"), gold=Percent("20"))
    cash = Percent("20")
    return AllocatorDecision(
        decision_id=DecisionId("allocator-260615-001"),
        created_at=created_at,
        universe=universe,
        summary_one_liner="배분 유지",
        gold_policy_mode=GoldPolicyMode.NORMAL,
        signal_summary=SignalSummary(summary="신호", reasons=reasons),
        cash_manager=CashManagerView(summary="현금", recommended_cash_percent=cash, reasons=reasons),
        asset_allocator=AssetAllocatorView(summary="배분", target_weights=weights, reasons=reasons),
        consistency_checker=ConsistencyCheckerView(passed=True, summary="확인", reasons=reasons),
        cash_policy=CashPolicy(cash_target_percent=cash, rationale="유동성", reasons=reasons),
        target_weights=weights,
        reasons=reasons,
    )


def _base_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": EXECUTION_INPUTS_SNAPSHOT_SCHEMA_VERSION,
        "source_id": "operator-fixture-1",
        "created_at": _NOW.isoformat(),
        "expires_at": (_NOW + timedelta(days=1)).isoformat(),
        "universe": _UNIVERSE,
        "allocator_decision": _allocator().model_dump(mode="json"),
        "portfolio_policy": {
            "mode": "rebalancing",
            "allocator_tolerance_percent": "5",
            "allocator_symbol_target_weight": "4",
            "paper_observation_min_invested_percent": None,
            "mdd_percent": None,
            "gold_trades_this_month": 0,
            "gold_trades_this_quarter": 0,
            "asset_bucket": "kr",
            "metadata": {},
        },
    }
    payload.update(overrides)
    payload["payload_sha256"] = compute_snapshot_payload_hash(payload)
    return payload


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "execution_inputs.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_valid_snapshot_loads(tmp_path: Path) -> None:
    snap = load_execution_inputs_snapshot(_write(tmp_path, _base_payload()))
    assert snap.schema_version == EXECUTION_INPUTS_SNAPSHOT_SCHEMA_VERSION
    assert snap.universe == _UNIVERSE
    assert snap.allocator_decision.universe == _UNIVERSE
    assert snap.portfolio_policy.allocator_symbol_target_weight == Percent("4")


def test_unknown_top_level_field_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["extra"] = 1
    payload["payload_sha256"] = compute_snapshot_payload_hash(payload)
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(_write(tmp_path, payload))
    assert exc.value.reason_code == "snapshot_unknown_field"


def test_unsupported_schema_version_rejected(tmp_path: Path) -> None:
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(_write(tmp_path, _base_payload(schema_version=2)))
    assert exc.value.reason_code == "snapshot_unsupported_version"


def test_naive_datetime_rejected(tmp_path: Path) -> None:
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(
            _write(tmp_path, _base_payload(created_at="2026-06-15T00:30:00"))
        )
    assert exc.value.reason_code == "snapshot_naive_datetime"


def test_created_after_expires_rejected(tmp_path: Path) -> None:
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(
            _write(
                tmp_path,
                _base_payload(expires_at=(_NOW - timedelta(hours=1)).isoformat()),
            )
        )
    assert exc.value.reason_code == "snapshot_invalid_validity"


def test_allocator_created_after_snapshot_rejected(tmp_path: Path) -> None:
    late = _allocator(created_at=_NOW + timedelta(hours=1)).model_dump(mode="json")
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(_write(tmp_path, _base_payload(allocator_decision=late)))
    assert exc.value.reason_code == "snapshot_allocator_created_after"


def test_universe_mismatch_rejected(tmp_path: Path) -> None:
    other = _allocator(universe="US_LARGE").model_dump(mode="json")
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(_write(tmp_path, _base_payload(allocator_decision=other)))
    assert exc.value.reason_code == "snapshot_universe_mismatch"


def test_hash_mismatch_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["source_id"] = "tampered-after-hash"
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(_write(tmp_path, payload))
    assert exc.value.reason_code == "snapshot_hash_mismatch"


def test_nested_allocator_tamper_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["allocator_decision"]["summary_one_liner"] = "변조됨"
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(_write(tmp_path, payload))
    assert exc.value.reason_code == "snapshot_hash_mismatch"


def test_bool_int_coercion_in_policy_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["portfolio_policy"]["gold_trades_this_month"] = True
    payload["payload_sha256"] = compute_snapshot_payload_hash(payload)
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(_write(tmp_path, payload))
    assert exc.value.reason_code == "snapshot_invalid_policy"


def test_percent_numeric_coercion_rejected(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["portfolio_policy"]["allocator_symbol_target_weight"] = 4
    payload["payload_sha256"] = compute_snapshot_payload_hash(payload)
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(_write(tmp_path, payload))
    assert exc.value.reason_code == "snapshot_invalid_policy"


def test_not_utf8_rejected(tmp_path: Path) -> None:
    path = tmp_path / "snap.json"
    path.write_bytes(b"\xff\xfe not utf8")
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(path)
    assert exc.value.reason_code == "snapshot_not_utf8"


def test_missing_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(tmp_path / "absent.json")
    assert exc.value.reason_code == "snapshot_file_missing"


def test_loader_error_does_not_leak_raw_payload(tmp_path: Path) -> None:
    payload = _base_payload()
    payload["source_id"] = "SECRET-TOKEN-SHOULD-NOT-APPEAR"
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        load_execution_inputs_snapshot(_write(tmp_path, payload))
    assert "SECRET-TOKEN" not in str(exc.value)


def test_snapshot_is_frozen(tmp_path: Path) -> None:
    snap = load_execution_inputs_snapshot(_write(tmp_path, _base_payload()))
    with pytest.raises(Exception):
        snap.source_id = "mutated"  # type: ignore[misc]


# --- provider binding -------------------------------------------------------


def _publish_active(tmp_path: Path, *, universe: str = _UNIVERSE):
    decision = AnalysisDecision(
        decision_id=DecisionId("analysis-260615-001"),
        created_at=_NOW,
        universe=universe,
        symbol="005930",
        market="KR",
        summary_one_liner="HOLD",
        bear=BearPerspective(summary="하방", risks=("리스크",), reasons=(AnalysisReason(reason="r", date_id=DateId("260615-1")),)),
        bull=BullPerspective(summary="상방", catalysts=("촉매",), reasons=(AnalysisReason(reason="r", date_id=DateId("260615-2")),)),
        risk_manager=RiskManagerEvaluation(summary="중립", reasons=(AnalysisReason(reason="r", date_id=DateId("260615-3")),)),
        fund_manager=FundManagerDecision(action=AnalysisAction.HOLD, target_weight_percent=Percent("4"), rationale="유지", reasons=(AnalysisReason(reason="r", date_id=DateId("260615-4")),)),
        reasons=(AnalysisReason(reason="r", date_id=DateId("260615-5")),),
    )
    snapshot = DecisionSnapshot.create(
        decision_id=decision.decision_id,
        created_at=decision.created_at,
        schema_name=ANALYSIS_DECISION_SCHEMA,
        raw_payload=decision.model_dump(mode="json"),
        validation_result=ValidationResult(passed=True, issues=(), schema_name=ANALYSIS_DECISION_SCHEMA),
    )
    store = ActiveDecisionStore(tmp_path / "active.sqlite3")
    store.publish(
        DecisionPublicationCandidate(snapshot=snapshot, plan=None, valid_from=_NOW, expires_at=_NOW + timedelta(days=1)),
        now=_NOW,
    )
    active = store.read_active("KR", "005930")
    store.close()
    assert active is not None
    return active


def test_provider_resolves_within_window(tmp_path: Path) -> None:
    snap = load_execution_inputs_snapshot(_write(tmp_path, _base_payload()))
    provider = ValidatedExecutionInputsProvider(snapshot=snap)
    active = _publish_active(tmp_path)
    inputs = provider.resolve(active=active, now=_NOW + timedelta(hours=1))
    assert inputs.allocator_decision.universe == _UNIVERSE
    assert inputs.portfolio_policy.allocator_symbol_target_weight == Percent("4")


def test_provider_rejects_expired_snapshot(tmp_path: Path) -> None:
    snap = load_execution_inputs_snapshot(_write(tmp_path, _base_payload()))
    provider = ValidatedExecutionInputsProvider(snapshot=snap)
    active = _publish_active(tmp_path)
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        provider.resolve(active=active, now=_NOW + timedelta(days=2))
    assert exc.value.reason_code == "snapshot_expired"


def test_provider_rejects_active_universe_mismatch(tmp_path: Path) -> None:
    snap = load_execution_inputs_snapshot(_write(tmp_path, _base_payload()))
    provider = ValidatedExecutionInputsProvider(snapshot=snap)
    active = _publish_active(tmp_path, universe="KR_SMALL")
    with pytest.raises(ExecutionInputsSnapshotError) as exc:
        provider.resolve(active=active, now=_NOW + timedelta(hours=1))
    assert exc.value.reason_code == "snapshot_active_universe_mismatch"
