"""RTM-7c.12 — guard the Monday Operator packet's envelope-capture wording.

The clean-exit clauses live only in the stdout envelope. The packet must keep the
distinction straight: ``stdout-envelope.json`` is produced by the
``--stdout-envelope-out`` tool flag (the file the validator reads), while
``stdout-envelope.shell.json`` is only a belt-and-suspenders console capture from
the shell redirect. A regression to "the redirect captures that envelope to
stdout-envelope.json" would mislead the Operator, so we guard it here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_MONDAY_OPERATOR_PACKET.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file()
    return DOC.read_text(encoding="utf-8")


def test_run_command_uses_tool_flag_and_shell_redirect(doc_text: str) -> None:
    assert '--stdout-envelope-out "$RUN_DIR/stdout-envelope.json"' in doc_text
    assert '--json > "$RUN_DIR/stdout-envelope.shell.json"' in doc_text


def test_envelope_capture_wording_attributes_file_to_flag_not_redirect(doc_text: str) -> None:
    # The validator-read file comes from the flag, not the redirect.
    assert "produced by the flag, not by the shell redirect" in doc_text
    # The shell redirect is described as a belt-and-suspenders console capture.
    assert "belt-and-suspenders console capture" in doc_text
    # The stale claim must not reappear.
    assert "redirect above captures\nthat envelope to `stdout-envelope.json`" not in doc_text
    assert "redirect above captures that envelope to `stdout-envelope.json`" not in doc_text


def test_post_run_existence_gate_present(doc_text: str) -> None:
    assert 'test -f "$RUN_DIR/stdout-envelope.json"' in doc_text
    assert 'test -f "$RUN_DIR/summary.json"' in doc_text
    assert 'test -f "$RUN_DIR/evidence.jsonl"' in doc_text
