from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "ops"))

from render_paper_day_report import build_report, main  # noqa: E402
from validate_paper_day_summary import EXPECTED_SCHEMA_VERSION  # noqa: E402


def _persisted_summary(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": EXPECTED_SCHEMA_VERSION,
        "run_id": "paper-day-005930-260622",
        "session_date": "2026-06-22",
        "market": "KR",
        "symbol": "005930",
        "paper_only": True,
        "activation_authorized": False,
        "real_order_adapter_constructed": False,
        "automatic_restart": False,
        "multi_symbol": False,
        "outcome": "PASS",
        "stop_reason": "startup_only",
        "nonterminal_journal": 0,
        "counters": {
            "counters": {
                "normalized_trades": 1,
                "normalized_quotes": 1,
                "connect_attempts": 1,
                "connected": 1,
                "subscription_requests": 2,
                "subscription_acks": 2,
                "quote_subscription_acks": 1,
                "quote_frames": 1,
                "all_subscribed": 1,
                "disconnects": 1,
            },
            "reason_counts": {"startup_only": 1},
            "timestamps": {"resource_close_completed_at": "2026-06-22T09:31:00+09:00"},
        },
        "source_kind": "kis_live",
    }
    base.update(overrides)
    return base


def _envelope(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "summary_publication_outcome": "WRITTEN",
        "summary_publication_reason_codes": [],
        "cleanup_outcome": "CLEAN",
        "runtime_lock_fd_closed": True,
        "runtime_lock_absent_confirmed": True,
        "runtime_lock_identity_matched": True,
        "runtime_lock_release_reason_code": None,
    }
    base.update(overrides)
    return base


def _evidence_lines(*rows: dict[str, object]) -> str:
    default = [
        {
            "event": "started",
            "stage": "startup",
            "recorded_at": "2026-06-22T09:30:00+09:00",
            "sensitive_data_present": False,
        },
        {
            "event": "finalized",
            "stage": "shutdown",
            "recorded_at": "2026-06-22T09:31:00+09:00",
            "sensitive_data_present": False,
        },
    ]
    use = list(rows) if rows else default
    return "".join(json.dumps(r) + "\n" for r in use)


def _write(tmp_path: Path, summary: object, evidence: str, envelope: object | None = None):
    summary_path = tmp_path / "summary.json"
    evidence_path = tmp_path / "evidence.jsonl"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    evidence_path.write_text(evidence, encoding="utf-8")
    envelope_path = None
    if envelope is not None:
        envelope_path = tmp_path / "stdout-envelope.json"
        envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    return summary_path, evidence_path, envelope_path


def _build(tmp_path, summary, evidence, envelope=None, **kw):
    summary_path, evidence_path, envelope_path = _write(tmp_path, summary, evidence, envelope)
    return build_report(
        summary_path=summary_path,
        evidence_path=evidence_path,
        envelope_path=envelope_path,
        expect_schema_version=kw.get("expect_schema_version", EXPECTED_SCHEMA_VERSION),
        expect_source_kind=kw.get("expect_source_kind"),
        max_timeline_rows=kw.get("max_timeline_rows", 200),
    )


def test_pass_report_has_clean_clauses(tmp_path: Path) -> None:
    md, result = _build(tmp_path, _persisted_summary(), _evidence_lines(), _envelope())
    assert result["verdict"] == "PASS"
    assert "# Paper Day Diagnostic Review Report" in md
    assert "**verdict: PASS**" in md
    assert "## Clean-exit clauses" in md
    assert "summary_publication_outcome | WRITTEN | ok" in md
    assert "cleanup_outcome | CLEAN | ok" in md
    assert "## Source readiness" in md
    assert "## Reviewer checklist" in md
    # All required sections present.
    for section in (
        "## Run identity",
        "## Verdict",
        "## Session timing",
        "## Paper-only safety proof",
        "## Evidence timeline",
        "## First failure",
        "## Counters",
        "## Orders and fills",
        "## Journal/completion state",
        "## Publication and lock state",
        "## Operator git/runtime hygiene",
        "## Remaining NO-GO items",
    ):
        assert section in md


def test_persisted_only_says_needs_review(tmp_path: Path) -> None:
    md, result = _build(tmp_path, _persisted_summary(), _evidence_lines())
    assert result["verdict"] == "NEEDS_REVIEW"
    assert "**verdict: NEEDS_REVIEW**" in md
    assert "missing_from_persisted_summary" in md
    assert "envelope: not provided" in md


def test_fail_source_approval_first_failure(tmp_path: Path) -> None:
    combined = {
        **_persisted_summary(outcome="FAIL", stop_reason="source_approval_failed"),
        **_envelope(summary_publication_outcome="NOT_WRITTEN"),
    }
    evidence = _evidence_lines(
        {
            "event": "started",
            "stage": "startup",
            "recorded_at": "2026-06-22T09:30:00+09:00",
            "sensitive_data_present": False,
        },
        {
            "event": "failed_closed",
            "stage": "runtime",
            "reason_code": "source_approval_failed",
            "recorded_at": "2026-06-22T09:30:02+09:00",
            "sensitive_data_present": False,
        },
    )
    md, result = _build(tmp_path, combined, evidence)
    assert result["verdict"] == "FAIL"
    assert "## First failure" in md
    assert "reason_code: source_approval_failed" in md
    assert "stage: runtime" in md


