"""Guard the next-session Operator packet (the current reusable run sheet).

`docs/PAPER_DAY_NEXT_OPERATOR_PACKET.md` is the current reusable run sheet for the
next regular KR market session. Unlike the historical 2026-06-22 Monday packet, it
must carry no baked-in active date, must require Operator-selected run variables,
must keep the stdout-envelope capture pattern and post-run existence gate, must
scope any future run to envelope/runbook validation only, and must keep the safety
prohibitions intact. These guards lock those properties so a future edit cannot
quietly turn it back into a date-frozen or unsafe sheet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_NEXT_OPERATOR_PACKET.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "next-session Operator packet must exist"
    return DOC.read_text(encoding="utf-8")


def test_next_packet_exists(doc_text: str) -> None:
    assert doc_text.strip(), "next-session packet must not be empty"
    assert "CURRENT next-session packet" in doc_text


def test_requires_operator_selected_run_variables(doc_text: str) -> None:
    for var in ("SESSION_DATE", "RUN_LABEL", "DURATION_SECONDS", "RUN_DIR", "HEAD"):
        assert var in doc_text, f"packet must require Operator-selected {var}"
    assert "Operator-selected" in doc_text
    # Run command must consume the Operator-selected variables, not a frozen date.
    assert '--session-date "$SESSION_DATE"' in doc_text
    assert '--duration-seconds "$DURATION_SECONDS"' in doc_text
    assert 'RUN_DIR="runtime/paper-day/$SESSION_DATE/$RUN_LABEL"' in doc_text
    # HEAD must be the current reviewed commit.
    assert "current reviewed" in doc_text


def test_historical_dates_not_used_as_active_defaults(doc_text: str) -> None:
    # The historical Monday packet/checklist dates must not appear as active
    # defaults (no baked-in date). They may only be referenced as historical.
    assert '--session-date 2026-06-22' not in doc_text
    assert 'runtime/paper-day/2026-06-22/' not in doc_text
    assert 'runtime/paper-day/2026-06-23/' not in doc_text
    assert 'SESSION_DATE="2026-06-22"' not in doc_text
    assert 'SESSION_DATE="2026-06-23"' not in doc_text
    # The packet must point at itself as the current sheet superseding the historical ones.
    assert "supersedes the historical" in doc_text


def test_envelope_capture_pattern_present(doc_text: str) -> None:
    assert '--stdout-envelope-out "$RUN_DIR/stdout-envelope.json"' in doc_text
    assert '--json > "$RUN_DIR/stdout-envelope.shell.json"' in doc_text
    # The validator-read file comes from the flag, not the redirect.
    assert "produced by the flag, not by the shell redirect" in doc_text
    assert "belt-and-suspenders console capture" in doc_text


def test_post_run_existence_gate_present(doc_text: str) -> None:
    assert 'test -f "$RUN_DIR/summary.json"' in doc_text
    assert 'test -f "$RUN_DIR/evidence.jsonl"' in doc_text
    assert 'test -f "$RUN_DIR/stdout-envelope.json"' in doc_text
    assert 'test -d "$RUN_DIR/db"' in doc_text
    assert 'test -z "$(git status --short)"' in doc_text
    assert 'test -z "$(git ls-files runtime)"' in doc_text


def test_offline_validator_and_report_use_envelope(doc_text: str) -> None:
    assert "ops/validate_paper_day_summary.py" in doc_text
    assert "ops/render_paper_day_report.py" in doc_text
    assert '--envelope "$RUN_DIR/stdout-envelope.json"' in doc_text


def test_readiness_checker_documented_before_run_command(doc_text: str) -> None:
    # The offline readiness checker must be referenced, and must appear before the
    # live run command so the Operator runs it first.
    checker_ref = "ops/check_next_paper_day_readiness.py"
    run_ref = "ops/run_attended_paper_day.py"
    assert checker_ref in doc_text
    assert run_ref in doc_text
    assert doc_text.index(checker_ref) < doc_text.index(run_ref)
    # It must be framed as offline/read-only and remind about live session state.
    assert "offline" in doc_text.lower()
    assert "session_state=OPEN must still be confirmed" in doc_text
    # It must pass the Operator-selected variables, not a frozen date.
    assert '--session-date "$SESSION_DATE"' in doc_text
    assert '--run-dir "$RUN_DIR"' in doc_text


def test_wrong_run_envelope_guard_documented(doc_text: str) -> None:
    # The packet must warn that the envelope has to come from the same run, name
    # the validator's identity cross-check, and the envelope_run_mismatch verdict.
    assert "envelope_run_mismatch" in doc_text
    assert "_envelope_capture.run_id" in doc_text
    # The four cross-checked identity fields must be named.
    for field in ("run_id", "session_date", "symbol"):
        assert field in doc_text
    # Operator must be told not to copy/reuse an envelope from another run.
    lowered = doc_text.lower()
    assert "reuse" in lowered
    assert "another run" in lowered
    # A wrong-run envelope can never be PASS.
    assert "can never be PASS" in doc_text or "cannot be PASS" in doc_text


def test_envelope_runbook_validation_only(doc_text: str) -> None:
    # Any future run is for envelope/runbook validation only — not parser verification.
    assert "envelope/runbook validation only" in doc_text
    assert "not for parser\nverification" in doc_text or "not for parser verification" in doc_text


def test_rehearsal_mention_includes_fixture_path_location(doc_text: str) -> None:
    # If the packet references the offline rehearsal, it must point at where the
    # rehearsal fixtures live (a directory path under tests/fixtures/...), and
    # frame it as offline/synthetic/not-a-live-KIS run.
    if "rehearsal" in doc_text.lower():
        assert "tests/fixtures/paper_day_reports/" in doc_text
        assert "not a live KIS run" in doc_text
        assert "network-free" in doc_text


def test_safety_prohibitions_present(doc_text: str) -> None:
    assert "no live orders" in doc_text
    assert "no activation" in doc_text
    assert "no daemon" in doc_text
    assert "no automatic restart" in doc_text
    assert "Operator-only live KIS" in doc_text
    # No raw frame/payload/URL/token/key/account/traceback logging.
    assert "no raw frame" in doc_text
    assert "traceback" in doc_text
