from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config.settings import ExecutionMode
from domain import Currency, DecisionId, Market, Money
from domain.enums import AccountRole, AssetClass, OrderSide, OrderType
from domain.order import OrderIntent
from domain.validation import ValidationIssue, ValidationResult, ValidationSeverity
from logs import (
    DailyRunStatus,
    DailySummaryStore,
    DebugEvent,
    DebugEventSource,
    LogSeverity,
    build_daily_summary,
    debug_event_from_exception,
    debug_event_from_nav_snapshot_error,
    debug_event_from_paper_loop_result,
    debug_event_from_validation_result,
)
from paper_loop.models import (
    PAPER_LOOP_SCHEMA,
    PAPER_LOOP_VALIDATOR_VERSION,
    PaperLoopResult,
    PaperLoopStatus,
    passed_validation_result,
)
from risk.models import OrderGenerationStatus
from risk.order_generation import OrderGenerationResult

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
TRADING_DATE = date(2026, 5, 22)


def _passed_result(*, schema_name: str = "test.schema", version: str = "v1") -> ValidationResult:
    return passed_validation_result(schema_name=schema_name, validator_version=version)


def _failed_result(*, code: str = "TEST_FAILED", message: str = "failed") -> ValidationResult:
    return ValidationResult(
        passed=False,
        issues=(
            ValidationIssue(
                code=code,
                message=message,
                severity=ValidationSeverity.ERROR,
            ),
        ),
        schema_name=PAPER_LOOP_SCHEMA,
        validator_version=PAPER_LOOP_VALIDATOR_VERSION,
    )


def _order_generation_result(
    *,
    status: OrderGenerationStatus = OrderGenerationStatus.NOOP,
    validation_result: ValidationResult | None = None,
    order_intent: OrderIntent | None = None,
) -> OrderGenerationResult:
    return OrderGenerationResult(
        status=status,
        order_intent=order_intent,
        validation_result=validation_result or _passed_result(schema_name="risk.v1", version="phase10"),
    )


def _order_intent(*, symbol: str = "005930", order_id: str = "order-001") -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        correlation_id="corr-001",
        symbol=symbol,
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        target_weight_percent=Decimal("5"),
        created_at=NOW,
    )


def _paper_loop_result(
    *,
    status: PaperLoopStatus,
    validation_result: ValidationResult | None = None,
    risk_result: ValidationResult | None = None,
    order_intent: OrderIntent | None = None,
    decision_snapshot_ids: tuple[DecisionId, ...] = (),
) -> PaperLoopResult:
    return PaperLoopResult(
        status=status,
        validation_result=validation_result or _passed_result(
            schema_name=PAPER_LOOP_SCHEMA,
            version=PAPER_LOOP_VALIDATOR_VERSION,
        ),
        risk_result=risk_result or _passed_result(schema_name="risk.v1", version="phase10"),
        order_generation_result=_order_generation_result(
            order_intent=order_intent,
            validation_result=risk_result or _passed_result(schema_name="risk.v1", version="phase10"),
        ),
        generated_order_intent=order_intent,
        executable_order_intent=order_intent,
        decision_snapshot_ids=decision_snapshot_ids,
    )


def test_failed_validation_result_maps_to_debug_event() -> None:
    validation = _failed_result(code="ALLOCATOR_SCHEMA_INVALID")
    event = debug_event_from_validation_result(
        validation_result=validation,
        timestamp_kst=NOW,
        source=DebugEventSource.ALLOCATOR,
        detail="allocator validation failed",
    )
    assert event is not None
    assert event.event_code == "LLM_SCHEMA_ERROR"
    assert event.validation_issue_codes == ("ALLOCATOR_SCHEMA_INVALID",)


def test_passed_validation_result_returns_none() -> None:
    event = debug_event_from_validation_result(
        validation_result=_passed_result(),
        timestamp_kst=NOW,
        source=DebugEventSource.ANALYSIS,
        detail="unused",
    )
    assert event is None


