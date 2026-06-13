"""RTM-7b — offline rehearsal CLI tests (network-free)."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_CLI = _REPO / "ops" / "rehearse_market_supervisor.py"


def _load_cli_module():
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    if str(_REPO / "src") not in sys.path:
        sys.path.insert(0, str(_REPO / "src"))
    spec = importlib.util.spec_from_file_location("rehearse_market_supervisor", _CLI)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path) -> Path:
    data = {
        "schedule": {
            "timezone": "Asia/Seoul",
            "trading_days": ["2026-06-15"],
            "pre_open": "08:30:00",
            "open": "09:00:00",
            "close": "15:30:00",
            "post_close_end": "16:00:00",
        },
        "steps": [
            {"clock": "2026-06-15T08:50:00+09:00"},
            {
                "clock": "2026-06-15T09:00:00+09:00",
                "transport": ["connected", "all_subscribed", "pong_sent"],
            },
            {
                "clock": "2026-06-15T09:05:00+09:00",
                "market": ["best_bid_ask"],
            },
            {"clock": "2026-06-15T15:30:00+09:00"},
            {"clock": "2026-06-15T18:00:00+09:00"},
        ],
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_deterministic_replay(tmp_path: Path) -> None:
    cli = _load_cli_module()
    fixture = _fixture(tmp_path)
    scenario = cli._load_scenario(fixture)
    summary1 = asyncio.run(cli.run_rehearsal(scenario))
    summary2 = asyncio.run(cli.run_rehearsal(scenario))
    assert summary1["monitor_initial_starts"] == summary2["monitor_initial_starts"]
    assert summary1["pending_tasks"] == 0


def test_malformed_fixture(tmp_path: Path) -> None:
    cli = _load_cli_module()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        cli._load_scenario(bad)


def test_runtime_path_restriction(tmp_path: Path) -> None:
    cli = _load_cli_module()
    with pytest.raises(ValueError, match="runtime"):
        cli._validate_evidence_path(tmp_path / "evidence.jsonl")


def test_evidence_under_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli_module()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.chdir(tmp_path)
    fixture = _fixture(tmp_path)
    scenario = cli._load_scenario(fixture)
    evidence = runtime / "evidence.jsonl"
    summary = asyncio.run(cli.run_rehearsal(scenario, evidence_path=evidence))
    assert evidence.exists()
    assert summary["pending_tasks"] == 0


def test_cli_no_broker_imports() -> None:
    forbidden = {"broker", "ledger", "execution", "paper_loop"}
    tree = ast.parse(_CLI.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert forbidden.isdisjoint(roots)


def test_cli_no_network_on_import() -> None:
    cli = _load_cli_module()
    assert hasattr(cli, "run_rehearsal")
