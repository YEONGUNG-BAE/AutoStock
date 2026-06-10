"""RTM-3 ops CLI tests — bounded fake-transport monitor (no network/broker/ledger).

ReplayMarketEventSource는 유한하므로 항상 EOF로 종료된다. CLI는 fixture를 RTM-1
parser로 정규화한 뒤 RTM-3 MarketMonitor로 재생하고, --validate-only는 파일을 쓰지
않으며 --evidence-out 지정 시에만 JSONL을 append한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "market_data" / "monitor" / "replay_basic.jsonl"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from run_kis_market_monitor import load_events, main

_SUMMARY_KEYS = {
    "monitor_session_id",
    "connection_attempts",
    "consecutive_failures",
    "applied",
    "duplicate",
    "out_of_order",
    "stream_mismatch",
    "future_event_error",
    "final_state",
}


def _capture_summary(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    out = capsys.readouterr().out
    return json.loads(out)


def test_validate_only_prints_summary_and_writes_no_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence_out = tmp_path / "evidence.jsonl"
    rc = main(
        [
            "--fixture",
            str(FIXTURE),
            "--validate-only",
            "--evidence-out",
            str(evidence_out),
        ]
    )
    assert rc == 0
    summary = _capture_summary(capsys)
    assert set(summary) == _SUMMARY_KEYS
    assert summary["final_state"] == "stopped"
    assert summary["connection_attempts"] == 1
    # validate-only never touches the evidence path even when given one
    assert not evidence_out.exists()


def test_evidence_out_appends_jsonl(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence_out = tmp_path / "evidence.jsonl"
    rc = main(["--fixture", str(FIXTURE), "--evidence-out", str(evidence_out)])
    assert rc == 0
    capsys.readouterr()  # drain summary
    assert evidence_out.exists()
    lines = [json.loads(line) for line in evidence_out.read_text().splitlines() if line.strip()]
    assert lines, "expected at least one evidence record"
    for record in lines:
        assert "kind" in record
        assert "timestamp" in record
        assert "state" in record

    # second run appends rather than truncates
    main(["--fixture", str(FIXTURE), "--evidence-out", str(evidence_out)])
    capsys.readouterr()
    appended = [
        line for line in evidence_out.read_text().splitlines() if line.strip()
    ]
    assert len(appended) == 2 * len(lines)


def test_max_events_limits_applied(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--fixture", str(FIXTURE), "--validate-only", "--max-events", "1"])
    assert rc == 0
    summary = _capture_summary(capsys)
    # fixture leads with a trade; one consumed event stops the bounded run
    assert summary["applied"] == 1


def test_malformed_jsonl_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not valid json}\n", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_events(bad)
