from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from emergency.models import EmergencyTriggerStatus
from paper_review.metrics import count_emergency_triggers
from paper_review.report import build_paper_review_report, render_paper_review_markdown
from postmortem.aggregation import top_error_tags_from_summaries
from paper_review_fixtures import (
    sample_emergency_event,
    sample_mdd_emergency_event,
    sample_review_input,
)
from postmortem_fixtures import sample_postmortem_record


def test_postmortem_tag_summaries_aggregate_via_phase13() -> None:
    record = sample_postmortem_record()
    tags = top_error_tags_from_summaries((record.tag_summary,))
    assert len(tags) <= 3
    assert all(tag.startswith("#") for tag in tags)


def test_empty_postmortem_input_allowed_and_report_builds() -> None:
    review_input = sample_review_input(postmortem_records=())
    report = build_paper_review_report(review_input)
    assert report.postmortem_top_error_tags == ()


def test_emergency_trigger_counts_deterministic() -> None:
    events = (
        sample_emergency_event(event_id="e1"),
        sample_mdd_emergency_event(event_id="m1"),
        sample_emergency_event(
            event_id="e2",
            payload_overrides={"status": EmergencyTriggerStatus.SUPPRESSED_BY_COOLDOWN},
        ),
    )
    counts = count_emergency_triggers(events)
    assert counts["STOCK_DROP"] == 2
    assert counts["MDD_KILLSWITCH"] == 1
    assert counts["cooldown_suppressed"] == 1
    assert list(counts.keys()) == sorted(counts.keys())


def test_report_canonical_dict_deterministic() -> None:
    review_input = sample_review_input(review_id="review-canonical")
    report_a = build_paper_review_report(review_input)
    report_b = build_paper_review_report(review_input)
    assert report_a.to_canonical_dict() == report_b.to_canonical_dict()


def test_payload_hash_deterministic() -> None:
    review_input = sample_review_input(review_id="review-hash")
    report = build_paper_review_report(review_input)
    assert report.payload_hash() == report.payload_hash()
    assert len(report.payload_hash()) == 64


def test_markdown_renderer_includes_human_approval_and_auto_apply_false() -> None:
    review_input = sample_review_input(review_id="review-md")
    report = build_paper_review_report(review_input)
    markdown = render_paper_review_markdown(report)

    assert "auto_apply=false" in markdown
    assert "human approval required" in markdown
    assert "No config changes applied" in markdown
    assert "No LLM call made" in markdown


def test_no_config_writer_scheduler_llm_broker() -> None:
    import paper_review.report as report_module

    source_path = report_module.__file__
    assert source_path is not None
    with open(source_path, encoding="utf-8") as handle:
        content = handle.read()
    forbidden = [
        "paper_broker",
        "kis_live",
        "submit_tiny_live_order",
        "place_tiny_live_order",
        "ollama",
        "scheduler",
        "config.toml",
    ]
    for token in forbidden:
        assert token not in content