def test_no_go_health_not_ready(tmp_path: Path) -> None:
    combined = {
        **_persisted_summary(outcome="NO_GO", stop_reason="health_not_ready"),
        **_envelope(),
    }
    md, result = _build(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == "NO_GO"
    assert "runtime outcome: NO_GO" in md
    assert "stop_reason: health_not_ready" in md


def test_post_close_invalid_timing_is_surfaced(tmp_path: Path) -> None:
    combined = {
        **_persisted_summary(outcome="NO_GO", stop_reason="invalid_session_window"),
        **_envelope(),
    }
    evidence = _evidence_lines(
        {
            "event": "session_window_check",
            "stage": "preflight",
            "reason_code": "invalid_session_window",
            "recorded_at": "2026-06-24T15:42:00+09:00",
            "sensitive_data_present": False,
            "snapshot": {
                "session_state": "POST_CLOSE",
                "required_session_state": "OPEN",
                "calendar_reason": None,
            },
        },
        {
            "event": "failed_closed",
            "stage": "preflight",
            "reason_code": "invalid_session_window",
            "recorded_at": "2026-06-24T15:42:00+09:00",
            "sensitive_data_present": False,
        },
        {
            "event": "heartbeat",
            "stage": "heartbeat",
            "reason_code": None,
            "recorded_at": "2026-06-24T15:42:01+09:00",
            "sensitive_data_present": False,
            "snapshot": {
                "session_state": "POST_CLOSE",
                "market_data_health": "NOT_EXPECTED",
                "quote_subscription_ready": False,
                "quote_frames": 0,
                "normalized_quotes": 0,
            },
        },
        {
            "event": "drop",
            "stage": "market_data",
            "reason_code": "source_error",
            "recorded_at": "2026-06-24T15:42:02+09:00",
            "sensitive_data_present": False,
            "snapshot": {
                "reason_subcode": "websocket_closed_after_ack",
            },
        },
    )
    md, result = _build(tmp_path, combined, evidence)
    assert result["verdict"] == "NO_GO"
    assert "## Session timing" in md
    assert "session_state | POST_CLOSE" in md
    assert "required_session_state | OPEN" in md
    assert "**invalid timing:** session_state=POST_CLOSE" in md
    assert "market_data_health | NOT_EXPECTED" in md
    assert "quote_subscription_ready | false" in md
    assert "quote_frames | 0" in md
    assert "normalized_quotes | 0" in md
    assert "websocket_closed_after_ack: 1" in md
    assert "stop_reason: invalid_session_window" in md


def test_report_does_not_surface_raw_source_error_details(tmp_path: Path) -> None:
    combined = {
        **_persisted_summary(outcome="NO_GO", stop_reason="health_not_ready"),
        **_envelope(),
    }
    evidence = _evidence_lines(
        {
            "event": "drop",
            "stage": "market_data",
            "reason_code": "source_error",
            "recorded_at": "2026-06-24T15:42:02+09:00",
            "sensitive_data_present": False,
            "snapshot": {
                "reason_subcode": "websocket_closed_after_ack",
                "raw_url": "wss://credentialed.example.invalid/socket?appsecret=LEAK",
                "traceback": "Traceback (most recent call last): secret frame",
                "raw_websocket_frame": "0|H0STASP0|005930|SECRET",
            },
        },
    )
    md, result = _build(tmp_path, combined, evidence)

    assert result["verdict"] == "NO_GO"
    assert "websocket_closed_after_ack: 1" in md
    for forbidden in (
        "credentialed.example.invalid",
        "appsecret",
        "Traceback",
        "raw_websocket_frame",
        "SECRET",
    ):
        assert forbidden not in md


def test_sensitive_data_is_hard_fail(tmp_path: Path) -> None:
    combined = {**_persisted_summary(), **_envelope()}
    leak = _evidence_lines(
        {
            "event": "started",
            "stage": "startup",
            "recorded_at": "2026-06-22T09:30:00+09:00",
            "sensitive_data_present": False,
        },
        {
            "event": "frame",
            "stage": "transport",
            "recorded_at": "2026-06-22T09:30:01+09:00",
            "sensitive_data_present": True,
        },
    )
    md, result = _build(tmp_path, combined, leak)
    assert result["verdict"] == "FAIL"
    assert "sensitive_data" in md
    assert "sensitive_data_present_any | true" in md
    assert "hard_fail: sensitive_data_present" in md


def test_missing_orders_fills_does_not_crash(tmp_path: Path) -> None:
    combined = {**_persisted_summary(stop_reason="completed"), **_envelope()}
    # No orders/fills counters present at all.
    md, result = _build(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == "PASS"
    assert "orders: missing" in md
    assert "fills: missing" in md
    assert "`orders > 0` is **not** required for PASS." in md


def test_zero_orders_is_pass(tmp_path: Path) -> None:
    summary = _persisted_summary(stop_reason="completed")
    summary["counters"]["counters"]["orders"] = 0  # type: ignore[index]
    summary["counters"]["counters"]["fills"] = 0  # type: ignore[index]
    combined = {**summary, **_envelope()}
    md, result = _build(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == "PASS"
    assert "orders: 0" in md


def test_malformed_evidence_blocks_pass(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    evidence_path = tmp_path / "evidence.jsonl"
    summary_path.write_text(json.dumps({**_persisted_summary(), **_envelope()}), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(
            {"event": "started", "recorded_at": "t", "sensitive_data_present": False}
        )
        + "\nnot json\n",
        encoding="utf-8",
    )
    md, result = build_report(
        summary_path=summary_path,
        evidence_path=evidence_path,
        envelope_path=None,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind=None,
        max_timeline_rows=200,
    )
    assert result["verdict"] == "NEEDS_REVIEW"
    assert "evidence_malformed" in result["pass_blockers"]
    assert "malformed evidence rows: 1" in md


def test_malformed_envelope_treated_as_absent(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    evidence_path = tmp_path / "evidence.jsonl"
    envelope_path = tmp_path / "stdout-envelope.json"
    summary_path.write_text(json.dumps(_persisted_summary()), encoding="utf-8")
    evidence_path.write_text(_evidence_lines(), encoding="utf-8")
    envelope_path.write_text("{ broken", encoding="utf-8")
    md, result = build_report(
        summary_path=summary_path,
        evidence_path=evidence_path,
        envelope_path=envelope_path,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind=None,
        max_timeline_rows=200,
    )
    assert result["verdict"] == "NEEDS_REVIEW"
    assert "envelope_error: envelope_malformed" in md


def test_out_file_written(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    summary_path, evidence_path, envelope_path = _write(
        tmp_path, _persisted_summary(), _evidence_lines(), _envelope()
    )
    out_path = tmp_path / "report" / "review-report.md"
    code = main(
        [
            "--summary",
            str(summary_path),
            "--evidence",
            str(evidence_path),
            "--envelope",
            str(envelope_path),
            "--out",
            str(out_path),
        ]
    )
    assert code == 0
    assert out_path.is_file()
    content = out_path.read_text(encoding="utf-8")
    assert content.startswith("# Paper Day Diagnostic Review Report")
    assert "verdict: PASS" in capsys.readouterr().out


def test_stdout_mode_prints_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    summary_path, evidence_path, envelope_path = _write(
        tmp_path, _persisted_summary(), _evidence_lines(), _envelope()
    )
    code = main(
        [
            "--summary",
            str(summary_path),
            "--evidence",
            str(evidence_path),
            "--envelope",
            str(envelope_path),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "# Paper Day Diagnostic Review Report" in out
    assert "**verdict: PASS**" in out


def test_stdout_mode_nonzero_exit_when_not_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary_path, evidence_path, _ = _write(tmp_path, _persisted_summary(), _evidence_lines())
    code = main(["--summary", str(summary_path), "--evidence", str(evidence_path)])
    assert code == 1


def test_does_not_mutate_inputs(tmp_path: Path) -> None:
    summary_path, evidence_path, envelope_path = _write(
        tmp_path, _persisted_summary(), _evidence_lines(), _envelope()
    )
    before = {
        p.name: p.read_bytes() for p in (summary_path, evidence_path, envelope_path)
    }
    build_report(
        summary_path=summary_path,
        evidence_path=evidence_path,
        envelope_path=envelope_path,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind=None,
        max_timeline_rows=200,
    )
    after = {p.name: p.read_bytes() for p in (summary_path, evidence_path, envelope_path)}
    assert before == after
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "evidence.jsonl",
        "stdout-envelope.json",
        "summary.json",
    ]


def test_source_kind_mismatch_blocks_pass(tmp_path: Path) -> None:
    combined = {**_persisted_summary(source_kind="replay"), **_envelope()}
    md, result = _build(
        tmp_path, combined, _evidence_lines(), expect_source_kind="kis_live"
    )
    assert result["verdict"] == "NEEDS_REVIEW"
    assert "source_kind_mismatch" in result["pass_blockers"]


def test_timeline_truncation(tmp_path: Path) -> None:
    rows = [
        {
            "event": "tick",
            "stage": "monitor",
            "recorded_at": f"2026-06-22T09:30:{i:02d}+09:00",
            "sensitive_data_present": False,
        }
        for i in range(5)
    ]
    combined = {**_persisted_summary(), **_envelope()}
    md, result = _build(
        tmp_path, combined, _evidence_lines(*rows), _envelope(), max_timeline_rows=2
    )
    assert "showing 2 of 5 rows" in md
