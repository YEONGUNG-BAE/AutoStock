from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain.enums import Market
from emergency.models import EmergencyTriggerStatus, EmergencyTriggerType
from emergency.scout_context import build_emergency_scout_context, compute_portfolio_snapshot_hash
from emergency_fixtures import sample_mdd_payload, sample_stock_drop_payload


def test_builds_context_from_trigger_payload() -> None:
    payload = sample_stock_drop_payload()
    snapshot_hash = compute_portfolio_snapshot_hash({"nav": "10000000"})
    context = build_emergency_scout_context(
        trigger_payload=payload,
        portfolio_snapshot_hash=snapshot_hash,
        created_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
    )
    assert context is not None
    assert context.scope_symbols == payload.scope_symbols
    assert context.market == Market.KR


def test_mdd_returns_none_no_llm() -> None:
    payload = sample_mdd_payload()
    context = build_emergency_scout_context(
        trigger_payload=payload,
        portfolio_snapshot_hash="abc123",
        created_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
    )
    assert context is None


def test_profit_run_10_percent_monitoring_returns_none() -> None:
    payload = sample_stock_drop_payload(
        trigger_type=EmergencyTriggerType.PROFIT_RUN,
        requires_llm_review=False,
        status=EmergencyTriggerStatus.NOOP,
    )
    context = build_emergency_scout_context(
        trigger_payload=payload,
        portfolio_snapshot_hash="abc123",
        created_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
    )
    assert context is None


def test_context_has_no_debug_event_code() -> None:
    payload = sample_stock_drop_payload()
    context = build_emergency_scout_context(
        trigger_payload=payload,
        portfolio_snapshot_hash="abc123",
        created_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
    )
    assert context is not None
    canonical = context.to_canonical_dict()
    assert "debug_event_code" not in canonical
    assert "related_debug_event_code" not in canonical


def test_context_includes_required_focus() -> None:
    payload = sample_stock_drop_payload()
    context = build_emergency_scout_context(
        trigger_payload=payload,
        portfolio_snapshot_hash="abc123",
        created_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
    )
    assert context is not None
    assert context.required_focus == "held_stock_and_same_sector_damage"
