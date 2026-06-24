from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "ops"))

from validate_paper_day_summary import (  # noqa: E402
    EXPECTED_SCHEMA_VERSION,
    FAIL,
    NEEDS_REVIEW,
    NO_GO,
    PASS,
    ValidatorError,
    classify,
    main,
    run_validate,
)


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
                "subscription_acks": 2,
                "quote_subscription_acks": 1,
                "quote_frames": 1,
                "all_subscribed": 1,
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
        "cleanup_outcome": "CLEAN",
        "runtime_lock_fd_closed": True,
        "runtime_lock_absent_confirmed": True,
        "runtime_lock_release_reason_code": None,
    }
    base.update(overrides)
    return base


def _evidence_lines(*rows: dict[str, object]) -> str:
    default = [
        {"event": "started", "stage": "startup", "sensitive_data_present": False},
        {"event": "finalized", "stage": "shutdown", "sensitive_data_present": False},
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
        envelope_path = tmp_path / "envelope.json"
        envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    return summary_path, evidence_path, envelope_path


def _run(tmp_path, summary, evidence, envelope=None, **kw):
    summary_path, evidence_path, envelope_path = _write(tmp_path, summary, evidence, envelope)
    return run_validate(
        summary_path=summary_path,
        evidence_path=evidence_path,
        envelope_path=envelope_path,
        expect_schema_version=kw.get("expect_schema_version", EXPECTED_SCHEMA_VERSION),
        expect_source_kind=kw.get("expect_source_kind"),
    )


def test_pass_with_separate_envelope(tmp_path: Path) -> None:
    result = _run(tmp_path, _persisted_summary(), _evidence_lines(), _envelope())
    assert result["verdict"] == PASS
    assert result["pass_blockers"] == []
    assert result["hard_fail"] == []
    assert result["missing_from_persisted_summary"] == []


def test_pass_with_combined_summary(tmp_path: Path) -> None:
    combined = {**_persisted_summary(), **_envelope()}
    result = _run(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == PASS


def test_zero_order_day_is_pass(tmp_path: Path) -> None:
    combined = {**_persisted_summary(stop_reason="completed", outcome="PASS"), **_envelope()}
    combined["counters"]["counters"]["orders"] = 0  # type: ignore[index]
    combined["counters"]["counters"]["fills"] = 0  # type: ignore[index]
    result = _run(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == PASS
    assert result["observations"]["counters"]["orders"] == 0


def test_missing_envelope_fields_needs_review(tmp_path: Path) -> None:
    result = _run(tmp_path, _persisted_summary(), _evidence_lines())
    assert result["verdict"] == NEEDS_REVIEW
    assert "missing_from_persisted_summary" in result["pass_blockers"]
    assert set(result["missing_from_persisted_summary"]) == {
        "summary_publication_outcome",
        "cleanup_outcome",
        "runtime_lock_fd_closed",
        "runtime_lock_absent_confirmed",
        "runtime_lock_release_reason_code",
    }


def test_no_go_outcome(tmp_path: Path) -> None:
    combined = {
        **_persisted_summary(outcome="NO_GO", stop_reason="health_not_ready"),
        **_envelope(),
    }
    result = _run(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == NO_GO


def test_fail_outcome(tmp_path: Path) -> None:
    combined = {
        **_persisted_summary(outcome="FAIL", stop_reason="source_approval_failed"),
        **_envelope(summary_publication_outcome="NOT_WRITTEN"),
    }
    result = _run(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == FAIL


def test_sensitive_evidence_is_hard_fail(tmp_path: Path) -> None:
    combined = {**_persisted_summary(), **_envelope()}
    leak = _evidence_lines(
        {"event": "started", "stage": "startup", "sensitive_data_present": False},
        {"event": "frame", "stage": "transport", "sensitive_data_present": True},
    )
    result = _run(tmp_path, combined, leak)
    assert result["verdict"] == FAIL
    assert "sensitive_data_present" in result["hard_fail"]


def test_paper_only_false_is_hard_fail(tmp_path: Path) -> None:
    combined = {**_persisted_summary(paper_only=False), **_envelope()}
    result = _run(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == FAIL
    assert "paper_only" in result["hard_fail"]


def test_activation_authorized_true_is_hard_fail(tmp_path: Path) -> None:
    combined = {**_persisted_summary(activation_authorized=True), **_envelope()}
    result = _run(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == FAIL
    assert "activation_authorized" in result["hard_fail"]


def test_schema_mismatch_blocks_pass(tmp_path: Path) -> None:
    combined = {**_persisted_summary(schema_version="paper_day_diagnostic.v2"), **_envelope()}
    result = _run(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == NEEDS_REVIEW
    assert "schema_version_mismatch" in result["pass_blockers"]


def test_source_kind_mismatch_blocks_pass(tmp_path: Path) -> None:
    combined = {**_persisted_summary(source_kind="replay"), **_envelope()}
    result = _run(tmp_path, combined, _evidence_lines(), expect_source_kind="kis_live")
    assert result["verdict"] == NEEDS_REVIEW
    assert "source_kind_mismatch" in result["pass_blockers"]


def test_source_kind_match_passes(tmp_path: Path) -> None:
    combined = {**_persisted_summary(), **_envelope()}
    result = _run(tmp_path, combined, _evidence_lines(), expect_source_kind="kis_live")
    assert result["verdict"] == PASS


def test_nonterminal_journal_blocks_pass(tmp_path: Path) -> None:
    combined = {**_persisted_summary(nonterminal_journal=2), **_envelope()}
    result = _run(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == NEEDS_REVIEW
    assert "nonterminal_journal" in result["pass_blockers"]


def test_lock_release_reason_blocks_pass(tmp_path: Path) -> None:
    combined = {
        **_persisted_summary(),
        **_envelope(runtime_lock_release_reason_code="runtime_lock_release_uncertain"),
    }
    result = _run(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == NEEDS_REVIEW
    assert "runtime_lock_release_reason_code" in result["pass_blockers"]


def test_cleanup_not_clean_blocks_pass(tmp_path: Path) -> None:
    combined = {**_persisted_summary(), **_envelope(cleanup_outcome="INCOMPLETE")}
    result = _run(tmp_path, combined, _evidence_lines())
    assert result["verdict"] == NEEDS_REVIEW
    assert "cleanup_outcome" in result["pass_blockers"]


def test_missing_summary_file_needs_review(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.jsonl"
    evidence_path.write_text(_evidence_lines(), encoding="utf-8")
    result = run_validate(
        summary_path=tmp_path / "absent.json",
        evidence_path=evidence_path,
        envelope_path=None,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind=None,
    )
    assert result["verdict"] == NEEDS_REVIEW
    assert "summary_missing" in result["pass_blockers"]


def test_missing_evidence_blocks_pass(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps({**_persisted_summary(), **_envelope()}), encoding="utf-8")
    result = run_validate(
        summary_path=summary_path,
        evidence_path=tmp_path / "absent.jsonl",
        envelope_path=None,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind=None,
    )
    assert result["verdict"] == NEEDS_REVIEW
    assert "evidence_missing" in result["pass_blockers"]


def test_first_failure_surfaced(tmp_path: Path) -> None:
    combined = {
        **_persisted_summary(outcome="FAIL", stop_reason="source_connect_failed"),
        **_envelope(summary_publication_outcome="NOT_WRITTEN"),
    }
    evidence = _evidence_lines(
        {"event": "started", "stage": "startup", "sensitive_data_present": False},
        {
            "event": "failed_closed",
            "stage": "runtime",
            "reason_code": "source_connect_failed",
            "recorded_at": "2026-06-22T09:30:05+09:00",
            "sensitive_data_present": False,
        },
    )
    result = _run(tmp_path, combined, evidence)
    assert result["verdict"] == FAIL
    ff = result["observations"]["first_failure"]
    assert ff["reason_code"] == "source_connect_failed"
    assert ff["stage"] == "runtime"


def test_session_quote_readiness_and_source_subcode_are_surfaced(tmp_path: Path) -> None:
    combined = {
        **_persisted_summary(outcome="NO_GO", stop_reason="invalid_session_window"),
        **_envelope(),
    }
    evidence = _evidence_lines(
        {
            "event": "heartbeat",
            "stage": "heartbeat",
            "reason_code": None,
            "recorded_at": "2026-06-24T15:42:00+09:00",
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
            "recorded_at": "2026-06-24T15:42:01+09:00",
            "sensitive_data_present": False,
            "snapshot": {
                "reason_subcode": "post_startup_source_iterator_error",
            },
        },
    )
    result = _run(tmp_path, combined, evidence)

    assert result["verdict"] == NO_GO
    assert result["observations"]["latest_session"]["session_state"] == "POST_CLOSE"
    quote = result["observations"]["latest_quote_readiness"]
    assert quote["market_data_health"] == "NOT_EXPECTED"
    assert quote["quote_subscription_ready"] is False
    assert quote["quote_frames"] == 0
    assert quote["normalized_quotes"] == 0
    assert result["observations"]["source_drop_subcodes"] == {
        "post_startup_source_iterator_error": 1
    }


def test_validator_does_not_surface_raw_source_error_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
                "reason_subcode": "post_startup_source_iterator_error",
                "raw_url": "wss://credentialed.example.invalid/socket?appsecret=LEAK",
                "traceback": "Traceback (most recent call last): secret frame",
                "raw_websocket_frame": "0|H0STASP0|005930|SECRET",
            },
        },
    )
    summary_path, evidence_path, _ = _write(tmp_path, combined, evidence)

    code = main(["--summary", str(summary_path), "--evidence", str(evidence_path)])
    out = capsys.readouterr().out

    assert code == 1
    assert "post_startup_source_iterator_error" in out
    for forbidden in (
        "credentialed.example.invalid",
        "appsecret",
        "Traceback",
        "raw_websocket_frame",
        "SECRET",
    ):
        assert forbidden not in out


def test_malformed_evidence_row_blocks_pass(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    evidence_path = tmp_path / "evidence.jsonl"
    summary_path.write_text(json.dumps({**_persisted_summary(), **_envelope()}), encoding="utf-8")
    evidence_path.write_text(
        json.dumps({"event": "started", "sensitive_data_present": False}) + "\nnot json\n",
        encoding="utf-8",
    )
    result = run_validate(
        summary_path=summary_path,
        evidence_path=evidence_path,
        envelope_path=None,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind=None,
    )
    assert result["verdict"] == NEEDS_REVIEW
    assert "evidence_malformed" in result["pass_blockers"]


def test_bad_envelope_path_raises(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.json"
    evidence_path = tmp_path / "evidence.jsonl"
    summary_path.write_text(json.dumps(_persisted_summary()), encoding="utf-8")
    evidence_path.write_text(_evidence_lines(), encoding="utf-8")
    bad_envelope = tmp_path / "envelope.json"
    bad_envelope.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ValidatorError):
        run_validate(
            summary_path=summary_path,
            evidence_path=evidence_path,
            envelope_path=bad_envelope,
            expect_schema_version=EXPECTED_SCHEMA_VERSION,
            expect_source_kind=None,
        )


def test_validator_does_not_mutate_files(tmp_path: Path) -> None:
    summary_path, evidence_path, _ = _write(
        tmp_path, {**_persisted_summary(), **_envelope()}, _evidence_lines()
    )
    before = (summary_path.read_bytes(), evidence_path.read_bytes())
    run_validate(
        summary_path=summary_path,
        evidence_path=evidence_path,
        envelope_path=None,
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind=None,
    )
    after = (summary_path.read_bytes(), evidence_path.read_bytes())
    assert before == after
    assert sorted(p.name for p in tmp_path.iterdir()) == ["evidence.jsonl", "summary.json"]


def test_main_exit_zero_on_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == PASS


def test_main_exit_one_on_needs_review(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    summary_path, evidence_path, _ = _write(tmp_path, _persisted_summary(), _evidence_lines())
    code = main(["--summary", str(summary_path), "--evidence", str(evidence_path), "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == NEEDS_REVIEW


def test_main_malformed_envelope_needs_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary_path, evidence_path, _ = _write(
        tmp_path, _persisted_summary(), _evidence_lines()
    )
    bad_envelope = tmp_path / "envelope.json"
    bad_envelope.write_text("{ broken", encoding="utf-8")
    code = main(
        [
            "--summary",
            str(summary_path),
            "--evidence",
            str(evidence_path),
            "--envelope",
            str(bad_envelope),
            "--json",
        ]
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == NEEDS_REVIEW
    assert "envelope_malformed" in payload["error"]


def test_main_empty_envelope_needs_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary_path, evidence_path, _ = _write(
        tmp_path, _persisted_summary(), _evidence_lines()
    )
    empty_envelope = tmp_path / "envelope.json"
    empty_envelope.write_text("", encoding="utf-8")
    code = main(
        [
            "--summary",
            str(summary_path),
            "--evidence",
            str(evidence_path),
            "--envelope",
            str(empty_envelope),
            "--json",
        ]
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == NEEDS_REVIEW


def test_classify_is_pure_no_io() -> None:
    result = classify(
        summary={**_persisted_summary(), **_envelope()},
        summary_error=None,
        envelope=None,
        evidence={
            "evidence_error": None,
            "rows": 2,
            "malformed_rows": 0,
            "sensitive_rows": 0,
            "first_failure": None,
        },
        expect_schema_version=EXPECTED_SCHEMA_VERSION,
        expect_source_kind="kis_live",
    )
    assert result["verdict"] == PASS
