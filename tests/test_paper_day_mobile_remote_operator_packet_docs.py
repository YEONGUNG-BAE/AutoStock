"""RTM-7c.12 — guard the mobile remote Operator packet against drift."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_MOBILE_REMOTE_OPERATOR_PACKET.md"

_HOT_PATH_FILES = (
    "src/composition/attended_paper_day.py",
    "ops/run_attended_paper_day.py",
    "src/data/kis_ws_source.py",
    "src/data/kis_ws_auth.py",
    "src/broker/kis_transport.py",
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file()
    return DOC.read_text(encoding="utf-8")


def test_has_three_operator_variables(doc_text: str) -> None:
    assert 'SESSION_DATE="2026-06-23"' in doc_text
    assert 'RUN_LABEL="day-1"' in doc_text
    assert 'DURATION_SECONDS="1800"' in doc_text


def test_has_exactly_four_paste_block_headings(doc_text: str) -> None:
    headings = re.findall(r"^## Paste block [1-4] — .+$", doc_text, re.MULTILINE)
    assert len(headings) == 4
    assert [heading.split()[3] for heading in headings] == ["1", "2", "3", "4"]


def test_has_shell_safe_run_and_exit_capture(doc_text: str) -> None:
    assert '--json > "$RUN_DIR/stdout-envelope.json"' in doc_text
    assert "PILOT_EXIT=$?" in doc_text
    assert "Do not pipe through tee." in doc_text
    assert "Do not use PIPESTATUS." in doc_text


def test_has_validation_report_and_handoff_commands(doc_text: str) -> None:
    assert "ops/validate_paper_day_summary.py" in doc_text
    assert "ops/render_paper_day_report.py" in doc_text
    assert 'echo "HEAD=$(git rev-parse HEAD)"' in doc_text
    assert 'find "$RUN_DIR" -maxdepth 2 -type f | sort' in doc_text


@pytest.mark.parametrize(
    "prohibition",
    (
        "unattended",
        "all-day",
        "background job",
        "nohup",
        "daemon",
        "tmux detach",
        "screen detach",
    ),
)
def test_prohibits_unattended_or_detached_operation(
    doc_text: str, prohibition: str
) -> None:
    assert prohibition in doc_text


def test_protects_remote_access_and_secrets(doc_text: str) -> None:
    assert "approved remote-control method" in doc_text
    assert "Do not bypass workplace policy." in doc_text
    assert "Do not save secrets in phone notes." in doc_text
    assert "Do not save KIS secrets in phone notes." in doc_text


def test_disconnect_and_sensitive_data_fail_closed(doc_text: str) -> None:
    assert "remote session disconnects during the run" in doc_text
    assert "treat as NEEDS_REVIEW until artifacts validated" in doc_text
    assert "sensitive_data_present=true" in doc_text
    assert "stop/escalate, do not paste artifact contents" in doc_text


@pytest.mark.parametrize("path", _HOT_PATH_FILES)
def test_mentions_frozen_runtime_hot_path(doc_text: str, path: str) -> None:
    assert path in doc_text
