from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from paper_review.report import build_paper_review_report
from paper_review.store import PaperReviewReportStore
from paper_review_fixtures import sample_review_input


def test_store_save_get_roundtrip(tmp_path: Path) -> None:
    store = PaperReviewReportStore(tmp_path / "reviews.jsonl")
    review_input = sample_review_input(review_id="review-store-1")
    report = build_paper_review_report(review_input)
    store.save(report)

    loaded = store.get("review-store-1")
    assert loaded is not None
    assert loaded.to_canonical_dict() == report.to_canonical_dict()


def test_duplicate_review_id_reject(tmp_path: Path) -> None:
    store = PaperReviewReportStore(tmp_path / "reviews.jsonl")
    review_input = sample_review_input(review_id="review-dup")
    report = build_paper_review_report(review_input)
    store.save(report)

    with pytest.raises(ValueError, match="duplicate review_id"):
        store.save(report)


def test_missing_file_returns_empty_iterator(tmp_path: Path) -> None:
    store = PaperReviewReportStore(tmp_path / "missing.jsonl")
    assert store.list_reports() == ()
    assert store.get("missing") is None


def test_corrupted_jsonl_row_raises_path_and_line(tmp_path: Path) -> None:
    path = tmp_path / "reviews.jsonl"
    path.write_text("{not-json}\n", encoding="utf-8")
    store = PaperReviewReportStore(path)

    with pytest.raises(ValueError, match=r"invalid JSONL row at line 1"):
        tuple(store.iter_reports())
