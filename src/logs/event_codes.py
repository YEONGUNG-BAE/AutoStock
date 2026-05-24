from __future__ import annotations

from collections.abc import Mapping
from typing import Final

# docs/DEBUG_EVENT_CODES.md와 동기화되는 canonical event_code catalog.
DEBUG_EVENT_CODE_CATALOG: Final[frozenset[str]] = frozenset(
    {
        "ALLOCATOR_CASH_TARGET_OUT_OF_RANGE",
        "ALLOCATOR_GOLD_EXCEPTION_REASON_MISSING",
        "ALLOCATOR_GOLD_TARGET_OUT_OF_RANGE",
        "ALLOCATOR_POLICY_INCONSISTENT",
        "ALLOCATOR_REASON_CODE_INVALID",
        "ALLOCATOR_REJECTED_FALLBACK",
        "ALLOCATOR_TARGET_SUM_INVALID",
        "ANALYSIS_ACTION_INVALID",
        "ANALYSIS_ASSET_RANGE_VIOLATION",
        "ANALYSIS_REASONING_DATE_ID_MISSING",
        "ANALYSIS_SINGLE_NAME_BUY_CAP_EXCEEDED",
        "ANALYSIS_WEIGHT_INVALID",
        "BROKER_API_ERROR",
        "CONFIG_LOAD_ERROR",
        "DATA_ADAPTER_ERROR",
        "DATE_ID_FORMAT_INVALID",
        "DATE_ID_MISSING",
        "DATE_ID_NOT_FOUND",
        "EMERGENCY_TRIGGER_RATE_LIMITED",
        "ENV_VAR_MISSING",
        "EVIDENCE_CONTRADICTION",
        "EVIDENCE_STALE",
        "GOLD_DECREASE_LIMIT_EXCEEDED",
        "GOLD_INCREASE_LIMIT_EXCEEDED",
        "GOLD_MICRO_ADJUST_IGNORED",
        "GOLD_TRADE_BLOCKED_MONTHLY_LIMIT",
        "GOLD_TRADE_BLOCKED_QUARTERLY_LIMIT",
        "LIVE_TRADING_GATE_FAILED",
        "LLM_ENUM_INVALID",
        "LLM_FIELD_MISSING",
        "LLM_FIELD_TYPE_INVALID",
        "LLM_JSON_PARSE_ERROR",
        "LLM_MARKDOWN_WRAPPER_ERROR",
        "LLM_SCHEMA_ERROR",
        "MARKET_CLOSED_ORDER_ATTEMPT",
        "MDD_COOLDOWN_ACTIVE",
        "MDD_LEVEL_1_TRIGGERED",
        "MDD_LEVEL_2_TRIGGERED",
        "MDD_LEVEL_3_TRIGGERED",
        "ORDER_FILL_ERROR",
        "ORDER_SUBMIT_ERROR",
        "PAPER_BROKER_SIM_ERROR",
        "PAPER_NAV_SNAPSHOT_ERROR",
        "PARTIAL_FILL_HANDLING_ERROR",
        "RISK_FILTER_ORDER_REJECTED",
        "RUNTIME_EXCEPTION",
        "SCHEDULER_CALENDAR_ERROR",
        "SCHEDULER_MISFIRE",
        "SLIPPAGE_REJECTION",
        "UNTRADEABLE_SECURITY",
    }
)

