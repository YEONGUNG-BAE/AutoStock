"""RTM-7b — market supervisor adapter tests + isolation guards."""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from data.kis_ws_source import KisWsTransportEvent
from data.market_supervisor_adapter import AdapterError, MarketSupervisorAdapter
from market_data.health_policy import MarketHealthTracker, RecordResult, provisional_thresholds
from market_data.models import MarketEventType
from market_data.monitor import MonitorEvidence, MonitorState

_REPO = Path(__file__).resolve().parents[1]
_DATA = _REPO / "src" / "data"
_MARKET_DATA = _REPO / "src" / "market_data"

_KST = ZoneInfo("Asia/Seoul")
_T0 = datetime(2026, 6, 15, 10, 0, tzinfo=_KST)


def test_kis_connected_maps_to_neutral() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    signal = adapter.adapt_kis_transport(KisWsTransportEvent(kind="connected", at=_T0))
    assert signal.kind == "connected"
    assert signal.at == _T0


def test_kis_all_subscribed_maps() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    signal = adapter.adapt_kis_transport(KisWsTransportEvent(kind="all_subscribed", at=_T0))
    assert signal.kind == "all_subscribed"


def test_kis_unknown_kind_fail_closed() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    with pytest.raises(AdapterError):
        adapter.adapt_kis_transport(KisWsTransportEvent(kind="bogus", at=_T0))


def test_kis_missing_at_fail_closed() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    with pytest.raises(AdapterError):
        adapter.adapt_kis_transport(KisWsTransportEvent(kind="connected"))


def test_monitor_quote_maps_to_market_signal() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    ev = MonitorEvidence(
        timestamp=_T0,
        monitor_session_id="s",
        state=MonitorState.RUNNING,
        connection_attempt=1,
        consecutive_failures=0,
        kind="apply",
        event_type=MarketEventType.BEST_BID_ASK.value,
        apply_status="applied",
    )
    signal = adapter.adapt_monitor_evidence(ev)
    assert signal is not None
    assert signal.event_type == "best_bid_ask"


def test_monitor_connect_maps_to_transport() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    ev = MonitorEvidence(
        timestamp=_T0,
        monitor_session_id="s",
        state=MonitorState.CONNECTING,
        connection_attempt=1,
        consecutive_failures=0,
        kind="connect",
    )
    signal = adapter.adapt_monitor_transport(ev)
    assert signal is not None
    assert signal.kind == "connected"


def test_forward_to_tracker() -> None:
    adapter = MarketSupervisorAdapter(clock=lambda: _T0)
    tracker = MarketHealthTracker(provisional_thresholds())
    event = KisWsTransportEvent(kind="connected", at=_T0)
    result = adapter.forward_kis_transport(event, tracker)
    assert result is RecordResult.RECORDED


def test_sanitize_secret_reason() -> None:
    assert MarketSupervisorAdapter.sanitize_reason_code("approval_key_leaked") == "sanitized"
    assert MarketSupervisorAdapter.sanitize_reason_code("quote_starvation") == "quote_starvation"


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


def test_adapter_does_not_import_broker_ledger_execution() -> None:
    forbidden = {"broker", "ledger", "execution", "paper_loop", "paper_execution"}
    tree = ast.parse((_DATA / "market_supervisor_adapter.py").read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert forbidden.isdisjoint(roots)
