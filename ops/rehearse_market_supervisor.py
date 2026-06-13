#!/usr/bin/env python3
"""RTM-7b — offline market supervisor rehearsal CLI (network-free).

fixture JSON scenario + fake clock + explicit schedule + scripted monitor로
MarketSupervisor 전체 일을 결정론적으로 재생한다. KIS credential/socket/DNS/
broker/ledger/execution을 호출하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from market_data.health_policy import MarketHealthTracker, provisional_thresholds
from market_data.market_session import (
    SessionWindow,
    build_explicit_schedule,
)
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
    """supervisor factory가 만드는 결정론적 monitor — 즉시 STOPPED 반환."""

    _instances: list["_ScriptedMonitor"] = []

    def __init__(self) -> None:
        self.state = MonitorState.IDLE
        self.started = False
        _ScriptedMonitor._instances.append(self)

    async def run(self) -> MonitorSummary:
        self.started = True
        self.state = MonitorState.RUNNING
        await asyncio.sleep(0)
        self.state = MonitorState.STOPPED
        from market_data.monitor import MonitorSummary

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
class _RehearsalState:
    clock_at: datetime
    actions: list[str] = field(default_factory=list)
    starvation_detected: bool = False
    flapping_detected: bool = False
    restart_budget_exhausted: bool = False


def _validate_evidence_path(path: Path) -> None:
    resolved = path.resolve()
    runtime = (Path.cwd() / "runtime").resolve()
    if not str(resolved).startswith(str(runtime)):
        raise ValueError("evidence path must be under runtime/")


async def run_rehearsal(
    scenario: dict[str, Any],
    *,
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    """scenario를 실행하고 JSON summary dict를 반환한다."""
    _ScriptedMonitor._instances.clear()
    schedule = _build_schedule(scenario["schedule"])
    steps: list[dict[str, Any]] = scenario["steps"]
    if not steps:
        raise ValueError("steps must be non-empty.")

    thresholds = provisional_thresholds()
    policy = provisional_supervisor_policy()
    if "thresholds" in scenario:
        from market_data.health_policy import HealthThresholds

        thr = scenario["thresholds"]
        thresholds = HealthThresholds(**thr)
    if "policy" in scenario:
        policy = SupervisorPolicy(**scenario["policy"])

    state = _RehearsalState(clock_at=_parse_datetime(steps[0]["clock"]))
    tick_idx = {"i": 0}

    tracker = MarketHealthTracker(thresholds)

    def clock() -> datetime:
        idx = min(tick_idx["i"], len(steps) - 1)
        step = steps[idx]
        at = _parse_datetime(step["clock"])
        state.clock_at = at
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
        state.actions.append(ev.action)
        if ev.action == str(SupervisorAction.HOLD_EXECUTION_ONLY):
            state.starvation_detected = True
        if "flapping" in (ev.reason_code or ""):
            state.flapping_detected = True
        if ev.action == str(SupervisorAction.FAILED_CLOSED):
            state.restart_budget_exhausted = True

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
        "session_transitions": len(steps),
        "transport_health_transitions": len([a for a in state.actions if "health" in str(a)]),
        "market_health_transitions": state.starvation_detected,
        "supervisor_actions": state.actions,
        "starvation_detected": state.starvation_detected,
        "flapping_detected": state.flapping_detected,
        "restart_budget_exhausted": state.restart_budget_exhausted,
        "pending_tasks": len(pending),
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
    parser.add_argument("--fixture", required=True, type=Path, help="JSON scenario fixture")
    parser.add_argument(
        "--evidence-out",
        type=Path,
        default=None,
        help="optional evidence JSONL path (must be under runtime/)",
    )
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
