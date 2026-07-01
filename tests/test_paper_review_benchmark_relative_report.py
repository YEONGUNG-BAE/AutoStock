from __future__ import annotations

import inspect
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from domain.portfolio import NavSnapshot
from paper_review.metrics import compute_benchmark_relative_metrics
from paper_review.models import BenchmarkRelativeMetrics, BenchmarkReturnPoint
from paper_review.report import render_benchmark_relative_metrics_markdown


BASE_TIME = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)


def _metrics(*, warnings: tuple[str, ...] = ()) -> BenchmarkRelativeMetrics:
    return BenchmarkRelativeMetrics(
        bot_total_return_percent=Decimal("5"),
        benchmark_total_return_percent=Decimal("2"),
        excess_return_percent=Decimal("3"),
        relative_drawdown_percent=Decimal("-1.25"),
        tracking_error_daily_percent=Decimal("0.50"),
        information_ratio_annualized=Decimal("1.75"),
        up_capture_percent=Decimal("125"),
        down_capture_percent=Decimal("81.81818181818181818181818182"),
        beta_to_benchmark=Decimal("0.90"),
        aligned_observation_count=3,
        return_observation_count=2,
        benchmark_observation_count=3,
        warnings=warnings,
    )


def _nav(day_offset: int, nav: str) -> NavSnapshot:
    total_nav = Decimal(nav)
    return NavSnapshot(
        snapshot_id=f"synthetic-nav-{day_offset}",
        as_of=BASE_TIME + timedelta(days=day_offset),
        total_nav_krw=total_nav,
        cash_krw=Decimal("0"),
        invested_krw=total_nav,
    )


def _benchmark(day_offset: int, value: str) -> BenchmarkReturnPoint:
    return BenchmarkReturnPoint(
        as_of=BASE_TIME + timedelta(days=day_offset),
        total_return_index_value=Decimal(value),
    )


def test_renders_core_fields() -> None:
    markdown = render_benchmark_relative_metrics_markdown(
        _metrics(),
        title="Synthetic benchmark review",
        benchmark_name="Synthetic S&P 500 total return (KRW-unhedged)",
    )

    assert markdown.startswith("# Synthetic benchmark review\n")
    assert "- Benchmark: Synthetic S&P 500 total return (KRW-unhedged)" in markdown
    assert "- Aligned observations: 3" in markdown
    assert "- Return observations: 2" in markdown
    assert "- Benchmark observations supplied: 3" in markdown
    assert "- Bot total return (%): 5" in markdown
    assert "- Benchmark total return (%): 2" in markdown
    assert "- Excess return (%): 3" in markdown
    assert "- Relative drawdown (%): -1.25" in markdown
    assert "- Tracking error (%): 0.50" in markdown
    assert "- Information ratio annualized: 1.75" in markdown
    assert "- Beta to benchmark: 0.90" in markdown
    assert "- Up-capture (%): 125" in markdown
    assert "- Down-capture (%): 81.81818181818181818181818182" in markdown


def test_renders_warnings_deterministically() -> None:
    markdown = render_benchmark_relative_metrics_markdown(
        _metrics(warnings=("no_benchmark_down_periods", "zero_benchmark_return_variance"))
    )

    first_index = markdown.index("- no_benchmark_down_periods")
    second_index = markdown.index("- zero_benchmark_return_variance")
    assert first_index < second_index


def test_renders_no_warning_state() -> None:
    markdown = render_benchmark_relative_metrics_markdown(_metrics())

    assert "## Warnings\n\n- None\n" in markdown


def test_includes_interpretation_notes() -> None:
    markdown = render_benchmark_relative_metrics_markdown(_metrics())
    lowered = markdown.lower()

    assert "positive excess return" in lowered
    assert "bot outperformed the benchmark" in lowered
    assert "negative relative drawdown" in lowered
    assert "underperformed from a prior relative peak" in lowered
    assert "down-capture above 100" in lowered
    assert "lost more than the benchmark" in lowered
    assert "not a historical backtest" in lowered
    assert "only as meaningful as the supplied nav and benchmark series" in lowered


def test_end_to_end_fixture_rehearsal_with_synthetic_krw_unhedged_sp500_tr() -> None:
    """Synthetic benchmark values are assumed to represent KRW-unhedged S&P 500 TR levels."""
    nav_snapshots = (
        _nav(0, "100"),
        _nav(1, "110"),
        _nav(2, "105"),
    )
    benchmark_points = (
        _benchmark(0, "100"),
        _benchmark(1, "104"),
        _benchmark(2, "102"),
    )

    metrics = compute_benchmark_relative_metrics(nav_snapshots, benchmark_points)
    markdown = render_benchmark_relative_metrics_markdown(metrics)

    assert "- Benchmark: S&P 500 total return (KRW-unhedged)" in markdown
    assert "- Bot total return (%): 5.00" in markdown
    assert "- Benchmark total return (%): 2.00" in markdown
    assert "- Excess return (%): 3.00" in markdown
    assert "- Relative drawdown (%): -" in markdown
    assert "- Up-capture (%): 250.0" in markdown
    assert "- Down-capture (%): 236.363636363636" in markdown
    assert "Synthetic benchmark values" not in markdown
    assert "## Warnings\n\n- None\n" in markdown


def test_paper_day_evidence_boundary_is_rendered_without_reading_evidence_files() -> None:
    markdown = render_benchmark_relative_metrics_markdown(_metrics())
    lowered = markdown.lower()

    assert "paper-day market-data evidence is not portfolio nav" in lowered
    assert "not valid input" in lowered
    assert "valid strategy nav series" in lowered
    assert "krw-unhedged s&p 500 total-return benchmark series" in lowered


def test_renderer_has_no_data_loading_or_fetching_behavior() -> None:
    source = inspect.getsource(render_benchmark_relative_metrics_markdown).lower()

    for forbidden in (
        "open(",
        "read_text",
        "requests",
        "urlopen",
        "socket",
        "yfinance",
        "fred",
    ):
        assert forbidden not in source
