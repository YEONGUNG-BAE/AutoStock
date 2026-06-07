#!/usr/bin/env python3
"""RTM-3 fake-transport market monitor CLI (bounded replay).

JSONL fixture frame들을 RTM-1 parser로 정규화한 뒤 RTM-3 MarketMonitor로 재생한다.
network/broker/ledger/trigger/LLM/order 경로를 호출하지 않으며, 실제 KIS WebSocket
연결도 하지 않는다(RTM-6 유보). ReplayMarketEventSource는 유한하므로 EOF로 항상
종료된다 — endless production daemon이 아니다.

clock은 fixture event들의 최신 received_at으로 고정한다(wall-clock 의존 제거,
미래 이벤트 오탐 방지). evidence는 --evidence-out 지정 시에만 JSONL로 append하며,
--validate-only는 파일을 쓰지 않고 summary만 stdout으로 낸다(RTM-1/RTM-2 패턴 일치).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from market_data.kis_ws_parser import KisWsFrameParser
from market_data.latest_state import LatestMarketStateStore
from market_data.models import MarketEvent
from market_data.monitor import MarketMonitor, MonitorEvidence, MonitorSummary
from market_data.replay_source import ReplayMarketEventSource


def load_events(fixture: Path) -> list[MarketEvent]:
    parser = KisWsFrameParser()
    events: list[MarketEvent] = []
    for raw_line in fixture.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        events.append(parser.parse(json.loads(line)))
    return events


def _resolve_now(events: list[MarketEvent]) -> datetime:
    times = [event.received_at for event in events]
    return max(times) if times else datetime.now(UTC)


def _evidence_record(evidence: MonitorEvidence) -> dict[str, object | None]:
    record = dataclasses.asdict(evidence)
    record["timestamp"] = evidence.timestamp.isoformat()
    record["state"] = evidence.state.value
    return record


def _summary_record(summary: MonitorSummary) -> dict[str, object]:
    record = dataclasses.asdict(summary)
    record["final_state"] = summary.final_state.value
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RTM-3 bounded fake-transport market monitor")
    parser.add_argument("--fixture", required=True, type=Path, help="JSONL frame fixture path")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="parse+replay and print summary only; never writes evidence files",
    )
    parser.add_argument(
        "--evidence-out",
        type=Path,
        default=None,
        help="append-only evidence JSONL path (ignored under --validate-only)",
    )
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--session-id", default="cli-replay")
    args = parser.parse_args(argv)

    events = load_events(args.fixture)
    now = _resolve_now(events)
    store = LatestMarketStateStore()

    out_handle: TextIO | None = None
    sink = None
    if not args.validate_only and args.evidence_out is not None:
        out_handle = args.evidence_out.open("a", encoding="utf-8")

        def sink(evidence: MonitorEvidence) -> None:
            out_handle.write(json.dumps(_evidence_record(evidence), ensure_ascii=False) + "\n")

    monitor = MarketMonitor(
        store=store,
        source_factory=lambda: ReplayMarketEventSource(events),
        clock=lambda: now,
        session_id=args.session_id,
        max_events=args.max_events,
        on_evidence=sink,
    )
    try:
        summary = asyncio.run(monitor.run())
    finally:
        if out_handle is not None:
            out_handle.close()

    json.dump(_summary_record(summary), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