DEBUG_EVENT_DEFAULT_SEVERITY: Final[Mapping[str, str]] = {
    "ALLOCATOR_CASH_TARGET_OUT_OF_RANGE": "HIGH",
    "ALLOCATOR_GOLD_EXCEPTION_REASON_MISSING": "MEDIUM",
    "ALLOCATOR_GOLD_TARGET_OUT_OF_RANGE": "HIGH",
    "ALLOCATOR_POLICY_INCONSISTENT": "HIGH",
    "ALLOCATOR_REASON_CODE_INVALID": "MEDIUM",
    "ALLOCATOR_REJECTED_FALLBACK": "HIGH",
    "ALLOCATOR_TARGET_SUM_INVALID": "HIGH",
    "ANALYSIS_ACTION_INVALID": "HIGH",
    "ANALYSIS_ASSET_RANGE_VIOLATION": "MEDIUM",
    "ANALYSIS_REASONING_DATE_ID_MISSING": "HIGH",
    "ANALYSIS_SINGLE_NAME_BUY_CAP_EXCEEDED": "HIGH",
    "ANALYSIS_WEIGHT_INVALID": "HIGH",
    "BROKER_API_ERROR": "HIGH",
    "CONFIG_LOAD_ERROR": "CRITICAL",
    "DATA_ADAPTER_ERROR": "HIGH",
    "DATE_ID_FORMAT_INVALID": "MEDIUM",
    "DATE_ID_MISSING": "HIGH",
    "DATE_ID_NOT_FOUND": "HIGH",
    "EMERGENCY_TRIGGER_RATE_LIMITED": "LOW",
    "ENV_VAR_MISSING": "HIGH",
    "EVIDENCE_CONTRADICTION": "HIGH",
    "EVIDENCE_STALE": "MEDIUM",
    "GOLD_DECREASE_LIMIT_EXCEEDED": "MEDIUM",
    "GOLD_INCREASE_LIMIT_EXCEEDED": "MEDIUM",
    "GOLD_MICRO_ADJUST_IGNORED": "LOW",
    "GOLD_TRADE_BLOCKED_MONTHLY_LIMIT": "LOW",
    "GOLD_TRADE_BLOCKED_QUARTERLY_LIMIT": "LOW",
    "LIVE_TRADING_GATE_FAILED": "CRITICAL",
    "LLM_ENUM_INVALID": "MEDIUM",
    "LLM_FIELD_MISSING": "HIGH",
    "LLM_FIELD_TYPE_INVALID": "HIGH",
    "LLM_JSON_PARSE_ERROR": "HIGH",
    "LLM_MARKDOWN_WRAPPER_ERROR": "MEDIUM",
    "LLM_SCHEMA_ERROR": "HIGH",
    "MARKET_CLOSED_ORDER_ATTEMPT": "MEDIUM",
    "MDD_COOLDOWN_ACTIVE": "LOW",
    "MDD_LEVEL_1_TRIGGERED": "HIGH",
    "MDD_LEVEL_2_TRIGGERED": "HIGH",
    "MDD_LEVEL_3_TRIGGERED": "CRITICAL",
    "ORDER_FILL_ERROR": "HIGH",
    "ORDER_SUBMIT_ERROR": "HIGH",
    "PAPER_BROKER_SIM_ERROR": "HIGH",
    "PAPER_NAV_SNAPSHOT_ERROR": "HIGH",
    "PARTIAL_FILL_HANDLING_ERROR": "MEDIUM",
    "RISK_FILTER_ORDER_REJECTED": "MEDIUM",
    "RUNTIME_EXCEPTION": "HIGH",
    "SCHEDULER_CALENDAR_ERROR": "HIGH",
    "SCHEDULER_MISFIRE": "MEDIUM",
    "SLIPPAGE_REJECTION": "LOW",
    "UNTRADEABLE_SECURITY": "MEDIUM",
}

DEBUG_EVENT_SOURCE_VALUES: Final[frozenset[str]] = frozenset(
    {
        "Allocator",
        "Analysis",
        "Broker",
        "Config",
        "DataAdapter",
        "PaperBroker",
        "PythonValidator",
        "RiskFilter",
        "Runtime",
        "Scheduler",
        "Scout",
    }
)


def is_valid_debug_event_code(value: str) -> bool:
    """등록된 Debug event_code인지 확인한다."""
    return value in DEBUG_EVENT_CODE_CATALOG


def parse_debug_event_code(value: str) -> str:
    """canonical Debug event_code를 파싱하고 catalog membership을 검증한다."""
    if not isinstance(value, str):
        raise ValueError("event_code must be a string.")

    normalized = value.strip()
    if not normalized:
        raise ValueError("event_code must not be blank.")

    if normalized != value:
        raise ValueError("event_code must not contain leading or trailing whitespace.")

    if not is_valid_debug_event_code(normalized):
        raise ValueError(f"unknown debug event_code: {normalized!r}")

    return normalized


def default_severity_for_event_code(event_code: str) -> str:
    """event_code의 catalog default severity 문자열을 반환한다."""
    parsed = parse_debug_event_code(event_code)
    return DEBUG_EVENT_DEFAULT_SEVERITY[parsed]
