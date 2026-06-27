"""Guard the readiness-checker NOT_READY troubleshooting doc.

`docs/PAPER_DAY_READINESS_TROUBLESHOOTING.md` lets a human Operator interpret a
`NOT_READY` / nonzero exit from `ops/check_next_paper_day_readiness.py` before a
live command, secret-safely and offline. These guards lock its offline/read-only
framing, that it covers every hard check name the checker can emit, the env-var
secret prohibitions, the do-not-delete-blindly guidance, and the do-not-bypass
rule — and that it is linked from the current status, the next packet, and the
dry-run rehearsal — so a future edit cannot weaken it into something that leaks
secrets or encourages bypassing the checker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_READINESS_TROUBLESHOOTING.md"

# Every check name the readiness checker can place in hard_failures.
CHECK_NAMES = (
    "repo_head_readable",
    "git_status_clean",
    "runtime_untracked",
    "config_exists",
    "config_untracked",
    "env:KIS_LIVE_APP_KEY",
    "env:KIS_LIVE_APP_SECRET",
    "env:KIS_LIVE_ACCOUNT",
    "env:KIS_WS_READONLY_CONFIRM",
    "session_date_valid",
    "run_label_valid",
    "duration_valid",
    "run_dir_matches",
    "run_dir_no_stale_artifacts",
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "readiness troubleshooting doc must exist"
    return DOC.read_text(encoding="utf-8")


def test_troubleshooting_doc_exists(doc_text: str) -> None:
    assert doc_text.strip(), "readiness troubleshooting doc must not be empty"


def test_framed_offline_read_only_before_live(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "offline" in lowered
    assert "read-only" in lowered
    assert "network-free" in lowered
    # Must say it runs before any live command.
    assert "before any live command" in lowered
    # Must name the checker it troubleshoots.
    assert "ops/check_next_paper_day_readiness.py" in doc_text


def test_lists_all_check_names(doc_text: str) -> None:
    for name in CHECK_NAMES:
        assert name in doc_text, f"troubleshooting doc must cover check {name}"


def test_env_secret_prohibitions_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "do not paste" in lowered
    assert "do not print" in lowered
    # Only metadata may be inspected.
    assert "present" in lowered
    assert "length" in lowered
    assert "strip_same" in lowered
    assert "placeholder" in lowered
    # Fix in the Operator shell.
    assert "operator shell" in lowered


def test_do_not_delete_blindly_and_run_dir_guidance(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "do not delete blindly" in lowered or "do not delete files blindly" in lowered
    assert "do not commit runtime" in lowered
    # Stale RUN_DIR -> pick a fresh one.
    assert "fresh run_dir" in lowered or "fresh `run_dir`" in lowered
    # Tracked runtime -> stop and review.
    assert "stop and review" in lowered or "stop" in lowered and "review" in lowered


def test_do_not_bypass_rule_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "do not bypass" in lowered
    # Nonzero exit means do not run the live command.
    assert "nonzero" in lowered
    assert "do not run the live command" in lowered
    # Resolve or classify as intentional NO_GO.
    assert "no_go" in lowered


def test_does_not_encourage_live_or_bypass(doc_text: str) -> None:
    lowered = doc_text.lower()
    # Must not tell the Operator to run live KIS or skip/disable the checker.
    assert "does not" in lowered and "bypass" in lowered


# --- cross-doc link guards -------------------------------------------------

CURRENT_STATUS = REPO_ROOT / "docs" / "PAPER_DAY_CURRENT_STATUS.md"
NEXT_PACKET = REPO_ROOT / "docs" / "PAPER_DAY_NEXT_OPERATOR_PACKET.md"
DRY_RUN = REPO_ROOT / "docs" / "PAPER_DAY_OPERATOR_DRY_RUN_REHEARSAL.md"

TROUBLESHOOTING_REF = "docs/PAPER_DAY_READINESS_TROUBLESHOOTING.md"


def test_linked_from_current_status_packet_and_rehearsal() -> None:
    for path in (CURRENT_STATUS, NEXT_PACKET, DRY_RUN):
        assert path.is_file(), f"{path.name} must exist"
        assert TROUBLESHOOTING_REF in path.read_text(encoding="utf-8"), (
            f"{path.name} must link the readiness troubleshooting doc"
        )
