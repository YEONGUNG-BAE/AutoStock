from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

from decision.canonical_json import canonical_json_dumps
from domain.identifiers import DecisionId, Percent
from domain.money import Money
from logs.models import DailyRunStatus, DailySummary, DebugEvent
from paper_loop.models import PaperLoopResult, PaperLoopStatus


class DailySummaryStore:
    """DailySummary append-only JSONL 저장소. duplicate summary_id는 거부한다."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def save(self, summary: DailySummary) -> None:
        """DailySummary 한 건을 append한다. duplicate summary_id는 ValueError."""
        existing = self.get(summary.summary_id)
        if existing is not None:
            raise ValueError(f"duplicate summary_id: {summary.summary_id}")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json_dumps(summary.to_canonical_dict())
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")

    def get(self, summary_id: str) -> DailySummary | None:
        """summary_id로 저장된 DailySummary를 조회한다."""
        for summary in self.iter_summaries():
            if summary.summary_id == summary_id:
                return summary
        return None

    def list_summaries(self) -> tuple[DailySummary, ...]:
        """저장된 모든 DailySummary를 write order대로 반환한다."""
        return tuple(self.iter_summaries())

    def iter_summaries(self) -> Iterator[DailySummary]:
        """저장된 DailySummary를 write order대로 순회한다."""
        if not self._path.exists():
            return

        with self._path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"invalid JSONL row at line {line_number} in {self._path}"
                    ) from exc

                if not isinstance(payload, dict):
                    raise ValueError(
                        f"invalid JSONL row at line {line_number} in {self._path}: "
                        "row must be a JSON object."
                    )

                yield _daily_summary_from_canonical_dict(payload)


def build_daily_summary(
    *,
    trading_date: date,
    created_at: datetime,
    results: tuple[PaperLoopResult, ...],
    debug_events: tuple[DebugEvent, ...] = (),
    ending_cash: Money | None = None,
    ending_nav: Money | None = None,
    portfolio_state: dict[str, Any] | None = None,
    asset_class_weights: dict[str, Percent] | None = None,
    market_observations: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> DailySummary:
    """PaperLoopResult tuple을 하루 DailySummary로 pure projection한다."""
    filled_orders = 0
    rejected_orders = 0
    blocked_orders = 0
    noop_count = 0
    validation_failed_count = 0
    nav_snapshot_count = 0
    symbols: set[str] = set()
    decision_ids: set[str] = set()

    for result in results:
        if result.status == PaperLoopStatus.FILLED:
            filled_orders += 1
        elif result.status == PaperLoopStatus.BROKER_REJECTED:
            rejected_orders += 1
        elif result.status == PaperLoopStatus.RISK_BLOCKED:
            blocked_orders += 1
        elif result.status == PaperLoopStatus.NOOP:
            noop_count += 1
        elif result.status in {
            PaperLoopStatus.QUANTITY_FAILED,
            PaperLoopStatus.VALIDATION_FAILED,
        }:
            validation_failed_count += 1

        if result.nav_snapshot is not None:
            nav_snapshot_count += 1

        symbol = _extract_symbol(result)
        if symbol is not None:
            symbols.add(symbol)

        for decision_id in result.decision_snapshot_ids:
            decision_ids.add(decision_id.value)

    total_runs = len(results)
    status = _resolve_daily_status(
        total_runs=total_runs,
        filled_orders=filled_orders,
        rejected_orders=rejected_orders,
        blocked_orders=blocked_orders,
        noop_count=noop_count,
        validation_failed_count=validation_failed_count,
    )

    summary_id = f"daily-{trading_date.isoformat()}"
    canonical_metadata = {} if metadata is None else metadata

    return DailySummary(
        summary_id=summary_id,
        trading_date=trading_date,
        created_at=created_at,
        status=status,
        total_runs=total_runs,
        filled_orders=filled_orders,
        rejected_orders=rejected_orders,
        blocked_orders=blocked_orders,
        noop_count=noop_count,
        validation_failed_count=validation_failed_count,
        nav_snapshot_count=nav_snapshot_count,
        range_violation_count=0,
        allocator_fallback_count=0,
        ending_cash=ending_cash,
        ending_nav=ending_nav,
        portfolio_state=portfolio_state,
        asset_class_weights=asset_class_weights,
        symbols_touched=tuple(sorted(symbols)),
        market_observations=market_observations,
        debug_event_ids=tuple(event.event_id for event in debug_events),
        decision_snapshot_ids=tuple(
            DecisionId(value) for value in sorted(decision_ids)
        ),
        metadata=canonical_metadata,
    )


def _resolve_daily_status(
    *,
    total_runs: int,
    filled_orders: int,
    rejected_orders: int,
    blocked_orders: int,
    noop_count: int,
    validation_failed_count: int,
) -> DailyRunStatus:
    if total_runs == 0:
        return DailyRunStatus.NOOP

    failed_count = rejected_orders + blocked_orders + validation_failed_count
    success_count = filled_orders + noop_count

    if failed_count == 0 and filled_orders > 0 and noop_count == 0:
        return DailyRunStatus.COMPLETED

    if failed_count == 0 and filled_orders > 0:
        return DailyRunStatus.COMPLETED

    if failed_count == 0 and noop_count == total_runs:
        return DailyRunStatus.NOOP

    if failed_count > 0 and success_count > 0:
        return DailyRunStatus.PARTIAL

    if failed_count > 0 and success_count == 0:
        return DailyRunStatus.FAILED

    return DailyRunStatus.NOOP


def _extract_symbol(result: PaperLoopResult) -> str | None:
    if result.fill is not None:
        return result.fill.symbol
    if result.executable_order_intent is not None:
        return result.executable_order_intent.symbol
    if result.generated_order_intent is not None:
        return result.generated_order_intent.symbol
    if result.broker_order_result is not None:
        return result.broker_order_result.symbol
    return None


def _daily_summary_from_canonical_dict(payload: dict[str, Any]) -> DailySummary:
    """JSONL row dict를 DailySummary domain model로 복원한다."""
    ending_cash = None
    if "ending_cash" in payload:
        cash_payload = payload["ending_cash"]
        ending_cash = Money.model_validate(cash_payload)

    ending_nav = None
    if "ending_nav" in payload:
        nav_payload = payload["ending_nav"]
        ending_nav = Money.model_validate(nav_payload)

    asset_class_weights = None
    if "asset_class_weights" in payload:
        asset_class_weights = {
            key: Percent(value)
            for key, value in payload["asset_class_weights"].items()
        }

    return DailySummary(
        summary_id=payload["summary_id"],
        trading_date=date.fromisoformat(payload["trading_date"]),
        created_at=datetime.fromisoformat(payload["created_at"]),
        status=DailyRunStatus(payload["status"]),
        total_runs=payload.get("total_runs", 0),
        filled_orders=payload.get("filled_orders", 0),
        rejected_orders=payload.get("rejected_orders", 0),
        blocked_orders=payload.get("blocked_orders", 0),
        noop_count=payload.get("noop_count", 0),
        validation_failed_count=payload.get("validation_failed_count", 0),
        nav_snapshot_count=payload.get("nav_snapshot_count", 0),
        range_violation_count=payload.get("range_violation_count", 0),
        allocator_fallback_count=payload.get("allocator_fallback_count", 0),
        ending_cash=ending_cash,
        ending_nav=ending_nav,
        portfolio_state=payload.get("portfolio_state"),
        asset_class_weights=asset_class_weights,
        symbols_touched=tuple(payload.get("symbols_touched", ())),
        market_observations=tuple(payload.get("market_observations", ())),
        debug_event_ids=tuple(payload.get("debug_event_ids", ())),
        decision_snapshot_ids=tuple(
            DecisionId(value) for value in payload.get("decision_snapshot_ids", ())
        ),
        notes=payload.get("notes"),
        metadata=payload.get("metadata", {}),
    )