def test_paper_loop_risk_blocked_maps_to_debug_event() -> None:
    risk_result = _failed_result(code="RISK_SOFT_BAND", message="blocked by risk")
    result = _paper_loop_result(
        status=PaperLoopStatus.RISK_BLOCKED,
        risk_result=risk_result,
        validation_result=risk_result,
        order_intent=_order_intent(),
        decision_snapshot_ids=(DecisionId("paper-loop-001"),),
    )
    event = debug_event_from_paper_loop_result(
        result=result,
        timestamp_kst=NOW,
        run_id=DecisionId("paper-loop-001"),
    )
    assert event is not None
    assert event.event_code == "RISK_FILTER_ORDER_REJECTED"
    assert event.symbol == "005930"


def test_paper_loop_broker_rejected_maps_to_debug_event() -> None:
    result = _paper_loop_result(status=PaperLoopStatus.BROKER_REJECTED)
    event = debug_event_from_paper_loop_result(result=result, timestamp_kst=NOW)
    assert event is not None
    assert event.event_code == "PAPER_BROKER_SIM_ERROR"


def test_paper_loop_filled_and_noop_return_none() -> None:
    filled = _paper_loop_result(status=PaperLoopStatus.FILLED)
    noop = _paper_loop_result(status=PaperLoopStatus.NOOP)
    assert debug_event_from_paper_loop_result(result=filled, timestamp_kst=NOW) is None
    assert debug_event_from_paper_loop_result(result=noop, timestamp_kst=NOW) is None


def test_exception_helper_includes_exception_type() -> None:
    event = debug_event_from_exception(
        exc=ValueError("bad config"),
        timestamp_kst=NOW,
        source=DebugEventSource.CONFIG,
        event_code="CONFIG_LOAD_ERROR",
        severity=LogSeverity.CRITICAL,
        detail="config load failed",
    )
    assert event.exception_type == "ValueError"
    assert "bad config" in event.metadata["exception_message"]


def test_nav_snapshot_error_helper() -> None:
    event = debug_event_from_nav_snapshot_error(
        timestamp_kst=NOW,
        detail="nav snapshot write failed",
        run_id="paper-loop-001",
    )
    assert event.event_code == "PAPER_NAV_SNAPSHOT_ERROR"


def test_deterministic_issue_code_ordering() -> None:
    validation = ValidationResult(
        passed=False,
        issues=(
            ValidationIssue(
                code="B_CODE",
                message="b",
                severity=ValidationSeverity.ERROR,
            ),
            ValidationIssue(
                code="A_CODE",
                message="a",
                severity=ValidationSeverity.ERROR,
            ),
        ),
        schema_name=PAPER_LOOP_SCHEMA,
        validator_version=PAPER_LOOP_VALIDATOR_VERSION,
    )
    event = debug_event_from_validation_result(
        validation_result=validation,
        timestamp_kst=NOW,
        source=DebugEventSource.RUNTIME,
        detail="failed",
    )
    assert event is not None
    assert event.validation_issue_codes == ("A_CODE", "B_CODE")


def test_no_invented_event_code() -> None:
    validation = _failed_result(code="ALLOCATOR_TARGET_WEIGHTS_SUM_INVALID")
    event = debug_event_from_validation_result(
        validation_result=validation,
        timestamp_kst=NOW,
        source=DebugEventSource.ALLOCATOR,
        detail="sum invalid",
    )
    assert event is not None
    assert event.event_code == "ALLOCATOR_TARGET_SUM_INVALID"


def test_all_filled_completed() -> None:
    summary = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(
            _paper_loop_result(
                status=PaperLoopStatus.FILLED,
                order_intent=_order_intent(symbol="005930"),
                decision_snapshot_ids=(DecisionId("paper-loop-001"),),
            ),
            _paper_loop_result(
                status=PaperLoopStatus.FILLED,
                order_intent=_order_intent(symbol="000660", order_id="order-002"),
                decision_snapshot_ids=(DecisionId("paper-loop-002"),),
            ),
        ),
    )
    assert summary.status == DailyRunStatus.COMPLETED
    assert summary.filled_orders == 2
    assert summary.symbols_touched == ("000660", "005930")


def test_all_noop_status() -> None:
    summary = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(
            _paper_loop_result(status=PaperLoopStatus.NOOP),
            _paper_loop_result(status=PaperLoopStatus.NOOP),
        ),
    )
    assert summary.status == DailyRunStatus.NOOP
    assert summary.noop_count == 2


