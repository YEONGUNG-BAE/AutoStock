"""RTM-6 — operator KIS websocket read-only smoke CLI tests.

No real network/DNS/credentials. --run wiring is exercised only through the injectable
execute_run seam with a fake websocket + fake approval transport. The CLI must never
auto-invoke a real run and must fail-closed on every gate.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import importlib.util

from config.settings import KisWsReadOnlySettings
from data.kis_ws_source import KisWsSubscription, KisWsSubscriptionError

# ops/ is not a package; load the smoke module by path.
_SMOKE_PATH = Path(__file__).resolve().parents[1] / "ops" / "run_kis_ws_readonly_smoke.py"
_spec = importlib.util.spec_from_file_location("run_kis_ws_readonly_smoke", _SMOKE_PATH)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

_KST = ZoneInfo("Asia/Seoul")
_NOW = datetime(2026, 6, 12, 10, 0, 0, tzinfo=_KST)
_TRADE_LEN = 46


def _trade_frame(*, symbol: str = "005930", prpr: str = "70000") -> str:
    record = ["0"] * _TRADE_LEN
    record[0] = symbol
    record[1] = "095959"
    record[2] = prpr
    record[12] = "10"
    record[13] = "123456"
    record[33] = "20260612"
    return f"0|H0STCNT0|1|{'^'.join(record)}"


class _FakeWebSocket:
    def __init__(self, inbox: list[str]) -> None:
        self._inbox = list(inbox)
        self.sent: list[str] = []
        self.pongs: list[bytes] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._inbox:
            raise RuntimeError("simulated server close (inbox drained).")
        return self._inbox.pop(0)

    async def pong(self, data: bytes = b"") -> None:
        self.pongs.append(data)

    async def close(self) -> None:
        self.closed = True


@dataclass
class _FakeResponse:
    status_code: int
    json_body: dict[str, Any] | None


class _RecordingTransport:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        timeout_seconds: float,
    ) -> _FakeResponse:
        self.calls.append({"method": method, "url": url})
        return self._response


def _ws_settings(**overrides: Any) -> KisWsReadOnlySettings:
    base: dict[str, Any] = {
        "enabled": True,
        "environment": "prod",
        "approval_base_url": "https://example.invalid",
        "websocket_url": "ws://example.invalid:21000",
        "app_key_env": "KIS_LIVE_APP_KEY",
        "app_secret_env": "KIS_LIVE_APP_SECRET",
        "connect_timeout_seconds": 10.0,
        "receive_timeout_seconds": 30.0,
        "max_subscriptions": 4,
        "confirmation_env_var": "KIS_WS_READONLY_CONFIRM",
        "confirmation_phrase": "ENABLE_KIS_WS_READONLY",
    }
    base.update(overrides)
    return KisWsReadOnlySettings(**base)


def _read_json_output(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


# ---------------------------------------------------------------------------
# validate-only (default): no network, no credentials, no fs writes.
# ---------------------------------------------------------------------------


def test_default_mode_is_validate_only_no_network(capsys: pytest.CaptureFixture[str]) -> None:
    rc = smoke.main(["--json"])
    assert rc == 0
    summary = _read_json_output(capsys)
    assert summary["outcome"] == "PASS"
    assert summary["mode"] == "validate-only"
    assert summary["network_called"] is False
    assert summary["http_called"] is False
    assert summary["orders_called"] is False


def test_validate_only_lists_subscriptions(capsys: pytest.CaptureFixture[str]) -> None:
    rc = smoke.main(["--validate-only", "--json", "--symbol", "005930"])
    assert rc == 0
    summary = _read_json_output(capsys)
    pairs = {(s["tr_id"], s["symbol"]) for s in summary["subscriptions"]}
    assert pairs == {("H0STCNT0", "005930"), ("H0STASP0", "005930")}


# ---------------------------------------------------------------------------
# gate failures (fail-closed, no network).
# ---------------------------------------------------------------------------


def test_run_and_validate_only_are_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    rc = smoke.main(["--run", "--validate-only", "--json"])
    assert rc == 1
    summary = _read_json_output(capsys)
    assert summary["outcome"] == "FAIL"
    assert "mutually exclusive" in summary["reason"]


def test_run_rejected_when_config_not_enabled(capsys: pytest.CaptureFixture[str]) -> None:
    # default example config has kis_ws_read_only.enabled = false.
    rc = smoke.main(["--run", "--json", "--max-events", "1"])
    assert rc == 1
    summary = _read_json_output(capsys)
    assert summary["outcome"] == "FAIL"
    assert "enabled must be true" in summary["reason"]


def _write_enabled_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[trading]
mode = "paper"
allow_live_trading = false

[llm]
provider = "ollama"
model = "qwen3.6:35b-mlx"
host = "http://localhost:11434"

[broker]
adapter = "paper"

[broker.kis_ws_read_only]
enabled = true
""".lstrip(),
        encoding="utf-8",
    )
    return cfg


