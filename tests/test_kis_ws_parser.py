"""RTM-1 — KIS WebSocket fixture parser tests (network/credential/broker-free)."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from market_data.kis_ws_parser import (
    PROVIDER_CONTRACT,
    KisWsFrameParser,
    KisWsParseError,
    SequenceTracker,
    SequenceViolationError,
)
from market_data.models import (
    MarketHeartbeat,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "market_data" / "kis_ws"
MARKET_DATA_SRC = REPO_ROOT / "src" / "market_data"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _trade_frame(**overrides: Any) -> dict[str, Any]:
    frame = _load("valid_trade.json")
    frame.update(overrides)
    return frame


def _quote_frame(**overrides: Any) -> dict[str, Any]:
    frame = _load("valid_best_bid_ask.json")
    frame.update(overrides)
    return frame


# --- valid frames -----------------------------------------------------------


def test_parse_valid_trade() -> None:
    event = KisWsFrameParser().parse(_load("valid_trade.json"))
    assert isinstance(event, NormalizedTradeTick)
    assert event.symbol == "005930"
    assert str(event.price) == "70000"
    assert event.provider_sequence.channel == "H0STCNT0|005930"


def test_parse_valid_best_bid_ask() -> None:
    event = KisWsFrameParser().parse(_load("valid_best_bid_ask.json"))
    assert isinstance(event, NormalizedBestBidAsk)
    assert event.bid_price < event.ask_price


def test_parse_valid_heartbeat() -> None:
    event = KisWsFrameParser().parse(_load("valid_heartbeat.json"))
    assert isinstance(event, MarketHeartbeat)
    assert event.provider_sequence is None


# --- fail-closed validation -------------------------------------------------


def test_timezone_naive_timestamp_rejected() -> None:
    frame = _trade_frame()
    frame["payload"]["trade_at"] = "2026-06-08T00:05:00"
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(frame)


def test_zero_or_negative_price_rejected() -> None:
    for bad in ("0", "-1"):
        frame = _trade_frame()
        frame["payload"]["price"] = bad
        with pytest.raises(KisWsParseError):
            KisWsFrameParser().parse(frame)


def test_zero_or_negative_quantity_rejected() -> None:
    for bad in ("0", "-3"):
        frame = _trade_frame()
        frame["payload"]["quantity"] = bad
        with pytest.raises(KisWsParseError):
            KisWsFrameParser().parse(frame)


def test_malformed_numeric_rejected() -> None:
    frame = _trade_frame()
    frame["payload"]["price"] = "not-a-number"
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(frame)


def test_crossed_book_rejected() -> None:
    frame = _quote_frame()
    frame["payload"]["bid_price"] = "70001"
    frame["payload"]["ask_price"] = "70000"
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(frame)


def test_blank_symbol_rejected() -> None:
    frame = _trade_frame()
    frame["payload"]["symbol"] = "   "
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(frame)


def test_unknown_market_rejected() -> None:
    frame = _trade_frame()
    frame["payload"]["market"] = "JP"
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(frame)


def test_unknown_event_type_rejected() -> None:
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(_trade_frame(type="liquidation"))


def test_unknown_provider_contract_rejected() -> None:
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(_trade_frame(provider_contract="kis-ws-fixture-v2"))


def test_missing_payload_field_rejected() -> None:
    frame = _trade_frame()
    del frame["payload"]["price"]
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(frame)


def test_missing_timestamp_rejected() -> None:
    frame = _trade_frame()
    del frame["payload"]["trade_at"]
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(frame)


def test_trade_without_sequence_rejected() -> None:
    frame = _trade_frame()
    del frame["sequence"]
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(frame)


def test_extra_envelope_field_rejected() -> None:
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(_trade_frame(injected="x"))


def test_extra_payload_field_rejected() -> None:
    frame = _trade_frame()
    frame["payload"]["injected"] = "x"
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(frame)


def test_non_mapping_frame_rejected() -> None:
    with pytest.raises(KisWsParseError):
        KisWsFrameParser().parse(["not", "a", "mapping"])


# --- sequence discipline ----------------------------------------------------


def test_duplicate_sequence_rejected() -> None:
    parser = KisWsFrameParser()
    parser.parse(_trade_frame(sequence=1))
    with pytest.raises(SequenceViolationError):
        parser.parse(_trade_frame(sequence=1))


def test_decreasing_sequence_rejected() -> None:
    parser = KisWsFrameParser()
    parser.parse(_trade_frame(sequence=5))
    with pytest.raises(SequenceViolationError):
        parser.parse(_trade_frame(sequence=4))


def test_sequence_gap_rejected() -> None:
    parser = KisWsFrameParser()
    parser.parse(_trade_frame(sequence=1))
    with pytest.raises(SequenceViolationError):
        parser.parse(_trade_frame(sequence=3))


def test_monotonic_sequence_accepted() -> None:
    parser = KisWsFrameParser()
    parser.parse(_trade_frame(sequence=1))
    parser.parse(_trade_frame(sequence=2))
    parser.parse(_trade_frame(sequence=3))


def test_independent_channels_keep_independent_sequences() -> None:
    parser = KisWsFrameParser()
    trade_channel = _trade_frame()["channel"]
    quote_channel = _quote_frame()["channel"]
    assert trade_channel != quote_channel

    parser.parse(_trade_frame(sequence=1))
    parser.parse(_quote_frame(sequence=1))  # independent channel may start at 1
    parser.parse(_trade_frame(sequence=2))
    parser.parse(_quote_frame(sequence=2))
    # gap on the quote channel must not be masked by the trade channel progressing
    with pytest.raises(SequenceViolationError):
        parser.parse(_quote_frame(sequence=9))


def test_sequence_tracking_can_be_disabled() -> None:
    parser = KisWsFrameParser(track_sequence=False)
    parser.parse(_trade_frame(sequence=1))
    parser.parse(_trade_frame(sequence=1))  # no SequenceViolationError when disabled


def test_sequence_tracker_unit() -> None:
    tracker = SequenceTracker()
    tracker.observe(provider="kis", channel="a", sequence=0)
    tracker.observe(provider="kis", channel="a", sequence=1)
    tracker.observe(provider="kis", channel="b", sequence=0)  # independent
    with pytest.raises(SequenceViolationError):
        tracker.observe(provider="kis", channel="a", sequence=1)


# --- leakage / isolation guards --------------------------------------------


def test_parser_error_does_not_leak_raw_frame_values() -> None:
    sentinel = "LEAK_SENTINEL_9F3A_DO_NOT_ECHO"
    frame = _trade_frame()
    frame["payload"]["price"] = sentinel
    raw_repr = repr(frame)
    with pytest.raises(KisWsParseError) as excinfo:
        KisWsFrameParser().parse(frame)
    message = str(excinfo.value)
    assert sentinel not in message
    assert raw_repr not in message


def test_contract_constant_is_stable() -> None:
    assert PROVIDER_CONTRACT == "kis-ws-fixture-v1"


# market_data 패키지 전역에서 금지되는 import root. RTM-6 전까지 network/broker/ledger
# /trigger/secret 경로는 어떤 파일에서도 들어와서는 안 된다.
_FORBIDDEN_ROOTS = {
    "broker",
    "ledger",
    "decision",
    "paper_loop",
    "llm",
    "os",
    "socket",
    "ssl",
    "subprocess",
    "http",
    "urllib",
    "requests",
    "aiohttp",
    "websocket",
    "websockets",
    "asyncio",
    "threading",
    "yfinance",
}

# 파일별 예외 allowlist. 가드를 삭제·전면 완화하지 않고 계약을 파일 단위로 좁힌다.
# monitor.py만 asyncio 오케스트레이션을 허용하고, latest_state.py만 threading.Lock을
# 허용한다. socket/websocket/broker/ledger 등은 두 파일에서도 여전히 금지된다.
_ALLOWED_IMPORTS_BY_FILE = {
    "monitor.py": {"asyncio"},
    "supervisor.py": {"asyncio"},
    "latest_state.py": {"threading"},
    "trigger_engine.py": {"threading"},
    "rolling_window.py": {"threading"},
}


def test_market_data_modules_have_no_forbidden_imports() -> None:
    offenders: list[str] = []
    for path in sorted(MARKET_DATA_SRC.glob("*.py")):
        allowed = _ALLOWED_IMPORTS_BY_FILE.get(path.name, set())
        effective_forbidden = _FORBIDDEN_ROOTS - allowed
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                if root in effective_forbidden:
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_asyncio_remains_forbidden_outside_monitor() -> None:
    # asyncio는 monitor.py/supervisor.py(오케스트레이션 레이어)에서만 허용된다.
    # 다른 어떤 파일에서도 새어들면 안 된다.
    for path in sorted(MARKET_DATA_SRC.glob("*.py")):
        if path.name in ("monitor.py", "supervisor.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert name.split(".")[0] != "asyncio", f"{path.name} imports asyncio"
