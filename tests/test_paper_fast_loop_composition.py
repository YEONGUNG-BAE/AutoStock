from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
from composition.paper_fast_loop import (
    AVAILABLE_REPLAY_FIXTURES,
    PaperFastLoopOutcome,
    PaperFastLoopPaths,
    build_paper_fast_loop_plan,
    inspect_paper_fast_loop,
    replay_offline,
)
from config.settings import RuntimePaperFastLoopSettings
from domain import DateId, DecisionId, Percent
from orchestration.execution_inputs_snapshot import compute_snapshot_payload_hash

_NOW = datetime(2026, 6, 16, 0, 30, tzinfo=UTC)
_UNIVERSE = "KR_LARGE"
_SYMBOL = "005930"


def _allocator() -> AllocatorDecision:
    reasons = (AllocatorReason(reason="근거", date_id=DateId("260616-1")),)
    weights = TargetWeights(kr=Percent("50"), us=Percent("30"), gold=Percent("20"))
    cash = Percent("20")
    return AllocatorDecision(
        decision_id=DecisionId("allocator-260616-001"),
        created_at=_NOW,
        universe=_UNIVERSE,
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


def _snapshot_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
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


def _settings(tmp_path: Path, **overrides: Any) -> RuntimePaperFastLoopSettings:
    # 경로는 base_dir 하위 상대경로로 검증되므로 runtime/ 접두사를 유지한다.
    defaults: dict[str, Any] = {"enabled": True, "market": "KR", "symbol": _SYMBOL}
    defaults.update(overrides)
    return RuntimePaperFastLoopSettings(**defaults)


def _write_snapshot(base_dir: Path, settings: RuntimePaperFastLoopSettings, payload: dict[str, Any]) -> None:
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=base_dir)
    paths.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    paths.snapshot_path.write_text(json.dumps(payload), encoding="utf-8")


# --- validate-only ---


def test_plan_ready_with_valid_snapshot_and_no_ledger(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_snapshot(tmp_path, settings, _snapshot_payload())
    plan = build_paper_fast_loop_plan(settings=settings, now=_NOW, base_dir=tmp_path)
    assert plan.outcome is PaperFastLoopOutcome.READY
    assert plan.reasons == ()
    assert plan.snapshot_universe == _UNIVERSE
    assert plan.symbol == _SYMBOL


def test_plan_not_ready_when_snapshot_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    plan = build_paper_fast_loop_plan(settings=settings, now=_NOW, base_dir=tmp_path)
    assert plan.outcome is PaperFastLoopOutcome.NOT_READY
    assert "snapshot_file_missing" in plan.reasons


def test_plan_not_ready_when_snapshot_expired(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_snapshot(tmp_path, settings, _snapshot_payload())
    plan = build_paper_fast_loop_plan(
        settings=settings, now=_NOW + timedelta(days=2), base_dir=tmp_path
    )
    assert plan.outcome is PaperFastLoopOutcome.NOT_READY
    assert "snapshot_expired" in plan.reasons


def test_plan_rejects_foreign_position(tmp_path: Path) -> None:
    import sqlite3

    settings = _settings(tmp_path)
    _write_snapshot(tmp_path, settings, _snapshot_payload())
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    paths.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.ledger_path)
    conn.execute(
        "CREATE TABLE current_positions (symbol TEXT, market TEXT, account_role TEXT, currency TEXT, quantity TEXT)"
    )
    conn.execute("INSERT INTO current_positions VALUES ('000660', 'KR', 'PAPER', 'KRW', '10')")
    conn.execute("INSERT INTO current_positions VALUES ('AAPL', 'US', 'PAPER', 'USD', '5')")
    conn.commit()
    conn.close()
    plan = build_paper_fast_loop_plan(settings=settings, now=_NOW, base_dir=tmp_path)
    assert plan.outcome is PaperFastLoopOutcome.NOT_READY
    assert "foreign_position_present" in plan.reasons
    assert "unsupported_market" in plan.reasons
    assert "unsupported_currency" in plan.reasons


# --- inspection ---


def test_inspect_reports_missing_databases(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    inspection = inspect_paper_fast_loop(settings=settings, base_dir=tmp_path)
    assert set(inspection.missing_databases) == {"ledger", "trigger_journal", "active_decision_store"}
    assert inspection.ledger is None


# --- replay ---


def test_replay_buy_fill_commits_one(tmp_path: Path) -> None:
    settings = _settings(tmp_path, symbol=_SYMBOL)
    result = replay_offline(settings=settings, temp_dir=tmp_path, fixture="buy_fill")
    assert result.committed_count == 1
    assert result.statuses == ("committed",)
    assert result.final_position_quantity == "57"
    assert result.journal_terminal_count == 1


def test_replay_hold_noop_does_not_fill(tmp_path: Path) -> None:
    settings = _settings(tmp_path, symbol=_SYMBOL)
    result = replay_offline(settings=settings, temp_dir=tmp_path, fixture="hold_noop")
    assert result.committed_count == 0
    assert result.final_position_quantity is None


def test_replay_unknown_fixture_raises(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(ValueError, match="unknown replay fixture"):
        replay_offline(settings=settings, temp_dir=tmp_path, fixture="nope")


def test_replay_uses_only_temp_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    replay_offline(settings=settings, temp_dir=tmp_path, fixture="buy_fill")
    # runtime/ 경로는 절대 생성되지 않는다.
    assert not (Path(tmp_path) / "runtime").exists()


def test_available_fixtures_constant() -> None:
    assert AVAILABLE_REPLAY_FIXTURES == ("buy_fill", "hold_noop")