def test_mix_filled_and_blocked_partial() -> None:
    summary = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(
            _paper_loop_result(status=PaperLoopStatus.FILLED, order_intent=_order_intent()),
            _paper_loop_result(
                status=PaperLoopStatus.RISK_BLOCKED,
                risk_result=_failed_result(code="RISK_BLOCKED"),
                validation_result=_failed_result(code="RISK_BLOCKED"),
            ),
        ),
    )
    assert summary.status == DailyRunStatus.PARTIAL
    assert summary.filled_orders == 1
    assert summary.blocked_orders == 1


def test_all_failed_status() -> None:
    summary = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(
            _paper_loop_result(
                status=PaperLoopStatus.VALIDATION_FAILED,
                validation_result=_failed_result(code="PAPER_LOOP_INPUT_VALIDATION_FAILED"),
            ),
            _paper_loop_result(
                status=PaperLoopStatus.BROKER_REJECTED,
                validation_result=_failed_result(code="PAPER_BROKER_REJECTED"),
            ),
        ),
    )
    assert summary.status == DailyRunStatus.FAILED
    assert summary.validation_failed_count == 1
    assert summary.rejected_orders == 1


def test_debug_event_ids_preserved() -> None:
    event = DebugEvent.create(
        timestamp_kst=NOW,
        source=DebugEventSource.PAPER_BROKER,
        event_code="PAPER_BROKER_SIM_ERROR",
        detail="rejected",
    )
    summary = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(_paper_loop_result(status=PaperLoopStatus.BROKER_REJECTED),),
        debug_events=(event,),
    )
    assert summary.debug_event_ids == (event.event_id,)


def test_decision_snapshot_ids_deterministic() -> None:
    summary = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(
            _paper_loop_result(
                status=PaperLoopStatus.FILLED,
                decision_snapshot_ids=(DecisionId("paper-loop-002"),),
            ),
            _paper_loop_result(
                status=PaperLoopStatus.NOOP,
                decision_snapshot_ids=(DecisionId("paper-loop-001"),),
            ),
        ),
    )
    assert [item.value for item in summary.decision_snapshot_ids] == [
        "paper-loop-001",
        "paper-loop-002",
    ]


def test_market_observations_preserved() -> None:
    summary = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(_paper_loop_result(status=PaperLoopStatus.NOOP),),
        market_observations=("kr market closed early",),
    )
    assert summary.market_observations == ("kr market closed early",)


def test_portfolio_state_canonical() -> None:
    summary = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(_paper_loop_result(status=PaperLoopStatus.NOOP),),
        portfolio_state={"cash": "1000", "positions": 2},
    )
    assert summary.portfolio_state == {"cash": "1000", "positions": 2}


def test_no_mutation_of_input_objects() -> None:
    result = _paper_loop_result(status=PaperLoopStatus.FILLED)
    event = DebugEvent.create(
        timestamp_kst=NOW,
        source=DebugEventSource.RUNTIME,
        event_code="RUNTIME_EXCEPTION",
        detail="test",
    )
    before_result_status = result.status
    before_event_detail = event.detail

    build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(result,),
        debug_events=(event,),
    )

    assert result.status == before_result_status
    assert event.detail == before_event_detail


def test_daily_summary_store_save_and_restore(tmp_path: Path) -> None:
    store = DailySummaryStore(tmp_path / "daily.jsonl")
    summary = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(_paper_loop_result(status=PaperLoopStatus.NOOP),),
        ending_nav=Money.from_str("100000000", Currency.KRW),
    )

    store.save(summary)
    restored = DailySummaryStore(tmp_path / "daily.jsonl").get(summary.summary_id)

    assert restored == summary


def test_daily_summary_store_duplicate_reject(tmp_path: Path) -> None:
    store = DailySummaryStore(tmp_path / "daily.jsonl")
    summary = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(),
    )
    store.save(summary)

    with pytest.raises(ValueError, match="duplicate summary_id"):
        store.save(summary)


def test_daily_summary_store_invalid_row_raises(tmp_path: Path) -> None:
    path = tmp_path / "daily.jsonl"
    path.write_text("{not-json}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSONL row"):
        DailySummaryStore(path).list_summaries()
