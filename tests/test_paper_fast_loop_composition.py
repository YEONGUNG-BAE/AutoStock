from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
    InspectionOutcome,
    PaperFastLoopOutcome,
    PaperFastLoopPaths,
    build_paper_fast_loop_plan,
    build_replay_snapshot_payload,
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


def test_plan_does_not_open_databases(tmp_path: Path) -> None:
    # B1: validate-only는 DB를 열지 않는다 — ledger 파일이 있어도 무시되고, 없으면 생성하지 않는다.
    settings = _settings(tmp_path)
    _write_snapshot(tmp_path, settings, _snapshot_payload())
    plan = build_paper_fast_loop_plan(settings=settings, now=_NOW, base_dir=tmp_path)
    assert plan.outcome is PaperFastLoopOutcome.READY
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    assert not paths.ledger_path.exists()
    assert not paths.trigger_journal_path.exists()
    assert not paths.active_decision_store_path.exists()


# --- inspection ---

_FULL_LEDGER_SCHEMA = """
CREATE TABLE order_intents (order_id TEXT PRIMARY KEY, symbol TEXT, market TEXT);
CREATE TABLE order_results (order_id TEXT PRIMARY KEY, status TEXT);
CREATE TABLE fills (fill_id TEXT PRIMARY KEY, order_id TEXT);
CREATE TABLE current_cash (currency TEXT, account_role TEXT, amount TEXT, PRIMARY KEY (currency, account_role));
CREATE TABLE current_positions (
    symbol TEXT, market TEXT, account_role TEXT, currency TEXT, quantity TEXT,
    PRIMARY KEY (symbol, market, account_role)
);
"""


def test_inspect_reports_missing_databases(tmp_path: Path) -> None:
    # B2: 모든 DB가 없으면 fail-closed — outcome NO_GO + missing_database reason, exit/exception 없음.
    settings = _settings(tmp_path)
    inspection = inspect_paper_fast_loop(settings=settings, base_dir=tmp_path)
    assert inspection.outcome is InspectionOutcome.NO_GO
    assert set(inspection.missing_databases) == {"ledger", "trigger_journal", "active_decision_store"}
    assert "missing_database:ledger" in inspection.reasons
    assert "missing_database:trigger_journal" in inspection.reasons
    assert "missing_database:active_decision_store" in inspection.reasons
    assert inspection.ledger is None


def test_inspect_flags_foreign_position(tmp_path: Path) -> None:
    # B1/B2: position/account-role/currency preflight은 inspect-existing에서 수행된다.
    import sqlite3

    settings = _settings(tmp_path)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    paths.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.ledger_path)
    conn.executescript(_FULL_LEDGER_SCHEMA)
    conn.execute("INSERT INTO current_positions VALUES ('000660', 'KR', 'PAPER', 'KRW', '10')")
    conn.execute("INSERT INTO current_positions VALUES ('AAPL', 'US', 'PAPER', 'USD', '5')")
    conn.commit()
    conn.close()
    inspection = inspect_paper_fast_loop(settings=settings, base_dir=tmp_path)
    assert inspection.outcome is InspectionOutcome.NO_GO
    assert "foreign_position_present" in inspection.reasons
    assert "unsupported_market" in inspection.reasons
    assert "unsupported_currency" in inspection.reasons


def test_inspect_flags_invalid_ledger_schema(tmp_path: Path) -> None:
    # B2: required table/column 누락 → sanitized schema reason, traceback 없음.
    import sqlite3

    settings = _settings(tmp_path)
    paths = PaperFastLoopPaths.from_settings(settings, base_dir=tmp_path)
    paths.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.ledger_path)
    conn.execute("CREATE TABLE current_positions (symbol TEXT, market TEXT)")
    conn.commit()
    conn.close()
    inspection = inspect_paper_fast_loop(settings=settings, base_dir=tmp_path)
    assert inspection.outcome is InspectionOutcome.NO_GO
    assert any(r.startswith("ledger_missing_table:") for r in inspection.reasons)


# --- replay ---


