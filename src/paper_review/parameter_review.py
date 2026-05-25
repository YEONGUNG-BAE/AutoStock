from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from allocator.rules import CASH_TARGET_MAX, CASH_TARGET_MIN, GOLD_EXCEPTION_MAX, GOLD_EXCEPTION_MIN
from emergency.models import EmergencyTriggerEvent, EmergencyTriggerType, MddStage
from logs.models import DailySummary
from paper_review.models import (
    AllocatorToleranceReview,
    AssetBandReview,
    ExecutionReviewMetrics,
    MddThresholdReview,
    ParameterRecommendation,
    PaperPerformanceMetrics,
    RecommendationActionability,
    RecommendationType,
    ReviewConfidence,
    ReviewPeriod,
    SampleSufficiency,
)
from postmortem.models import PostmortemRecord
from risk.rules import ASSET_CLASS_SOFT_BAND_MAX, ASSET_CLASS_SOFT_BAND_MIN


def _observe_more_recommendation(
    *,
    recommendation_id: str,
    parameter_name: str,
    current_value: str,
    reason: str,
) -> ParameterRecommendation:
    """insufficient sample용 OBSERVE_MORE recommendation."""
    return ParameterRecommendation(
        recommendation_id=recommendation_id,
        parameter_name=parameter_name,
        current_value=current_value,
        candidate_value=None,
        recommendation_type=RecommendationType.OBSERVE_MORE,
        actionability=RecommendationActionability.OBSERVE_MORE,
        evidence=(),
        confidence_level=ReviewConfidence.LOW,
        risk_of_change="insufficient sample; no parameter change candidate",
    )


def review_mdd_threshold(
    *,
    performance: PaperPerformanceMetrics,
    emergency_events: Sequence[EmergencyTriggerEvent],
    period: ReviewPeriod,
) -> MddThresholdReview:
    """MDD threshold conservative rule-based review."""
    level_1_count = 0
    level_2_count = 0
    level_3_count = 0

    for event in emergency_events:
        if event.payload.trigger_type != EmergencyTriggerType.MDD_KILLSWITCH:
            continue
        stage = event.payload.metadata.get("mdd_stage")
        if stage == MddStage.LEVEL_1.value:
            level_1_count += 1
        elif stage == MddStage.LEVEL_2.value:
            level_2_count += 1
        elif stage == MddStage.LEVEL_3.value:
            level_3_count += 1

    recommendations: list[ParameterRecommendation] = []

    if period.sample_sufficiency == SampleSufficiency.INSUFFICIENT:
        recommendations.append(
            _observe_more_recommendation(
                recommendation_id="mdd-threshold-observe",
                parameter_name="mdd_threshold_levels",
                current_value="LEVEL_1=-10%, LEVEL_2=-15%, LEVEL_3=-20%",
                reason="insufficient sample",
            )
        )
    elif level_3_count > 0:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="mdd-threshold-level3-investigate",
                parameter_name="mdd_threshold_level_3",
                current_value="-20%",
                candidate_value=None,
                recommendation_type=RecommendationType.INVESTIGATE,
                actionability=RecommendationActionability.HUMAN_REVIEW_REQUIRED,
                evidence=(
                    f"MDD Level 3 event occurred {level_3_count} time(s) during review period.",
                    f"observed max drawdown: {performance.max_drawdown_percent}%.",
                    "HIGH RISK: any MDD threshold change requires human review.",
                ),
                confidence_level=ReviewConfidence.HIGH,
                risk_of_change="HIGH — Level 3 events indicate severe drawdown; threshold changes are risky.",
            )
        )
    elif level_1_count + level_2_count >= 2:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="mdd-threshold-repeated-investigate",
                parameter_name="mdd_threshold_levels",
                current_value="LEVEL_1=-10%, LEVEL_2=-15%",
                candidate_value=None,
                recommendation_type=RecommendationType.INVESTIGATE,
                actionability=RecommendationActionability.HUMAN_REVIEW_REQUIRED,
                evidence=(
                    f"repeated MDD events: level_1={level_1_count}, level_2={level_2_count}.",
                    "investigate whether threshold is too loose or strategy exposure too high.",
                ),
                confidence_level=ReviewConfidence.MEDIUM,
                risk_of_change="MEDIUM — repeated MDD triggers may indicate calibration drift.",
            )
        )
    elif level_1_count + level_2_count + level_3_count == 0:
        if performance.max_drawdown_percent > Decimal("-5"):
            recommendations.append(
                ParameterRecommendation(
                    recommendation_id="mdd-threshold-keep",
                    parameter_name="mdd_threshold_levels",
                    current_value="LEVEL_1=-10%, LEVEL_2=-15%, LEVEL_3=-20%",
                    candidate_value=None,
                    recommendation_type=RecommendationType.KEEP,
                    actionability=RecommendationActionability.NOT_ACTIONABLE,
                    evidence=(
                        f"no MDD events during review; max drawdown {performance.max_drawdown_percent}%.",
                    ),
                    confidence_level=ReviewConfidence.MEDIUM,
                    risk_of_change="LOW — no MDD trigger evidence for change.",
                )
            )
        else:
            recommendations.append(
                ParameterRecommendation(
                    recommendation_id="mdd-threshold-observe-drawdown",
                    parameter_name="mdd_threshold_levels",
                    current_value="LEVEL_1=-10%, LEVEL_2=-15%, LEVEL_3=-20%",
                    candidate_value=None,
                    recommendation_type=RecommendationType.OBSERVE_MORE,
                    actionability=RecommendationActionability.OBSERVE_MORE,
                    evidence=(
                        f"drawdown {performance.max_drawdown_percent}% without MDD trigger; "
                        "monitor for missed-risk patterns.",
                    ),
                    confidence_level=ReviewConfidence.LOW,
                    risk_of_change="LOW — no automatic threshold change.",
                )
            )

    return MddThresholdReview(
        observed_max_drawdown_percent=performance.max_drawdown_percent,
        mdd_level_1_count=level_1_count,
        mdd_level_2_count=level_2_count,
        mdd_level_3_count=level_3_count,
        false_positive_suspected_count=0,
        missed_risk_suspected_count=0,
        recommendations=tuple(recommendations),
    )


def review_execution_model(
    *,
    execution: ExecutionReviewMetrics,
    period: ReviewPeriod,
) -> tuple[ParameterRecommendation, ...]:
    """execution model conservative review recommendations."""
    recommendations: list[ParameterRecommendation] = []

    if period.sample_sufficiency == SampleSufficiency.INSUFFICIENT:
        recommendations.append(
            _observe_more_recommendation(
                recommendation_id="execution-model-observe",
                parameter_name="execution_model",
                current_value="paper_market_fill",
                reason="insufficient sample",
            )
        )
        return tuple(recommendations)

    if execution.fill_count == 0 and execution.order_intent_count > 0:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="execution-model-insufficient-fills",
                parameter_name="execution_model",
                current_value="paper_market_fill",
                candidate_value=None,
                recommendation_type=RecommendationType.OBSERVE_MORE,
                actionability=RecommendationActionability.OBSERVE_MORE,
                evidence=(
                    f"order_intent_count={execution.order_intent_count} but fill_count=0.",
                ),
                confidence_level=ReviewConfidence.LOW,
                risk_of_change="LOW — observe more before changing execution model.",
            )
        )

    emergency_orders = execution.emergency_count + execution.mdd_killswitch_count
    if emergency_orders >= 3:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="execution-model-emergency-resilience",
                parameter_name="execution_model",
                current_value="paper_market_fill",
                candidate_value=None,
                recommendation_type=RecommendationType.INVESTIGATE,
                actionability=RecommendationActionability.HUMAN_REVIEW_REQUIRED,
                evidence=(
                    f"emergency/MDD orders generated: emergency={execution.emergency_count}, "
                    f"mdd_killswitch={execution.mdd_killswitch_count}.",
                    "investigate execution model resilience under stress.",
                ),
                confidence_level=ReviewConfidence.MEDIUM,
                risk_of_change="MEDIUM — stress-path execution quality needs human review.",
            )
        )

    if execution.rejected_count > 0:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="execution-model-rejected-orders",
                parameter_name="execution_model",
                current_value="paper_market_fill",
                candidate_value=None,
                recommendation_type=RecommendationType.INVESTIGATE,
                actionability=RecommendationActionability.HUMAN_REVIEW_REQUIRED,
                evidence=(f"rejected_count={execution.rejected_count} during review period.",),
                confidence_level=ReviewConfidence.MEDIUM,
                risk_of_change="MEDIUM — rejected orders may indicate execution path issues.",
            )
        )

    if not recommendations:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="execution-model-keep",
                parameter_name="execution_model",
                current_value="paper_market_fill",
                candidate_value=None,
                recommendation_type=RecommendationType.KEEP,
                actionability=RecommendationActionability.NOT_ACTIONABLE,
                evidence=(
                    f"fill_count={execution.fill_count}, order_intent_count={execution.order_intent_count}.",
                ),
                confidence_level=ReviewConfidence.MEDIUM,
                risk_of_change="LOW — no execution model change indicated.",
            )
        )

    return tuple(recommendations)


def _asset_weight_in_band(weight: Decimal) -> bool:
    return ASSET_CLASS_SOFT_BAND_MIN <= weight <= ASSET_CLASS_SOFT_BAND_MAX


def review_asset_bands(
    *,
    daily_summaries: Sequence[DailySummary],
    emergency_events: Sequence[EmergencyTriggerEvent],
    postmortem_records: Sequence[PostmortemRecord],
    period: ReviewPeriod,
) -> AssetBandReview:
    """asset band conservative rule-based review."""
    kr_breaches = 0
    us_breaches = 0
    gold_breaches = 0
    cash_breaches = 0
    in_band_days = 0
    total_weight_days = 0
    recovery_review_count = 0

    for summary in daily_summaries:
        kr_breaches += summary.range_violation_count
        if summary.asset_class_weights:
            total_weight_days += 1
            day_in_band = True
            for key, percent in summary.asset_class_weights.items():
                if not _asset_weight_in_band(percent.value):
                    day_in_band = False
                    lowered = key.lower()
                    if "kr" in lowered:
                        kr_breaches += 1
                    elif "us" in lowered:
                        us_breaches += 1
                    elif "gold" in lowered:
                        gold_breaches += 1
                    elif "cash" in lowered:
                        cash_breaches += 1
            if day_in_band:
                in_band_days += 1

        cash_percent = _extract_cash_percent(summary)
        if cash_percent is not None:
            if cash_percent < CASH_TARGET_MIN or cash_percent > CASH_TARGET_MAX:
                cash_breaches += 1

    for event in emergency_events:
        if event.payload.requires_recovery_review:
            recovery_review_count += 1

    time_in_band: Decimal | None = None
    if total_weight_days > 0:
        time_in_band = (Decimal(in_band_days) / Decimal(total_weight_days)) * Decimal("100")

    recommendations: list[ParameterRecommendation] = []
    total_breaches = kr_breaches + us_breaches + gold_breaches + cash_breaches

    if period.sample_sufficiency == SampleSufficiency.INSUFFICIENT:
        recommendations.append(
            _observe_more_recommendation(
                recommendation_id="asset-band-observe",
                parameter_name="asset_class_soft_bands",
                current_value=f"{ASSET_CLASS_SOFT_BAND_MIN}~{ASSET_CLASS_SOFT_BAND_MAX}%",
                reason="insufficient sample",
            )
        )
    elif total_breaches >= 5:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="asset-band-sustained-breach",
                parameter_name="asset_class_soft_bands",
                current_value=f"{ASSET_CLASS_SOFT_BAND_MIN}~{ASSET_CLASS_SOFT_BAND_MAX}%",
                candidate_value=None,
                recommendation_type=RecommendationType.INVESTIGATE,
                actionability=RecommendationActionability.HUMAN_REVIEW_REQUIRED,
                evidence=(
                    f"sustained band breaches: kr={kr_breaches}, us={us_breaches}, "
                    f"gold={gold_breaches}, cash={cash_breaches}.",
                    "do not widen bands only because of short-term underperformance.",
                ),
                confidence_level=ReviewConfidence.MEDIUM,
                risk_of_change="MEDIUM — band changes require Postmortem evidence.",
            )
        )
    elif total_breaches == 0:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="asset-band-keep",
                parameter_name="asset_class_soft_bands",
                current_value=f"{ASSET_CLASS_SOFT_BAND_MIN}~{ASSET_CLASS_SOFT_BAND_MAX}%",
                candidate_value=None,
                recommendation_type=RecommendationType.KEEP,
                actionability=RecommendationActionability.NOT_ACTIONABLE,
                evidence=("no sustained asset band breaches detected.",),
                confidence_level=ReviewConfidence.MEDIUM,
                risk_of_change="LOW — bands appear adequate for review period.",
            )
        )
    else:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="asset-band-observe-transient",
                parameter_name="asset_class_soft_bands",
                current_value=f"{ASSET_CLASS_SOFT_BAND_MIN}~{ASSET_CLASS_SOFT_BAND_MAX}%",
                candidate_value=None,
                recommendation_type=RecommendationType.OBSERVE_MORE,
                actionability=RecommendationActionability.OBSERVE_MORE,
                evidence=(
                    f"transient breaches total={total_breaches}; "
                    "emergency/MDD transient breaches are not normal target states.",
                ),
                confidence_level=ReviewConfidence.LOW,
                risk_of_change="LOW — observe more before band change.",
            )
        )

    _ = postmortem_records  # Phase 16: Postmortem evidence reserved for future P3 integration

    return AssetBandReview(
        kr_band_breach_count=kr_breaches,
        us_band_breach_count=us_breaches,
        gold_band_breach_count=gold_breaches,
        cash_band_breach_count=cash_breaches,
        time_in_band_percent=time_in_band,
        recovery_review_count=recovery_review_count,
        recommendations=tuple(recommendations),
    )


def _extract_cash_percent(summary: DailySummary) -> Decimal | None:
    """DailySummary portfolio_state 또는 asset_class_weights에서 cash % 추출."""
    if summary.asset_class_weights:
        for key, percent in summary.asset_class_weights.items():
            if "cash" in key.lower():
                return percent.value

    if summary.portfolio_state and isinstance(summary.portfolio_state, dict):
        cash_value = summary.portfolio_state.get("cash_percent")
        if cash_value is not None:
            return Decimal(str(cash_value))

    return None


def review_allocator_tolerance(
    *,
    daily_summaries: Sequence[DailySummary],
    period: ReviewPeriod,
) -> AllocatorToleranceReview:
    """Allocator tolerance conservative rule-based review."""
    allocator_fallback_count = sum(item.allocator_fallback_count for item in daily_summaries)
    target_sum_invalid_count = sum(item.validation_failed_count for item in daily_summaries)
    tolerance_breach_count = sum(item.range_violation_count for item in daily_summaries)
    cash_target_out_of_range_count = 0
    gold_target_out_of_range_count = 0

    for summary in daily_summaries:
        cash_percent = _extract_cash_percent(summary)
        if cash_percent is not None and (
            cash_percent < CASH_TARGET_MIN or cash_percent > CASH_TARGET_MAX
        ):
            cash_target_out_of_range_count += 1

        if summary.asset_class_weights:
            for key, percent in summary.asset_class_weights.items():
                if "gold" in key.lower() and (
                    percent.value < GOLD_EXCEPTION_MIN or percent.value > GOLD_EXCEPTION_MAX
                ):
                    gold_target_out_of_range_count += 1

    recommendations: list[ParameterRecommendation] = []

    if period.sample_sufficiency == SampleSufficiency.INSUFFICIENT:
        recommendations.append(
            _observe_more_recommendation(
                recommendation_id="allocator-tolerance-observe",
                parameter_name="allocator_tolerance_percent",
                current_value="schema-defined",
                reason="insufficient sample",
            )
        )
    elif allocator_fallback_count >= 3:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="allocator-fallback-investigate",
                parameter_name="allocator_schema_and_tolerance",
                current_value="allocator.v1",
                candidate_value=None,
                recommendation_type=RecommendationType.INVESTIGATE,
                actionability=RecommendationActionability.HUMAN_REVIEW_REQUIRED,
                evidence=(
                    f"allocator_fallback_count={allocator_fallback_count}.",
                    "schema/fallback errors suggest prompt/schema/tooling review, not necessarily tolerance change.",
                ),
                confidence_level=ReviewConfidence.MEDIUM,
                risk_of_change="MEDIUM — distinguish schema errors from strategy drift.",
            )
        )
    elif tolerance_breach_count >= 5 and allocator_fallback_count == 0:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="allocator-tolerance-breach-investigate",
                parameter_name="allocator_tolerance_percent",
                current_value="schema-defined",
                candidate_value=None,
                recommendation_type=RecommendationType.INVESTIGATE,
                actionability=RecommendationActionability.HUMAN_REVIEW_REQUIRED,
                evidence=(
                    f"tolerance_breach_count={tolerance_breach_count} without allocator fallback.",
                    "frequent tolerance breaches without schema errors warrant investigation.",
                ),
                confidence_level=ReviewConfidence.MEDIUM,
                risk_of_change="MEDIUM — no automatic tolerance update.",
            )
        )
    else:
        recommendations.append(
            ParameterRecommendation(
                recommendation_id="allocator-tolerance-keep",
                parameter_name="allocator_tolerance_percent",
                current_value="schema-defined",
                candidate_value=None,
                recommendation_type=RecommendationType.KEEP,
                actionability=RecommendationActionability.NOT_ACTIONABLE,
                evidence=(
                    f"allocator_fallback_count={allocator_fallback_count}, "
                    f"validation_failed_count={target_sum_invalid_count}.",
                ),
                confidence_level=ReviewConfidence.MEDIUM,
                risk_of_change="LOW — no tolerance change indicated.",
            )
        )

    return AllocatorToleranceReview(
        allocator_fallback_count=allocator_fallback_count,
        target_sum_invalid_count=target_sum_invalid_count,
        cash_target_out_of_range_count=cash_target_out_of_range_count,
        gold_target_out_of_range_count=gold_target_out_of_range_count,
        tolerance_breach_count=tolerance_breach_count,
        recommendations=tuple(recommendations),
    )
