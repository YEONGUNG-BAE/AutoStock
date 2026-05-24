from __future__ import annotations

from datetime import datetime
from typing import Any

from domain.identifiers import DecisionId
from domain.validation import ValidationResult
from logs.event_codes import default_severity_for_event_code, parse_debug_event_code
from logs.models import DebugEvent, DebugEventSource, LogSeverity
from paper_loop.models import PaperLoopResult, PaperLoopStatus

# validation issue code → docs/DEBUG_EVENT_CODES.md event_code 매핑.
_ISSUE_CODE_TO_EVENT_CODE: dict[str, str] = {
    "ALLOCATOR_SCHEMA_INVALID": "LLM_SCHEMA_ERROR",
    "ANALYSIS_SCHEMA_INVALID": "LLM_SCHEMA_ERROR",
    "SCOUT_SCHEMA_INVALID": "LLM_SCHEMA_ERROR",
    "ALLOCATOR_DATE_ID_MISSING": "DATE_ID_MISSING",
    "ANALYSIS_DATE_ID_MISSING": "ANALYSIS_REASONING_DATE_ID_MISSING",
    "DATE_ID_MISSING": "DATE_ID_MISSING",
    "DATE_ID_MISSING_FIELD": "DATE_ID_MISSING",
    "DATE_ID_INVALID": "DATE_ID_FORMAT_INVALID",
    "ALLOCATOR_DATE_ID_STALE": "EVIDENCE_STALE",
    "ANALYSIS_DATE_ID_STALE": "EVIDENCE_STALE",
    "DATE_ID_STALE": "EVIDENCE_STALE",
    "ALLOCATOR_DATE_ID_FUTURE_SOURCE": "DATE_ID_FORMAT_INVALID",
    "ANALYSIS_DATE_ID_FUTURE_SOURCE": "DATE_ID_FORMAT_INVALID",
    "ALLOCATOR_TARGET_WEIGHTS_SUM_INVALID": "ALLOCATOR_TARGET_SUM_INVALID",
    "ALLOCATOR_GOLD_BAND_VIOLATION": "ALLOCATOR_GOLD_TARGET_OUT_OF_RANGE",
    "ALLOCATOR_CASH_TARGET_BAND_VIOLATION": "ALLOCATOR_CASH_TARGET_OUT_OF_RANGE",
    "ALLOCATOR_CONSISTENCY_CHECK_FAILED": "ALLOCATOR_POLICY_INCONSISTENT",
    "ALLOCATOR_TARGET_WEIGHTS_MISMATCH": "ALLOCATOR_POLICY_INCONSISTENT",
    "ALLOCATOR_CASH_TARGET_MISMATCH": "ALLOCATOR_POLICY_INCONSISTENT",
    "ANALYSIS_ALLOCATOR_TOLERANCE_VIOLATION": "ANALYSIS_ASSET_RANGE_VIOLATION",
    "RISK_DIRECTIONAL_SLIPPAGE_EXCEEDED": "SLIPPAGE_REJECTION",
    "RISK_GOLD_TRADE_FREQUENCY_EXCEEDED": "GOLD_TRADE_BLOCKED_MONTHLY_LIMIT",
    "PAPER_LOOP_INPUT_VALIDATION_FAILED": "RUNTIME_EXCEPTION",
    "PAPER_BROKER_REJECTED": "PAPER_BROKER_SIM_ERROR",
}

# schema_name hint → DebugEventSource
_SCHEMA_SOURCE_MAP: dict[str, DebugEventSource] = {
    "allocator": DebugEventSource.ALLOCATOR,
    "analysis": DebugEventSource.ANALYSIS,
    "scout": DebugEventSource.SCOUT,
    "risk": DebugEventSource.RISK_FILTER,
    "paper_loop": DebugEventSource.RUNTIME,
    "date_id": DebugEventSource.SCOUT,
}