def test_replay_buy_fill_proves_duplicate_and_restart_safety(tmp_path: Path) -> None:
    # B4/B5: validated snapshot이 실제 실행에 공급되고, 첫 fill 이후 반복/재시작 모두 중복 fill 0.
    settings = _settings(tmp_path, symbol=_SYMBOL)
    result = replay_offline(settings=settings, temp_dir=tmp_path, fixture="buy_fill")
    assert result.snapshot_loaded is True
    assert result.snapshot_reason is None
    # 첫 이벤트만 체결. 반복(동일 스택)은 max-fires로 suppressed, 재시작(같은 DB·새 스택)은
    # 동일 idempotency key가 terminal(committed)로 남아 journal dedup으로 skip된다 — 둘 다 중복 체결 0.
    assert result.first_status == "committed"
    assert result.repeat_status == "suppressed"
    assert result.restart_status == "skipped_terminal"
    assert result.committed_count == 1
    assert result.order_result_count == 1
    assert result.filled_result_count == 1
    assert result.fill_count == 1
    assert result.final_position_quantity == "57"
    # 체결 1회만 terminal, 추가 order/abort 없음, nonterminal 없음.
    assert result.journal_terminal_count == 1
    state_counts = dict(result.journal_state_counts)
    assert state_counts.get("committed", 0) == 1
    assert state_counts.get("reserved", 0) == 0
    assert state_counts.get("dispatching", 0) == 0
    # 현금은 정확히 1회만 차감(57 * 70000 = 3,990,000 → 96,010,000).
    assert result.final_cash_amount == "96010000"


def test_replay_hold_noop_does_not_fill(tmp_path: Path) -> None:
    settings = _settings(tmp_path, symbol=_SYMBOL)
    result = replay_offline(settings=settings, temp_dir=tmp_path, fixture="hold_noop")
    assert result.snapshot_loaded is True
    assert result.committed_count == 0
    assert result.fill_count == 0
    assert result.final_position_quantity is None


def test_replay_tampered_snapshot_yields_zero_execution(tmp_path: Path) -> None:
    # B4: hash 변조 snapshot → load fail-closed → 실행 0.
    settings = _settings(tmp_path, symbol=_SYMBOL)
    payload = build_replay_snapshot_payload()
    payload["payload_sha256"] = "0" * 64  # 변조.
    snapshot_file = tmp_path / "tampered.json"
    snapshot_file.write_text(json.dumps(payload), encoding="utf-8")
    result = replay_offline(
        settings=settings, temp_dir=tmp_path, fixture="buy_fill", snapshot_path=snapshot_file
    )
    assert result.snapshot_loaded is False
    assert result.snapshot_reason == "snapshot_hash_mismatch"
    assert result.committed_count == 0
    assert result.fill_count == 0
    assert result.final_position_quantity is None


def test_replay_stale_snapshot_yields_zero_execution(tmp_path: Path) -> None:
    # B4: 이벤트 시점에 만료된 snapshot → resolve가 snapshot_expired → 실행 0.
    settings = _settings(tmp_path, symbol=_SYMBOL)
    expired = datetime(2026, 6, 16, 9, 15, tzinfo=ZoneInfo("Asia/Seoul"))  # 이벤트(09:30) 이전.
    payload = build_replay_snapshot_payload(expires_at=expired)
    snapshot_file = tmp_path / "stale.json"
    snapshot_file.write_text(json.dumps(payload), encoding="utf-8")
    result = replay_offline(
        settings=settings, temp_dir=tmp_path, fixture="buy_fill", snapshot_path=snapshot_file
    )
    # snapshot 자체는 load되지만 resolve에서 만료로 실행 입력을 못 받아 체결 0.
    assert result.snapshot_loaded is True
    assert result.committed_count == 0
    assert result.fill_count == 0
    assert result.first_status == "execution_inputs_unavailable"


def test_replay_universe_mismatch_snapshot_yields_zero_execution(tmp_path: Path) -> None:
    # B4: snapshot universe ≠ active decision universe → resolve mismatch → 실행 0.
    settings = _settings(tmp_path, symbol=_SYMBOL)
    payload = build_replay_snapshot_payload(symbol_universe="KR_OTHER")
    snapshot_file = tmp_path / "mismatch.json"
    snapshot_file.write_text(json.dumps(payload), encoding="utf-8")
    result = replay_offline(
        settings=settings, temp_dir=tmp_path, fixture="buy_fill", snapshot_path=snapshot_file
    )
    assert result.snapshot_loaded is True
    assert result.committed_count == 0
    assert result.fill_count == 0
    assert result.first_status == "execution_inputs_unavailable"


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
