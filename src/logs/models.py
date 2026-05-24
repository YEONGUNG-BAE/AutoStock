from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from decision.canonical_json import canonicalize_payload, payload_sha256
from domain._datetime import require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.identifiers import DecisionId, Percent
from domain.money import Money
from logs.event_codes import default_severity_for_event_code, parse_debug_event_code


class LogSeverity(StrEnum):
    """docs/DEBUG_EVENT_CODES.md severity vocabulary."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class DebugEventSource(StrEnum):
    """docs/DEBUG_EVENT_CODES.md source values."""

    SCOUT = "Scout"
    ANALYSIS = "Analysis"
    ALLOCATOR = "Allocator"
    PYTHON_VALIDATOR = "PythonValidator"
    RISK_FILTER = "RiskFilter"
    BROKER = "Broker"
    PAPER_BROKER = "PaperBroker"
    SCHEDULER = "Scheduler"
    CONFIG = "Config"
    DATA_ADAPTER = "DataAdapter"
    RUNTIME = "Runtime"


class DailyRunStatus(StrEnum):
    """하루 paper loop 실행 집계 상태."""

    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    NOOP = "NOOP"


class DebugEvent(BaseModel):
    """기술/운영 디버그 이벤트. Postmortem error_tags를 포함하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    timestamp_kst: datetime
    source: DebugEventSource
    severity: LogSeverity
    event_code: str
    detail: str
    action_taken: str | None = None
    fallback: str | None = None
    related_file: str | None = None
    human_note: str | None = None
    run_id: str | None = None
    decision_id: DecisionId | None = None
    order_id: str | None = None
    symbol: str | None = None
    market: str | None = None
    validation_issue_codes: tuple[str, ...] = ()
    exception_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id", mode="before")
    @classmethod
    def validate_event_id(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="event_id")

    @field_validator("timestamp_kst", mode="before")
    @classmethod
    def validate_timestamp_kst(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return require_timezone_aware_datetime(value, field_name="timestamp_kst")

    @field_validator("event_code", mode="before")
    @classmethod
    def validate_event_code(cls, value: Any) -> str:
        return parse_debug_event_code(str(value))

    @field_validator("detail", mode="before")
    @classmethod
    def validate_detail(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="detail")

    @field_validator(
        "action_taken",
        "fallback",
        "related_file",
        "human_note",
        "run_id",
        "order_id",
        "symbol",
        "market",
        "exception_type",
        mode="before",
    )
    @classmethod
    def validate_optional_strings(cls, value: Any, info) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("validation_issue_codes", mode="before")
    @classmethod
    def validate_validation_issue_codes(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("validation_issue_codes must be a sequence of strings.")

        normalized: list[str] = []
        for index, item in enumerate(value):
            normalized.append(
                normalize_required_string(item, field_name=f"validation_issue_codes[{index}]")
            )
        return tuple(sorted(normalized))

    @field_validator("metadata", mode="before")
    @classmethod
    def validate_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value

    @model_validator(mode="after")
    def validate_canonical_metadata(self) -> Self:
        expected = canonicalize_payload(self.metadata)
        if self.metadata != expected:
            raise ValueError("metadata must be in canonical JSON-compatible form.")
        return self

    @classmethod
    def create(
        cls,
        *,
        timestamp_kst: datetime,
        source: DebugEventSource,
        event_code: str,
        detail: str,
        severity: LogSeverity | None = None,
        event_id: str | None = None,
        action_taken: str | None = None,
        fallback: str | None = None,
        related_file: str | None = None,
        human_note: str | None = None,
        run_id: str | None = None,
        decision_id: DecisionId | None = None,
        order_id: str | None = None,
        symbol: str | None = None,
        market: str | None = None,
        validation_issue_codes: tuple[str, ...] = (),
        exception_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DebugEvent:
        """canonical payload hash 기반 deterministic event_id를 생성한다."""
        parsed_code = parse_debug_event_code(event_code)
        resolved_severity = severity or LogSeverity(default_severity_for_event_code(parsed_code))
        canonical_metadata = {} if metadata is None else canonicalize_payload(metadata)

        payload_for_id = {
            "timestamp_kst": require_timezone_aware_datetime(
                timestamp_kst,
                field_name="timestamp_kst",
            ).isoformat(),
            "source": source.value,
            "severity": resolved_severity.value,
            "event_code": parsed_code,
            "detail": normalize_required_string(detail, field_name="detail"),
            **({"action_taken": action_taken} if action_taken is not None else {}),
            **({"fallback": fallback} if fallback is not None else {}),
            **({"related_file": related_file} if related_file is not None else {}),
            **({"human_note": human_note} if human_note is not None else {}),
            **({"run_id": run_id} if run_id is not None else {}),
            **({"decision_id": decision_id.value} if decision_id is not None else {}),
            **({"order_id": order_id} if order_id is not None else {}),
            **({"symbol": symbol} if symbol is not None else {}),
            **({"market": market} if market is not None else {}),
            **(
                {"validation_issue_codes": list(sorted(validation_issue_codes))}
                if validation_issue_codes
                else {}
            ),
            **({"exception_type": exception_type} if exception_type is not None else {}),
            **({"metadata": canonical_metadata} if canonical_metadata else {}),
        }
        resolved_event_id = event_id or f"debug-{payload_sha256(payload_for_id)[:16]}"

        return cls(
            event_id=resolved_event_id,
            timestamp_kst=timestamp_kst,
            source=source,
            severity=resolved_severity,
            event_code=parsed_code,
            detail=detail,
            action_taken=action_taken,
            fallback=fallback,
            related_file=related_file,
            human_note=human_note,
            run_id=run_id,
            decision_id=decision_id,
            order_id=order_id,
            symbol=symbol,
            market=market,
            validation_issue_codes=validation_issue_codes,
            exception_type=exception_type,
            metadata=canonical_metadata,
        )

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic replay/저장을 위한 canonical dict 표현을 반환한다."""
        payload: dict[str, Any] = {
            "event_id": self.event_id,
            "timestamp_kst": self.timestamp_kst.isoformat(),
            "source": self.source.value,
            "severity": self.severity.value,
            "event_code": self.event_code,
            "detail": self.detail,
        }
        optional_fields = (
            ("action_taken", self.action_taken),
            ("fallback", self.fallback),
            ("related_file", self.related_file),
            ("human_note", self.human_note),
            ("run_id", self.run_id),
            ("decision_id", self.decision_id.value if self.decision_id else None),
            ("order_id", self.order_id),
            ("symbol", self.symbol),
            ("market", self.market),
            ("exception_type", self.exception_type),
        )
        for key, value in optional_fields:
            if value is not None:
                payload[key] = value

        if self.validation_issue_codes:
            payload["validation_issue_codes"] = list(self.validation_issue_codes)

        if self.metadata:
            payload["metadata"] = canonicalize_payload(self.metadata)

        return canonicalize_payload(payload)


class DailySummary(BaseModel):
    """하루 단위 운영/페이퍼 결과 요약. Postmortem error_tags를 포함하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary_id: str
    trading_date: date
    created_at: datetime
    status: DailyRunStatus
    total_runs: int = 0
    filled_orders: int = 0
    rejected_orders: int = 0
    blocked_orders: int = 0
    noop_count: int = 0
    validation_failed_count: int = 0
    nav_snapshot_count: int = 0
    range_violation_count: int = 0
    allocator_fallback_count: int = 0
    ending_cash: Money | None = None
    ending_nav: Money | None = None
    portfolio_state: dict[str, Any] | None = None
    asset_class_weights: dict[str, Percent] | None = None
    symbols_touched: tuple[str, ...] = ()
    market_observations: tuple[str, ...] = ()
    debug_event_ids: tuple[str, ...] = ()
    decision_snapshot_ids: tuple[DecisionId, ...] = ()
    notes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("summary_id", mode="before")
    @classmethod
    def validate_summary_id(cls, value: Any) -> str:
        return normalize_required_string(value, field_name="summary_id")

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return require_timezone_aware_datetime(value, field_name="created_at")

    @field_validator(
        "total_runs",
        "filled_orders",
        "rejected_orders",
        "blocked_orders",
        "noop_count",
        "validation_failed_count",
        "nav_snapshot_count",
        "range_violation_count",
        "allocator_fallback_count",
        mode="after",
    )
    @classmethod
    def validate_non_negative_counts(cls, value: int, info) -> int:
        if value < 0:
            raise ValueError(f"{info.field_name} must be >= 0.")
        return value

    @field_validator("symbols_touched", mode="before")
    @classmethod
    def validate_symbols_touched(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("symbols_touched must be a sequence of strings.")

        normalized: list[str] = []
        for index, item in enumerate(value):
            normalized.append(
                normalize_required_string(item, field_name=f"symbols_touched[{index}]")
            )
        return tuple(sorted(set(normalized)))

    @field_validator("market_observations", mode="before")
    @classmethod
    def validate_market_observations(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("market_observations must be a sequence of strings.")

        normalized: list[str] = []
        for index, item in enumerate(value):
            normalized.append(
                normalize_required_string(item, field_name=f"market_observations[{index}]")
            )
        return tuple(normalized)

    @field_validator("debug_event_ids", mode="before")
    @classmethod
    def validate_debug_event_ids(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("debug_event_ids must be a sequence of strings.")

        normalized: list[str] = []
        for index, item in enumerate(value):
            normalized.append(
                normalize_required_string(item, field_name=f"debug_event_ids[{index}]")
            )
        return tuple(normalized)

    @field_validator("decision_snapshot_ids", mode="before")
    @classmethod
    def validate_decision_snapshot_ids(cls, value: Any) -> tuple[DecisionId, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("decision_snapshot_ids must be a sequence of DecisionId.")

        normalized: list[DecisionId] = []
        for item in value:
            if isinstance(item, DecisionId):
                normalized.append(item)
            else:
                normalized.append(DecisionId(str(item)))
        unique_by_value = {item.value: item for item in normalized}
        return tuple(unique_by_value[value] for value in sorted(unique_by_value))

    @field_validator("notes", mode="before")
    @classmethod
    def validate_notes(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name="notes")

    @field_validator("portfolio_state", "metadata", mode="before")
    @classmethod
    def validate_json_compatible_object(cls, value: Any, info) -> dict[str, Any] | None:
        if value is None:
            return None if info.field_name == "portfolio_state" else {}
        if not isinstance(value, dict):
            raise ValueError(f"{info.field_name} must be a JSON-compatible object.")
        canonicalize_payload(value)
        return value

    @model_validator(mode="after")
    def validate_canonical_objects(self) -> Self:
        if self.portfolio_state is not None:
            expected = canonicalize_payload(self.portfolio_state)
            if self.portfolio_state != expected:
                raise ValueError("portfolio_state must be in canonical JSON-compatible form.")

        expected_metadata = canonicalize_payload(self.metadata)
        if self.metadata != expected_metadata:
            raise ValueError("metadata must be in canonical JSON-compatible form.")

        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic replay/저장을 위한 canonical dict 표현을 반환한다."""
        payload: dict[str, Any] = {
            "summary_id": self.summary_id,
            "trading_date": self.trading_date.isoformat(),
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
            "total_runs": self.total_runs,
            "filled_orders": self.filled_orders,
            "rejected_orders": self.rejected_orders,
            "blocked_orders": self.blocked_orders,
            "noop_count": self.noop_count,
            "validation_failed_count": self.validation_failed_count,
            "nav_snapshot_count": self.nav_snapshot_count,
            "range_violation_count": self.range_violation_count,
            "allocator_fallback_count": self.allocator_fallback_count,
        }

        if self.ending_cash is not None:
            payload["ending_cash"] = {
                "amount": str(self.ending_cash.amount),
                "currency": self.ending_cash.currency.value,
            }
        if self.ending_nav is not None:
            payload["ending_nav"] = {
                "amount": str(self.ending_nav.amount),
                "currency": self.ending_nav.currency.value,
            }
        if self.portfolio_state is not None:
            payload["portfolio_state"] = canonicalize_payload(self.portfolio_state)
        if self.asset_class_weights is not None:
            payload["asset_class_weights"] = {
                key: str(percent.value)
                for key, percent in sorted(self.asset_class_weights.items())
            }
        if self.symbols_touched:
            payload["symbols_touched"] = list(self.symbols_touched)
        if self.market_observations:
            payload["market_observations"] = list(self.market_observations)
        if self.debug_event_ids:
            payload["debug_event_ids"] = list(self.debug_event_ids)
        if self.decision_snapshot_ids:
            payload["decision_snapshot_ids"] = [
                item.value for item in self.decision_snapshot_ids
            ]
        if self.notes is not None:
            payload["notes"] = self.notes
        if self.metadata:
            payload["metadata"] = canonicalize_payload(self.metadata)

        return canonicalize_payload(payload)
