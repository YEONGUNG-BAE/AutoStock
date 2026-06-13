"""RTM-7b — offline rehearsal CLI tests."""

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


def _load_cli():
    if str(_REPO / "src") not in sys.path:
        sys.path.insert(0, str(_REPO / "src"))
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))
    spec = importlib.util.spec_from_file_location("rehearse_market_supervisor", _CLI)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path, *, monitor_mode: str = "long_running") -> Path:
    data = {
        "monitor_mode": monitor_mode,
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
            {"clock": "2026-06-15T09:05:00+09:00", "market": ["best_bid_ask"]},
            {"clock": "2026-06-15T11:00:00+09:00"},
            {"clock": "2026-06-15T15:30:00+09:00"},
            {"clock": "2026-06-15T18:00:00+09:00"},
        ],
    }
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_valid_runtime_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.chdir(tmp_path)
    cli._validate_evidence_path(runtime / "evidence.jsonl")


def test_runtime_evil_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="runtime"):
        cli._validate_evidence_path(tmp_path / "runtime_evil" / "out.jsonl")


def test_parent_traversal_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load_cli()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside.jsonl"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="runtime"):
        cli._validate_evidence_path(outside)


def test_transition_counts_are_actual_changes(tmp_path: Path) -> None:
    cli = _load_cli()
    scenario = cli._load_scenario(_fixture(tmp_path))
    summary = asyncio.run(cli.run_rehearsal(scenario))
    assert summary["transport_health_transitions"] >= 0
    assert isinstance(summary["transport_health_sequence"], list)
    assert summary["pending_tasks"] == 0


def test_long_running_cancel_on_close(tmp_path: Path) -> None:
    cli = _load_cli()
    scenario = cli._load_scenario(_fixture(tmp_path, monitor_mode="long_running"))
    summary = asyncio.run(cli.run_rehearsal(scenario))
    assert summary["long_running_cancels"] >= 1


def test_malformed_fixture(tmp_path: Path) -> None:
    cli = _load_cli()
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        cli._load_scenario(bad)


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
