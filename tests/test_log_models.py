from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from decision.canonical_json import canonicalize_payload, payload_sha256
from domain import Currency, DecisionId, Money, Percent
from logs import (
    DailyRunStatus,
    DailySummary,
    DebugEvent,
    DebugEventSource,
    LogSeverity,
)
from logs.debug_writer import _format_timestamp_kst

NOW = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
KST = ZoneInfo("Asia/Seoul")


def _sample_debug_event(**overrides: object) -> DebugEvent:
    base = {
        "event_id": "debug-sample-001",
        "timestamp_kst": NOW,
        "source": DebugEventSource.PAPER_BROKER,
        "severity": LogSeverity.HIGH,
        "event_code": "PAPER_BROKER_SIM_ERROR",
        "detail": "broker rejected order",
    }
    base.update(overrides)
    return DebugEvent(**base)


def test_valid_enum_parsing() -> None:
    assert LogSeverity("HIGH") == LogSeverity.HIGH
    assert DebugEventSource("RiskFilter") == DebugEventSource.RISK_FILTER
    assert DailyRunStatus("COMPLETED") == DailyRunStatus.COMPLETED


def test_invalid_enum_reject() -> None:
    with pytest.raises(ValueError):
        LogSeverity("ERROR")


def test_valid_debug_event() -> None:
    event = _sample_debug_event(
        run_id="paper-loop-001",
        decision_id=DecisionId("paper-loop-001"),
        validation_issue_codes=("PAPER_BROKER_REJECTED",),
        metadata={"attempt": 1},
    )
    assert event.run_id == "paper-loop-001"
    assert event.validation_issue_codes == ("PAPER_BROKER_REJECTED",)


def test_debug_event_extra_field_reject() -> None:
    with pytest.raises(ValueError):
        DebugEvent(
            event_id="debug-001",
            timestamp_kst=NOW,
            source=DebugEventSource.RUNTIME,
            severity=LogSeverity.HIGH,
            event_code="RUNTIME_EXCEPTION",
            detail="failure",
            error_tags={"#추격_매수": 1},
        )


def test_naive_timestamp_kst_reject() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _sample_debug_event(timestamp_kst=datetime(2026, 5, 22, 12, 0))


def test_blank_detail_reject() -> None:
    with pytest.raises(ValueError, match="detail"):
        _sample_debug_event(detail="   ")


def test_optional_blank_reject() -> None:
    with pytest.raises(ValueError, match="run_id"):
        _sample_debug_event(run_id="   ")


def test_metadata_invalid_reject() -> None:
    with pytest.raises(ValueError, match="metadata"):
        _sample_debug_event(metadata={"bad": {1, 2}})


def test_validation_issue_codes_blank_reject() -> None:
    with pytest.raises(ValueError, match="validation_issue_codes"):
        _sample_debug_event(validation_issue_codes=("VALID", "   "))


def test_unknown_event_code_reject() -> None:
    with pytest.raises(ValueError, match="unknown debug event_code"):
        _sample_debug_event(event_code="INVENTED_CODE")


def test_no_error_tags_field_allowed() -> None:
    assert "error_tags" not in DebugEvent.model_fields
    assert "top_error_tags" not in DebugEvent.model_fields


def test_event_type_message_source_component_extra_reject() -> None:
    with pytest.raises(ValueError):
        DebugEvent(
            event_id="debug-001",
            timestamp_kst=NOW,
            source=DebugEventSource.RUNTIME,
            severity=LogSeverity.HIGH,
            event_code="RUNTIME_EXCEPTION",
            detail="failure",
            message="legacy message",
        )


def test_deterministic_create_helper() -> None:
    first = DebugEvent.create(
        timestamp_kst=NOW,
        source=DebugEventSource.RISK_FILTER,
        event_code="RISK_FILTER_ORDER_REJECTED",
        detail="blocked",
        run_id="paper-loop-001",
        validation_issue_codes=("RISK_BLOCKED", "RISK_SOFT_BAND"),
    )
    second = DebugEvent.create(
        timestamp_kst=NOW,
        source=DebugEventSource.RISK_FILTER,
        event_code="RISK_FILTER_ORDER_REJECTED",
        detail="blocked",
        run_id="paper-loop-001",
        validation_issue_codes=("RISK_SOFT_BAND", "RISK_BLOCKED"),
    )
    assert first.event_id == second.event_id
    assert first.validation_issue_codes == ("RISK_BLOCKED", "RISK_SOFT_BAND")


