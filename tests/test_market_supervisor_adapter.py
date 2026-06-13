"""RTM-7b — market supervisor adapter tests + isolation guards."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from data.kis_ws_source import KisWsTransportEvent
from data.market_supervisor_adapter import (
    AdapterError,
    InformationalTransportEvent,
    MarketSupervisorAdapter,
)
from market_data.health_policy import MarketHealthTracker, RecordResult, provisional_thresholds
from market_data.models import MarketEventType
from market_data.monitor import MonitorEvidence, MonitorState

_REPO = Path(__file__).resolve().parents[1]
_DATA = _REPO / "src" / "data"
_MARKET_DATA = _REPO / "src" / "market_data"

_KST = ZoneInfo("Asia/Seoul")
_T0 = datetime(2026, 6, 15, 10, 0, tzinfo=_KST)


def test_ack_does_not_set_all_subscribed() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    tracker = MarketHealthTracker(provisional_thresholds())
    _connected(tracker, _T0)
    result = adapter.forward_kis_transport(KisWsTransportEvent(kind="ack", at=_T0), tracker)
    assert result is None
    assert tracker.all_subscribed is False


def test_subscribed_does_not_set_all_subscribed() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    tracker = MarketHealthTracker(provisional_thresholds())
    _connected(tracker, _T0)
    adapted = adapter.adapt_kis_transport(KisWsTransportEvent(kind="subscribed", at=_T0))
    assert isinstance(adapted, InformationalTransportEvent)
    assert tracker.all_subscribed is False


def test_all_subscribed_sets_tracker_state() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    tracker = MarketHealthTracker(provisional_thresholds())
    _connected(tracker, _T0)
    result = adapter.forward_kis_transport(KisWsTransportEvent(kind="all_subscribed", at=_T0), tracker)
    assert result is RecordResult.RECORDED
    assert tracker.all_subscribed is True


def test_two_acks_then_all_subscribed_sequence() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    tracker = MarketHealthTracker(provisional_thresholds())
    _connected(tracker, _T0)
    adapter.forward_kis_transport(KisWsTransportEvent(kind="ack", at=_T0), tracker)
    assert tracker.all_subscribed is False
    adapter.forward_kis_transport(KisWsTransportEvent(kind="ack", at=_T0 + timedelta(seconds=1)), tracker)
    assert tracker.all_subscribed is False
    adapter.forward_kis_transport(KisWsTransportEvent(kind="all_subscribed", at=_T0 + timedelta(seconds=2)), tracker)
    assert tracker.all_subscribed is True


def _connected(tracker: MarketHealthTracker, at: datetime) -> None:
    tracker.record_transport_event(kind="connected", at=at, now=at)


def test_kis_unknown_kind_fail_closed() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    with pytest.raises(AdapterError):
        adapter.adapt_kis_transport(KisWsTransportEvent(kind="bogus", at=_T0))


def test_forward_connected() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    tracker = MarketHealthTracker(provisional_thresholds())
    result = adapter.forward_kis_transport(KisWsTransportEvent(kind="connected", at=_T0), tracker)
    assert result is RecordResult.RECORDED


def test_market_data_does_not_import_data() -> None:
    offenders: list[str] = []
    for path in sorted(_MARKET_DATA.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name.split(".")[0] == "data" or name.startswith("data."):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []
