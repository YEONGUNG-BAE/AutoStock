from __future__ import annotations

from paper_review.data_quality import collect_data_quality_warnings
from paper_review.metrics import (
    compute_execution_review_metrics,
    compute_paper_performance_metrics,
    count_emergency_triggers,
)
from paper_review.models import (
    BenchmarkRelativeMetrics,
    PaperReviewInput,
    PaperReviewReport,
    ParameterRecommendation,
)
from paper_review.parameter_review import (
    review_allocator_tolerance,
    review_asset_bands,
    review_execution_model,
    review_mdd_threshold,
)
from postmortem.aggregation import top_error_tags_from_summaries


def build_paper_review_report(review_input: PaperReviewInput) -> PaperReviewReport:
    """PaperReviewInput에서 deterministic paper review report를 생성한다."""
    period = review_input.period
    data_quality_warnings = collect_data_quality_warnings(review_input)

    performance_metrics = compute_paper_performance_metrics(
        review_input.nav_snapshots,
        period,
    )

    rejected_count = sum(summary.rejected_orders for summary in review_input.daily_summaries)
    rejected_available = bool(review_input.daily_summaries)

    execution_metrics = compute_execution_review_metrics(
        review_input.order_intents,
        review_input.fills,
        rejected_count=rejected_count,
        rejected_count_available=rejected_available,
    )

    mdd_threshold_review = review_mdd_threshold(
        performance=performance_metrics,
        emergency_events=review_input.emergency_events,
        period=period,
    )

    asset_band_review = review_asset_bands(
        daily_summaries=review_input.daily_summaries,
        emergency_events=review_input.emergency_events,
        postmortem_records=review_input.postmortem_records,
        period=period,
    )

    allocator_tolerance_review = review_allocator_tolerance(
        daily_summaries=review_input.daily_summaries,
        period=period,
    )

    execution_recommendations = review_execution_model(
        execution=execution_metrics,
        period=period,
    )

    tag_summaries = tuple(record.tag_summary for record in review_input.postmortem_records)
    postmortem_top_error_tags = top_error_tags_from_summaries(tag_summaries)

    emergency_trigger_counts = count_emergency_triggers(review_input.emergency_events)

    recommendations = _merge_recommendations(
        mdd_threshold_review.recommendations,
        asset_band_review.recommendations,
        allocator_tolerance_review.recommendations,
        execution_recommendations,
    )

    metadata = dict(review_input.metadata)
    metadata.setdefault("phase", "16")
    metadata.setdefault("auto_apply", False)
    metadata.setdefault("human_approval_required", True)

    return PaperReviewReport(
        review_id=review_input.review_id,
        created_at=review_input.created_at,
        period=period,
        sample_sufficiency=period.sample_sufficiency,
        performance_metrics=performance_metrics,
        execution_metrics=execution_metrics,
        mdd_threshold_review=mdd_threshold_review,
        asset_band_review=asset_band_review,
        allocator_tolerance_review=allocator_tolerance_review,
        postmortem_top_error_tags=postmortem_top_error_tags,
        emergency_trigger_counts=emergency_trigger_counts,
        data_quality_warnings=data_quality_warnings,
        recommendations=recommendations,
        metadata=metadata,
    )


def _merge_recommendations(
    *groups: tuple[ParameterRecommendation, ...],
) -> tuple[ParameterRecommendation, ...]:
    """여러 review section recommendation을 deterministic order로 병합한다."""
    merged: list[ParameterRecommendation] = []
    for group in groups:
        merged.extend(group)
    merged.sort(key=lambda item: item.recommendation_id)
    return tuple(merged)


def render_benchmark_relative_metrics_markdown(
    metrics: BenchmarkRelativeMetrics,
    *,
    title: str = "Benchmark-relative performance",
    benchmark_name: str = "S&P 500 total return (KRW-unhedged)",
) -> str:
    """BenchmarkRelativeMetrics를 deterministic human-readable markdown으로 렌더링한다."""
    lines: list[str] = [
        f"# {title}",
        "",
        f"- Benchmark: {benchmark_name}",
        "",
        "## Observation counts",
        "",
        f"- Aligned observations: {metrics.aligned_observation_count}",
        f"- Return observations: {metrics.return_observation_count}",
        f"- Benchmark observations supplied: {metrics.benchmark_observation_count}",
        "",
        "## Core return metrics",
        "",
        f"- Bot total return (%): {_format_optional_decimal(metrics.bot_total_return_percent)}",
        f"- Benchmark total return (%): {_format_optional_decimal(metrics.benchmark_total_return_percent)}",
        f"- Excess return (%): {_format_optional_decimal(metrics.excess_return_percent)}",
        "",
        "## Relative risk metrics",
        "",
        f"- Relative drawdown (%): {_format_optional_decimal(metrics.relative_drawdown_percent)}",
        f"- Tracking error (%): {_format_optional_decimal(metrics.tracking_error_daily_percent)}",
        f"- Information ratio annualized: {_format_optional_decimal(metrics.information_ratio_annualized)}",
        f"- Beta to benchmark: {_format_optional_decimal(metrics.beta_to_benchmark)}",
        "",
        "## Capture metrics",
        "",
        f"- Up-capture (%): {_format_optional_decimal(metrics.up_capture_percent)}",
        f"- Down-capture (%): {_format_optional_decimal(metrics.down_capture_percent)}",
        "",
        "## Warnings",
        "",
    ]

    if metrics.warnings:
        for warning in metrics.warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Interpretation notes",
            "",
            "- Positive excess return means the bot outperformed the benchmark over the aligned window.",
            "- Negative relative drawdown means the bot underperformed from a prior relative peak.",
            "- Down-capture above 100 means the bot lost more than the benchmark in benchmark-down periods.",
            "- Metrics are only as meaningful as the supplied NAV and benchmark series.",
            "- This report is not a historical backtest by itself.",
            "- Paper-Day market-data evidence is not portfolio NAV and is not valid input for this report.",
            "- Real investment-performance numbers require a valid strategy NAV series plus a KRW-unhedged S&P 500 total-return benchmark series.",
        ]
    )

    return "\n".join(lines).rstrip() + "\n"


def _format_optional_decimal(value: object | None) -> str:
    if value is None:
        return "None"
    return str(value)


def render_paper_review_markdown(report: PaperReviewReport) -> str:
    """PaperReviewReport를 deterministic markdown으로 렌더링한다. LLM prose 없음."""
    lines: list[str] = [
        f"# Paper Review Report: {report.review_id}",
        "",
        f"- Created at: {report.created_at.isoformat()}",
        f"- Period: {report.period.start_date.isoformat()} ~ {report.period.end_date.isoformat()}",
        f"- Calendar days: {report.period.calendar_days}",
        f"- Sample sufficiency: **{report.sample_sufficiency.value}**",
        "",
        "## Out of scope",
        "",
        "- No config changes applied",
        "- No orders submitted",
        "- No live/KIS data mutated",
        "- No LLM call made",
        "- auto_apply=false for all recommendations",
        "- human approval required for all actionable recommendations",
        "",
    ]

    if report.data_quality_warnings:
        lines.extend(["## Data quality warnings", ""])
        for warning in report.data_quality_warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.extend(
        [
            "## Observations",
            "",
            "### Performance",
            f"- Start NAV (KRW): {report.performance_metrics.start_nav_krw}",
            f"- End NAV (KRW): {report.performance_metrics.end_nav_krw}",
            f"- Total return (%): {report.performance_metrics.total_return_percent}",
            f"- Max drawdown (%): {report.performance_metrics.max_drawdown_percent}",
            f"- NAV snapshot count: {report.performance_metrics.nav_snapshot_count}",
            "",
            "### Execution",
            f"- Order intents: {report.execution_metrics.order_intent_count}",
            f"- Fills: {report.execution_metrics.fill_count}",
            f"- Rejected: {report.execution_metrics.rejected_count}",
            f"- Emergency orders: {report.execution_metrics.emergency_count}",
            f"- MDD killswitch orders: {report.execution_metrics.mdd_killswitch_count}",
            "",
            "### Emergency triggers",
        ]
    )

    if report.emergency_trigger_counts:
        for key, count in report.emergency_trigger_counts.items():
            lines.append(f"- {key}: {count}")
    else:
        lines.append("- (none)")

    lines.extend(["", "### Postmortem top error tags"])
    if report.postmortem_top_error_tags:
        for tag in report.postmortem_top_error_tags:
            lines.append(f"- {tag}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Diagnostics", ""])
    lines.append(
        f"- MDD events: L1={report.mdd_threshold_review.mdd_level_1_count}, "
        f"L2={report.mdd_threshold_review.mdd_level_2_count}, "
        f"L3={report.mdd_threshold_review.mdd_level_3_count}"
    )
    lines.append(
        f"- Asset band breaches: KR={report.asset_band_review.kr_band_breach_count}, "
        f"US={report.asset_band_review.us_band_breach_count}, "
        f"Gold={report.asset_band_review.gold_band_breach_count}, "
        f"Cash={report.asset_band_review.cash_band_breach_count}"
    )
    lines.append(
        f"- Allocator fallback count: {report.allocator_tolerance_review.allocator_fallback_count}"
    )
    lines.append(
        f"- False positive suspected (MDD): {report.mdd_threshold_review.false_positive_suspected_count}"
    )
    lines.append(
        f"- Missed risk suspected (MDD): {report.mdd_threshold_review.missed_risk_suspected_count}"
    )
    lines.append("")

    lines.extend(["## Recommendations", ""])
    for recommendation in report.recommendations:
        lines.extend(
            [
                f"### {recommendation.recommendation_id}",
                f"- parameter: {recommendation.parameter_name}",
                f"- type: {recommendation.recommendation_type.value}",
                f"- actionability: {recommendation.actionability.value}",
                f"- current_value: {recommendation.current_value}",
                f"- candidate_value: {recommendation.candidate_value}",
                f"- confidence: {recommendation.confidence_level.value}",
                f"- risk_of_change: {recommendation.risk_of_change}",
                f"- requires_human_approval: {recommendation.requires_human_approval}",
                f"- auto_apply: {recommendation.auto_apply}",
            ]
        )
        if recommendation.evidence:
            lines.append("- evidence:")
            for item in recommendation.evidence:
                lines.append(f"  - {item}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
