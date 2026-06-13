#!/usr/bin/env python3
"""RTM-7b — offline market supervisor rehearsal CLI (network-free)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from market_data.health_policy import (
    MarketHealthTracker,
    MarketDataHealthStatus,
    TransportHealthStatus,
    provisional_thresholds,
)
from market_data.market_session import SessionWindow, build_explicit_schedule
from market_data.monitor import MonitorState, MonitorSummary
from market_data.supervisor import (
    MarketSupervisor,
    SupervisorAction,
    SupervisorEvidence,
    SupervisorPolicy,
    SupervisorState,
    provisional_supervisor_policy,
)

from domain.enums import Market

_MAX_STEPS = 500


def _parse_time(value: str) -> time:
    parts = value.split(":")
    if len(parts) < 2:
        raise ValueError(f"invalid time: {value}")
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) > 2 else 0
    return time(h, m, s)


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be tz-aware: {value}")
    return dt


def _load_scenario(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"malformed fixture: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("fixture root must be an object.")
    for key in ("schedule", "steps"):
        if key not in data:
            raise ValueError(f"fixture missing required key: {key}")
    steps = data["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list.")
    if len(steps) > _MAX_STEPS:
        raise ValueError(f"steps exceed max {_MAX_STEPS}.")
    return data


def _build_schedule(schedule: dict[str, Any]) -> object:
    tz = ZoneInfo(schedule["timezone"])
    days = [date.fromisoformat(d) for d in schedule["trading_days"]]
    window = SessionWindow(
        pre_open=_parse_time(schedule["pre_open"]),
        open=_parse_time(schedule["open"]),
        close=_parse_time(schedule["close"]),
        post_close_end=_parse_time(schedule["post_close_end"]),
    )
    return build_explicit_schedule(timezone=tz, trading_days=days, window=window)


class _ScriptedMonitor:
    """fixture-driven monitor — long-running 또는 즉시 종료."""

    _instances: list["_ScriptedMonitor"] = []
    _mode: str = "long_running"

    def __init__(self) -> None:
        self.state = MonitorState.IDLE
        self.cancelled = False
        self.cleanup_done = False
        _ScriptedMonitor._instances.append(self)

    async def run(self) -> MonitorSummary:
        self.state = MonitorState.RUNNING
        if _ScriptedMonitor._mode == "instant_exit":
            self.state = MonitorState.STOPPED
            return _monitor_summary()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            self.cleanup_done = True
            self.state = MonitorState.STOPPED
            raise
        return _monitor_summary()


def _monitor_summary() -> MonitorSummary:
    return MonitorSummary(
        monitor_session_id="rehearsal",
        connection_attempts=1,
        consecutive_failures=0,
        applied=0,
        duplicate=0,
        out_of_order=0,
        stream_mismatch=0,
        future_event_error=0,
        final_state=MonitorState.STOPPED,
    )


@dataclass
class _TransitionTracker:
    transport_sequence: list[str] = field(default_factory=list)
    market_sequence: list[str] = field(default_factory=list)
    action_sequence: list[str] = field(default_factory=list)
    session_sequence: list[str] = field(default_factory=list)
    transport_transitions: int = 0
    market_transitions: int = 0
    action_transitions: int = 0
    session_transitions: int = 0
    _prev_transport: str | None = None
    _prev_market: str | None = None
    _prev_action: str | None = None
    _prev_session: str | None = None

    def observe(self, ev: SupervisorEvidence) -> None:
        if self._prev_transport is not None and ev.transport != self._prev_transport:
            self.transport_transitions += 1
        if self._prev_market is not None and ev.market_data != self._prev_market:
            self.market_transitions += 1
        if self._prev_action is not None and ev.action != self._prev_action:
            self.action_transitions += 1
        if self._prev_session is not None and ev.session_state != self._prev_session:
            self.session_transitions += 1
        self.transport_sequence.append(ev.transport)
        self.market_sequence.append(ev.market_data)
        self.action_sequence.append(ev.action)
        self.session_sequence.append(ev.session_state)
        self._prev_transport = ev.transport
        self._prev_market = ev.market_data
        self._prev_action = ev.action
        self._prev_session = ev.session_state


def _validate_evidence_path(path: Path) -> None:
    runtime_root = (Path.cwd() / "runtime").resolve()
    candidate = path.resolve()
    try:
        ok = candidate.is_relative_to(runtime_root)
    except AttributeError:
        ok = str(candidate).startswith(str(runtime_root) + "/") or candidate == runtime_root
    if not ok:
        raise ValueError("evidence path must be under runtime/")


async def run_rehearsal(
    scenario: dict[str, Any],
    *,
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    _ScriptedMonitor._instances.clear()
    mode = scenario.get("monitor_mode", "long_running")
    if mode not in ("long_running", "instant_exit"):
        raise ValueError("monitor_mode must be long_running or instant_exit.")
    _ScriptedMonitor._mode = mode

    schedule = _build_schedule(scenario["schedule"])
    steps: list[dict[str, Any]] = scenario["steps"]

    thresholds = provisional_thresholds()
    policy = provisional_supervisor_policy()
    if "thresholds" in scenario:
        from market_data.health_policy import HealthThresholds

        thresholds = HealthThresholds(**scenario["thresholds"])
    if "policy" in scenario:
        policy = SupervisorPolicy(**scenario["policy"])

    tick_idx = {"i": 0}
    tracker = MarketHealthTracker(thresholds)
    transitions = _TransitionTracker()
    starvation_detected = False
    flapping_detected = False

    def clock() -> datetime:
        idx = min(tick_idx["i"], len(steps) - 1)
        step = steps[idx]
        at = _parse_datetime(step["clock"])
        for sig in step.get("transport", []):
            tracker.record_transport_event(kind=sig, at=at, now=at)
        for sig in step.get("market", []):
            tracker.record_market_event(event_type=sig, at=at, now=at)
        return at

    async def tick_sleep(_seconds: float) -> None:
        tick_idx["i"] += 1
        await asyncio.sleep(0)

    evidence_records: list[dict[str, Any]] = []

    def on_evidence(ev: SupervisorEvidence) -> None:
        record = asdict(ev)
        record["timestamp"] = ev.timestamp.isoformat()
        record["state"] = ev.state.value
        evidence_records.append(record)
        transitions.observe(ev)
        if ev.action == str(SupervisorAction.HOLD_EXECUTION_ONLY):
            nonlocal starvation_detected
            starvation_detected = True
        if ev.transport == str(TransportHealthStatus.FLAPPING):
            nonlocal flapping_detected
            flapping_detected = True

    sup = MarketSupervisor(
        market=Market.KR,
        calendar=schedule,  # type: ignore[arg-type]
        monitor_factory=_ScriptedMonitor,
        tracker=tracker,
        clock=clock,
        sleep=tick_sleep,
        policy=policy,
        max_ticks=len(steps),
        on_evidence=on_evidence,
    )

    summary = await sup.run()
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    result = {
        "outcome": "pass" if summary.final_state is not SupervisorState.FAILED_CLOSED else "failed_closed",
        "final_state": summary.final_state.value,
        "monitor_initial_starts": summary.monitor_initial_starts,
        "monitor_restarts": summary.monitor_restarts,
        "monitor_cancels": summary.monitor_cancels,
        "restarts_in_current_window": summary.restarts_in_current_window,
        "transport_health_transitions": transitions.transport_transitions,
        "market_health_transitions": transitions.market_transitions,
        "supervisor_action_transitions": transitions.action_transitions,
        "session_transitions": transitions.session_transitions,
        "transport_health_sequence": transitions.transport_sequence,
        "market_health_sequence": transitions.market_sequence,
        "supervisor_action_sequence": transitions.action_sequence,
        "starvation_detected": starvation_detected,
        "flapping_detected": flapping_detected,
        "restart_budget_exhausted": summary.final_state is SupervisorState.FAILED_CLOSED,
        "pending_tasks": len(pending),
        "long_running_cancels": sum(1 for m in _ScriptedMonitor._instances if m.cancelled),
    }

    if evidence_path is not None:
        _validate_evidence_path(evidence_path)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        with evidence_path.open("w", encoding="utf-8") as handle:
            for rec in evidence_records:
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RTM-7b offline market supervisor rehearsal")
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--evidence-out", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        scenario = _load_scenario(args.fixture)
        summary = asyncio.run(run_rehearsal(scenario, evidence_path=args.evidence_out))
    except ValueError as exc:
        print(json.dumps({"outcome": "error", "reason": str(exc)}), file=sys.stderr)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
