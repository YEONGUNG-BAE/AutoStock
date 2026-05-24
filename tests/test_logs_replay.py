from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config.settings import ExecutionMode
from decision.canonical_json import canonicalize_payload, payload_sha256
from domain import DecisionId, Market
from domain.enums import AccountRole, AssetClass, OrderSide, OrderType
from domain.order import OrderIntent
from logs import (
    DailySummaryStore,
    DebugEvent,
    DebugEventSource,
    DebugMarkdownWriter,
    JsonlEventLog,
    LogSeverity,
    build_daily_summary,
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


def _order_intent(*, symbol: str = "005930") -> OrderIntent:
    return OrderIntent(
        order_id="order-001",
        correlation_id="corr-001",
        symbol=symbol,
        market=Market.KR,
        asset_class=AssetClass.KR_EQUITY,
        account_role=AccountRole.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        execution_mode=ExecutionMode.NORMAL,
        target_weight_percent=__import__("decimal").Decimal("5"),
        created_at=NOW,
    )


def _paper_loop_result(*, status: PaperLoopStatus) -> PaperLoopResult:
    passed = passed_validation_result(
        schema_name=PAPER_LOOP_SCHEMA,
        validator_version=PAPER_LOOP_VALIDATOR_VERSION,
    )
    return PaperLoopResult(
        status=status,
        validation_result=passed,
        risk_result=passed_validation_result(schema_name="risk.v1", validator_version="phase10"),
        order_generation_result=OrderGenerationResult(
            status=OrderGenerationStatus.NOOP,
            order_intent=None,
            validation_result=passed_validation_result(schema_name="risk.v1", validator_version="phase10"),
        ),
        generated_order_intent=_order_intent() if status == PaperLoopStatus.FILLED else None,
        decision_snapshot_ids=(DecisionId("paper-loop-001"),),
    )


def test_same_debug_event_same_canonical_hash() -> None:
    event = DebugEvent.create(
        timestamp_kst=NOW,
        source=DebugEventSource.ALLOCATOR,
        event_code="LLM_JSON_PARSE_ERROR",
        detail="invalid json",
        metadata={"component": "allocator"},
    )
    first = payload_sha256(canonicalize_payload(event.to_canonical_dict()))
    second = payload_sha256(canonicalize_payload(event.to_canonical_dict()))
    assert first == second


def test_jsonl_append_read_preserves_canonical_dict(tmp_path: Path) -> None:
    event = DebugEvent.create(
        timestamp_kst=NOW,
        source=DebugEventSource.DATA_ADAPTER,
        event_code="DATA_ADAPTER_ERROR",
        detail="fred timeout",
        metadata={"retry": 1},
    )
    log_path = tmp_path / "events.jsonl"
    JsonlEventLog(log_path).append(event)
    restored = JsonlEventLog(log_path).list_events()[0]
    assert restored.to_canonical_dict() == event.to_canonical_dict()


def test_same_paper_loop_results_same_daily_summary_dict() -> None:
    results = (
        _paper_loop_result(status=PaperLoopStatus.FILLED),
        _paper_loop_result(status=PaperLoopStatus.NOOP),
    )
    first = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=results,
    )
    second = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=results,
    )
    assert first.to_canonical_dict() == second.to_canonical_dict()
    assert payload_sha256(first.to_canonical_dict()) == payload_sha256(second.to_canonical_dict())


def test_debug_markdown_rendering_deterministic(tmp_path: Path) -> None:
    event = DebugEvent.create(
        timestamp_kst=NOW,
        source=DebugEventSource.RISK_FILTER,
        event_code="GOLD_TRADE_BLOCKED_MONTHLY_LIMIT",
        detail="gold trade blocked",
        metadata={"policy": "monthly_limit"},
    )
    writer = DebugMarkdownWriter(tmp_path / "Debug.md")
    first = writer.render_event(event)
    second = writer.render_event(event)
    assert first == second


def test_no_mutation_of_input_objects(tmp_path: Path) -> None:
    event = DebugEvent.create(
        timestamp_kst=NOW,
        source=DebugEventSource.CONFIG,
        event_code="CONFIG_LOAD_ERROR",
        detail="missing config",
        metadata={"path": "config/config.toml"},
    )
    before = event.to_canonical_dict()

    JsonlEventLog(tmp_path / "events.jsonl").append(event)
    DebugMarkdownWriter(tmp_path / "Debug.md").append_event(event)
    build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(_paper_loop_result(status=PaperLoopStatus.FILLED),),
        debug_events=(event,),
    )

    assert event.to_canonical_dict() == before


def test_daily_summary_store_order_deterministic(tmp_path: Path) -> None:
    store = DailySummaryStore(tmp_path / "daily.jsonl")
    first = build_daily_summary(
        trading_date=date(2026, 5, 21),
        created_at=NOW,
        results=(_paper_loop_result(status=PaperLoopStatus.NOOP),),
    )
    second = build_daily_summary(
        trading_date=TRADING_DATE,
        created_at=NOW,
        results=(_paper_loop_result(status=PaperLoopStatus.FILLED),),
    )
    store.save(first)
    store.save(second)

    summaries = DailySummaryStore(tmp_path / "daily.jsonl").list_summaries()
    assert [item.summary_id for item in summaries] == [
        first.summary_id,
        second.summary_id,
    ]