def debug_event_from_validation_result(
    *,
    validation_result: ValidationResult,
    timestamp_kst: datetime,
    source: DebugEventSource | None = None,
    detail: str | None = None,
    severity: LogSeverity | None = None,
    run_id: str | None = None,
    decision_id: DecisionId | None = None,
    related_file: str | None = None,
    action_taken: str | None = None,
    fallback: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DebugEvent | None:
    """failed ValidationResult를 DebugEvent로 변환한다. passed=True이면 None."""
    if validation_result.passed:
        return None

    issue_codes = tuple(sorted({issue.code for issue in validation_result.issues}))
    event_code = _resolve_event_code_from_issue_codes(issue_codes)
    resolved_source = source or _infer_source_from_schema(validation_result.schema_name)
    resolved_detail = detail or _join_issue_messages(validation_result)

    return DebugEvent.create(
        timestamp_kst=timestamp_kst,
        source=resolved_source,
        event_code=event_code,
        detail=resolved_detail,
        severity=severity,
        run_id=run_id,
        decision_id=decision_id,
        related_file=related_file,
        action_taken=action_taken,
        fallback=fallback,
        validation_issue_codes=issue_codes,
        metadata=metadata,
    )


def debug_event_from_paper_loop_result(
    *,
    result: PaperLoopResult,
    timestamp_kst: datetime,
    run_id: str | DecisionId | None = None,
    detail: str | None = None,
) -> DebugEvent | None:
    """PaperLoopResult failure status를 DebugEvent로 변환한다. FILLED/NOOP은 None."""
    if result.status in {PaperLoopStatus.FILLED, PaperLoopStatus.NOOP}:
        return None

    normalized_run_id = _normalize_run_id(run_id)
    issue_codes = _collect_paper_loop_issue_codes(result)
    symbol = _extract_symbol_from_result(result)

    if result.status == PaperLoopStatus.RISK_BLOCKED:
        event_code = "RISK_FILTER_ORDER_REJECTED"
        source = DebugEventSource.RISK_FILTER
        resolved_detail = detail or _join_issue_messages_from_codes(result.risk_result)
    elif result.status == PaperLoopStatus.BROKER_REJECTED:
        event_code = "PAPER_BROKER_SIM_ERROR"
        source = DebugEventSource.PAPER_BROKER
        resolved_detail = detail or "Paper broker rejected or produced inconsistent state."
    elif result.status == PaperLoopStatus.QUANTITY_FAILED:
        # quantity resolution failure는 주문 제출 단계 실패로 분류한다.
        event_code = "ORDER_SUBMIT_ERROR"
        source = DebugEventSource.RUNTIME
        resolved_detail = detail or _join_issue_messages(result.validation_result)
    elif result.status == PaperLoopStatus.VALIDATION_FAILED:
        event_code = _resolve_event_code_from_issue_codes(issue_codes)
        source = DebugEventSource.PYTHON_VALIDATOR
        resolved_detail = detail or _join_issue_messages(result.validation_result)
    else:
        event_code = "RUNTIME_EXCEPTION"
        source = DebugEventSource.RUNTIME
        resolved_detail = detail or f"Unhandled paper loop status: {result.status.value}."

    return DebugEvent.create(
        timestamp_kst=timestamp_kst,
        source=source,
        event_code=event_code,
        detail=resolved_detail,
        run_id=normalized_run_id,
        symbol=symbol,
        validation_issue_codes=issue_codes,
    )


def debug_event_from_exception(
    *,
    exc: BaseException,
    timestamp_kst: datetime,
    source: DebugEventSource = DebugEventSource.RUNTIME,
    event_code: str = "RUNTIME_EXCEPTION",
    severity: LogSeverity | None = None,
    detail: str | None = None,
    run_id: str | None = None,
    decision_id: DecisionId | None = None,
    related_file: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DebugEvent:
    """Exception을 DebugEvent로 변환한다."""
    parsed_code = parse_debug_event_code(event_code)
    resolved_detail = detail or str(exc)
    merged_metadata = dict(metadata or {})
    merged_metadata.setdefault("exception_message", str(exc))

    return DebugEvent.create(
        timestamp_kst=timestamp_kst,
        source=source,
        event_code=parsed_code,
        detail=resolved_detail,
        severity=severity or LogSeverity(default_severity_for_event_code(parsed_code)),
        run_id=run_id,
        decision_id=decision_id,
        related_file=related_file,
        exception_type=type(exc).__name__,
        metadata=merged_metadata,
    )


def debug_event_from_nav_snapshot_error(
    *,
    timestamp_kst: datetime,
    detail: str,
    run_id: str | None = None,
    exc: BaseException | None = None,
    metadata: dict[str, Any] | None = None,
) -> DebugEvent:
    """Paper fill 이후 NAV snapshot write failure를 PAPER_NAV_SNAPSHOT_ERROR로 기록한다."""
    merged_metadata = dict(metadata or {})
    if exc is not None:
        merged_metadata.setdefault("exception_message", str(exc))
        exception_type = type(exc).__name__
    else:
        exception_type = None

    return DebugEvent.create(
        timestamp_kst=timestamp_kst,
        source=DebugEventSource.PAPER_BROKER,
        event_code="PAPER_NAV_SNAPSHOT_ERROR",
        detail=detail,
        run_id=run_id,
        exception_type=exception_type,
        metadata=merged_metadata,
    )


def _resolve_event_code_from_issue_codes(issue_codes: tuple[str, ...]) -> str:
    """deterministic sorted issue code 순서로 catalog event_code를 선택한다."""
    for code in sorted(issue_codes):
        mapped = _ISSUE_CODE_TO_EVENT_CODE.get(code)
        if mapped is not None:
            return mapped
    return "RUNTIME_EXCEPTION"


def _infer_source_from_schema(schema_name: str | None) -> DebugEventSource:
    if schema_name is None:
        return DebugEventSource.PYTHON_VALIDATOR

    lowered = schema_name.lower()
    for prefix, source in _SCHEMA_SOURCE_MAP.items():
        if prefix in lowered:
            return source
    return DebugEventSource.PYTHON_VALIDATOR


def _join_issue_messages(validation_result: ValidationResult) -> str:
    if not validation_result.issues:
        return "Validation failed without issue details."
    return "; ".join(issue.message for issue in validation_result.issues)


def _join_issue_messages_from_codes(validation_result: ValidationResult) -> str:
    return _join_issue_messages(validation_result)


def _collect_paper_loop_issue_codes(result: PaperLoopResult) -> tuple[str, ...]:
    codes: set[str] = set()
    for validation in (
        result.validation_result,
        result.risk_result,
        result.order_generation_result.validation_result,
    ):
        for issue in validation.issues:
            codes.add(issue.code)
    if result.quantity_resolution_result is not None:
        for issue in result.quantity_resolution_result.validation_result.issues:
            codes.add(issue.code)
    return tuple(sorted(codes))


def _extract_symbol_from_result(result: PaperLoopResult) -> str | None:
    if result.executable_order_intent is not None:
        return result.executable_order_intent.symbol
    if result.generated_order_intent is not None:
        return result.generated_order_intent.symbol
    return None


def _normalize_run_id(run_id: str | DecisionId | None) -> str | None:
    if run_id is None:
        return None
    if isinstance(run_id, DecisionId):
        return run_id.value
    return run_id