def test_debug_md_rendering_converts_timestamp_to_kst() -> None:
    rendered_ts = _format_timestamp_kst(NOW)
    assert rendered_ts.endswith("+09:00")


def test_valid_daily_summary() -> None:
    summary = DailySummary(
        summary_id="daily-2026-05-22",
        trading_date=date(2026, 5, 22),
        created_at=NOW,
        status=DailyRunStatus.COMPLETED,
        total_runs=2,
        filled_orders=2,
        symbols_touched=("005930", "AAPL"),
        ending_cash=Money.from_str("1000000", Currency.KRW),
    )
    assert summary.total_runs == 2


def test_daily_summary_extra_field_reject() -> None:
    with pytest.raises(ValueError):
        DailySummary(
            summary_id="daily-2026-05-22",
            trading_date=date(2026, 5, 22),
            created_at=NOW,
            status=DailyRunStatus.FAILED,
            error_tags=("tag",),
        )


def test_negative_counts_reject() -> None:
    with pytest.raises(ValueError, match="filled_orders"):
        DailySummary(
            summary_id="daily-2026-05-22",
            trading_date=date(2026, 5, 22),
            created_at=NOW,
            status=DailyRunStatus.FAILED,
            filled_orders=-1,
        )


def test_daily_summary_naive_created_at_reject() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DailySummary(
            summary_id="daily-2026-05-22",
            trading_date=date(2026, 5, 22),
            created_at=datetime(2026, 5, 22, 12, 0),
            status=DailyRunStatus.NOOP,
        )


def test_daily_summary_debug_event_ids_blank_reject() -> None:
    with pytest.raises(ValueError, match="debug_event_ids"):
        DailySummary(
            summary_id="daily-2026-05-22",
            trading_date=date(2026, 5, 22),
            created_at=NOW,
            status=DailyRunStatus.NOOP,
            debug_event_ids=("debug-001", "   "),
        )


def test_daily_summary_market_observations_blank_reject() -> None:
    with pytest.raises(ValueError, match="market_observations"):
        DailySummary(
            summary_id="daily-2026-05-22",
            trading_date=date(2026, 5, 22),
            created_at=NOW,
            status=DailyRunStatus.NOOP,
            market_observations=("valid", "   "),
        )


def test_daily_summary_metadata_invalid_reject() -> None:
    with pytest.raises(ValueError, match="metadata"):
        DailySummary(
            summary_id="daily-2026-05-22",
            trading_date=date(2026, 5, 22),
            created_at=NOW,
            status=DailyRunStatus.NOOP,
            metadata={"bad": float("nan")},
        )


def test_daily_summary_no_error_tags_field() -> None:
    assert "error_tags" not in DailySummary.model_fields
    assert "top_error_tags" not in DailySummary.model_fields


def test_daily_summary_symbols_sorted_unique() -> None:
    summary = DailySummary(
        summary_id="daily-2026-05-22",
        trading_date=date(2026, 5, 22),
        created_at=NOW,
        status=DailyRunStatus.NOOP,
        symbols_touched=("AAPL", "005930", "AAPL"),
    )
    assert summary.symbols_touched == ("005930", "AAPL")


def test_daily_summary_asset_class_weights_percent_validation() -> None:
    summary = DailySummary(
        summary_id="daily-2026-05-22",
        trading_date=date(2026, 5, 22),
        created_at=NOW,
        status=DailyRunStatus.NOOP,
        asset_class_weights={"kr": Percent("40"), "us": Percent("35")},
    )
    assert summary.asset_class_weights is not None
    assert summary.asset_class_weights["kr"].value == Decimal("40")


def test_debug_event_to_canonical_dict_hash_stable() -> None:
    event = DebugEvent.create(
        timestamp_kst=NOW,
        source=DebugEventSource.DATA_ADAPTER,
        event_code="DATA_ADAPTER_ERROR",
        detail="fred timeout",
        metadata={"source": "fred"},
    )
    first_hash = payload_sha256(canonicalize_payload(event.to_canonical_dict()))
    second_hash = payload_sha256(canonicalize_payload(event.to_canonical_dict()))
    assert first_hash == second_hash
