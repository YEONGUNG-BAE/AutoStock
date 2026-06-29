"""Guard the Paper-Day internal-runtime-error classification-gap note."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "PAPER_DAY_INTERNAL_RUNTIME_ERROR_CLASSIFICATION.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "classification-gap doc must exist"
    return DOC.read_text(encoding="utf-8")


def test_doc_exists_and_is_non_empty(doc_text: str) -> None:
    assert doc_text.strip()


def test_doc_preserves_formal_fail_not_pass_conversion(doc_text: str) -> None:
    normalized = " ".join(doc_text.split())
    assert "formal verdict remains FAIL" in doc_text
    assert "not a PASS conversion" in doc_text
    assert "Do not retroactively convert this run to PASS" in normalized
    assert "historical run remains" in doc_text
    assert "`internal_runtime_error`" in doc_text


def test_doc_distinguishes_parser_and_safety_failures(doc_text: str) -> None:
    assert "not parser failure" in doc_text
    assert "not safety-failure evidence" in doc_text
    assert "This case is not parser failure evidence" in doc_text
    assert "This case is safety-clean" in doc_text


def test_doc_names_failure_path_and_events(doc_text: str) -> None:
    for token in (
        "MonitorExhaustedError",
        "internal_runtime_error",
        "source_exhausted_after_reconnects",
        "malformed_control_after_ack",
        "exhausted",
        "failed_closed",
        "finalized",
    ):
        assert token in doc_text


def test_doc_describes_normalized_regression_coverage(doc_text: str) -> None:
    normalized = " ".join(doc_text.split())
    assert "fake/sanitized regression coverage" in doc_text
    assert "outcome=FAIL" in doc_text
    assert "terminal reason is normalized to `source_exhausted_after_reconnects`" in normalized
    assert "Generic unexpected monitor exceptions are still classified as" in doc_text
    assert "Reconnect behavior" in doc_text
    assert "live order behavior are unchanged" in doc_text


def test_doc_includes_safety_invariants(doc_text: str) -> None:
    for invariant in (
        "paper_only=true",
        "activation_authorized=false",
        "real_order_adapter_constructed=false",
        "automatic_restart=false",
        "nonterminal_journal=0",
        "cleanup_outcome=CLEAN",
        "summary_publication_outcome=WRITTEN",
    ):
        assert invariant in doc_text


def test_doc_includes_quote_trade_normalization_facts(doc_text: str) -> None:
    for fact in (
        "quote_frames == normalized_quotes",
        "trade_frames == normalized_trades",
        "quote_frames + trade_frames == parse_success",
    ):
        assert fact in doc_text


def test_doc_keeps_no_immediate_rerun_and_no_full_paper_guidance(doc_text: str) -> None:
    assert "No immediate live rerun required" in doc_text
    assert "Do not proceed to full paper solely from this FAIL" in doc_text


def test_doc_does_not_include_raw_or_secret_examples(doc_text: str) -> None:
    lowered = doc_text.lower()
    forbidden_example_patterns = (
        "wss://",
        "ws://",
        "https://",
        "http://",
        "token=",
        "account=",
        "app_key=",
        "appkey",
        "approval_key=",
        "raw frame:",
        "raw payload:",
    )
    for pattern in forbidden_example_patterns:
        assert pattern not in lowered
