from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain.enums import Market
from emergency.models import EmergencyTriggerEvent, EmergencyTriggerStatus, EmergencyTriggerType
from emergency.store import EmergencyEventStore, MddEventLog
from emergency_fixtures import sample_mdd_payload, sample_stock_drop_payload


def _sample_event(*, event_id: str = "event-001") -> EmergencyTriggerEvent:
    return EmergencyTriggerEvent(
        event_id=event_id,
        payload=sample_stock_drop_payload(),
        created_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
    )


def test_save_get_roundtrip(tmp_path: Path) -> None:
    store = EmergencyEventStore(tmp_path / "emergency.jsonl")
    event = _sample_event()
    store.save(event)

    loaded = store.get(event.event_id)
    assert loaded is not None
    assert loaded.to_canonical_dict() == event.to_canonical_dict()


def test_duplicate_event_id_reject(tmp_path: Path) -> None:
    store = EmergencyEventStore(tmp_path / "emergency.jsonl")
    event = _sample_event()
    store.save(event)

    with pytest.raises(ValueError, match="duplicate event_id"):
        store.save(event)


def test_missing_file_iterates_empty(tmp_path: Path) -> None:
    store = EmergencyEventStore(tmp_path / "missing.jsonl")
    assert store.list_events() == ()
    assert store.get("missing") is None


def test_corrupted_json_row_raises(tmp_path: Path) -> None:
    path = tmp_path / "emergency.jsonl"
    path.write_text("{not-json}\n", encoding="utf-8")
    store = EmergencyEventStore(path)

    with pytest.raises(ValueError, match=r"invalid JSONL row at line 1"):
        tuple(store.iter_events())


def test_filter_by_trigger_type_market_symbol_status(tmp_path: Path) -> None:
    store = EmergencyEventStore(tmp_path / "emergency.jsonl")
    stock_event = _sample_event(event_id="stock")
    mdd_event = EmergencyTriggerEvent(
        event_id="mdd",
        payload=sample_mdd_payload(),
        created_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
    )
    store.save(stock_event)
    store.save(mdd_event)

    assert len(store.list_events(trigger_type=EmergencyTriggerType.STOCK_DROP)) == 1
    assert len(store.list_events(trigger_type=EmergencyTriggerType.MDD_KILLSWITCH)) == 1
    assert len(store.list_events(market=Market.KR)) == 1
    assert len(store.list_events(symbol="005930")) == 1
    assert len(store.list_events(status=EmergencyTriggerStatus.DETECTED)) == 2


def test_mdd_event_log(tmp_path: Path) -> None:
    log = MddEventLog(tmp_path / "mdd.jsonl")
    log.save(
        EmergencyTriggerEvent(
            event_id="mdd-1",
            payload=sample_mdd_payload(),
            created_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        )
    )
    log.save(
        EmergencyTriggerEvent(
            event_id="stock-1",
            payload=sample_stock_drop_payload(),
            created_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        )
    )
    mdd_only = log.list_mdd_events()
    assert len(mdd_only) == 1
    assert mdd_only[0].payload.trigger_type == EmergencyTriggerType.MDD_KILLSWITCH