def test_run_rejected_without_confirmation_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _write_enabled_config(tmp_path)
    monkeypatch.delenv("KIS_WS_READONLY_CONFIRM", raising=False)
    rc = smoke.main(["--run", "--json", "--config", str(cfg), "--max-events", "1"])
    assert rc == 1
    summary = _read_json_output(capsys)
    assert summary["outcome"] == "FAIL"
    assert "KIS_WS_READONLY_CONFIRM" in summary["reason"]


def test_run_rejected_when_unbounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _write_enabled_config(tmp_path)
    monkeypatch.setenv("KIS_WS_READONLY_CONFIRM", "ENABLE_KIS_WS_READONLY")
    rc = smoke.main(["--run", "--json", "--config", str(cfg)])
    assert rc == 1
    summary = _read_json_output(capsys)
    assert summary["outcome"] == "FAIL"
    assert "requires a bound" in summary["reason"]


# ---------------------------------------------------------------------------
# pure helpers.
# ---------------------------------------------------------------------------


def test_build_subscriptions_two_trs_per_symbol() -> None:
    subs = smoke.build_subscriptions(["005930", "000660"], max_subscriptions=4)
    assert len(subs) == 4
    assert {(s.tr_id, s.symbol) for s in subs} == {
        ("H0STCNT0", "005930"),
        ("H0STASP0", "005930"),
        ("H0STCNT0", "000660"),
        ("H0STASP0", "000660"),
    }


def test_build_subscriptions_enforces_cap() -> None:
    with pytest.raises(smoke.SmokeInputError, match="exceeds max_subscriptions"):
        smoke.build_subscriptions(["005930", "000660", "035720"], max_subscriptions=4)


def test_validate_bounds_requires_a_bound() -> None:
    with pytest.raises(smoke.SmokeInputError, match="requires a bound"):
        smoke._validate_bounds(None, None)
    smoke._validate_bounds(1, None)  # ok
    smoke._validate_bounds(None, 1.0)  # ok


def test_validate_bounds_rejects_non_positive() -> None:
    with pytest.raises(smoke.SmokeInputError):
        smoke._validate_bounds(0, None)
    with pytest.raises(smoke.SmokeInputError):
        smoke._validate_bounds(None, 0.0)


def test_confirmation_ok() -> None:
    ws = _ws_settings()
    assert smoke._confirmation_ok(ws, {"KIS_WS_READONLY_CONFIRM": "ENABLE_KIS_WS_READONLY"})
    assert not smoke._confirmation_ok(ws, {})
    assert not smoke._confirmation_ok(ws, {"KIS_WS_READONLY_CONFIRM": "wrong"})


def test_validate_evidence_path_rejects_outside_runtime() -> None:
    with pytest.raises(smoke.SmokeInputError, match="under runtime/"):
        smoke._validate_evidence_path(Path("/tmp/evidence.jsonl"))


def test_validate_evidence_path_accepts_under_runtime() -> None:
    resolved = smoke._validate_evidence_path(Path("runtime/ws/evidence.jsonl"))
    assert resolved is not None
    assert (Path.cwd() / "runtime").resolve() in resolved.parents


# ---------------------------------------------------------------------------
# execute_run wiring: bounded run through MarketMonitor with fakes (no network).
# ---------------------------------------------------------------------------


def test_execute_run_drives_bounded_run_with_fakes(tmp_path: Path) -> None:
    ws = _FakeWebSocket([_trade_frame(prpr="70000"), _trade_frame(prpr="70100")])

    async def connect() -> _FakeWebSocket:
        return ws

    transport = _RecordingTransport(_FakeResponse(200, {"approval_key": "APV-XYZ"}))
    evidence_out = tmp_path / "runtime" / "ws-evidence.jsonl"

    result = smoke.execute_run(
        ws_settings=_ws_settings(),
        environ={"KIS_LIVE_APP_KEY": "APPKEY", "KIS_LIVE_APP_SECRET": "APPSECRET"},
        subscriptions=[KisWsSubscription(tr_id="H0STCNT0", symbol="005930")],
        max_events=2,
        duration_seconds=None,
        connect=connect,
        approval_transport=transport,
        clock=lambda: _NOW,
        evidence_out=evidence_out,
    )

    assert result["outcome"] == "PASS"
    assert result["applied"] == 2
    assert result["orders_called"] is False
    assert result["network_called"] is True
    # transport-health evidence is recorded separately from market-data health.
    assert result["transport_health"]["connected"] == 1
    assert result["transport_health"]["subscription_sent"] >= 1
    # approval request was issued exactly once via the injected transport.
    assert len(transport.calls) == 1
    # evidence file written under runtime/, JSONL, no raw frame fields.
    lines = evidence_out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2
    for line in lines:
        record = json.loads(line)
        assert "raw" not in record
        assert "frame" not in record


def test_bad_subscription_tr_id_rejected_at_construction() -> None:
    with pytest.raises(KisWsSubscriptionError):
        KisWsSubscription(tr_id="BADTR", symbol="005930")
