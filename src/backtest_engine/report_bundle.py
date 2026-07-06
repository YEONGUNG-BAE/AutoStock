"""Benchmark-relative metrics to markdown report bundle adapter for Phase 2c-9.

이 모듈은 이미 계산된 ``BacktestBenchmarkRelativeResult``를 감싸 기존 Phase 1.5
markdown renderer로 사람이 읽을 수 있는 보고서를 생성한다. benchmark-relative
지표를 재계산하거나 walk-forward를 실행하거나 데이터를 로드·조회하지 않으며,
보고서 파일을 쓰거나 프로젝트 수준의 투자 결론을 추가하지 않는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from backtest_engine.benchmark_adapter import BacktestBenchmarkRelativeResult
from paper_review.report import render_benchmark_relative_metrics_markdown

BACKTEST_REPORT_BUNDLE_POLICY_V1 = "benchmark_relative_metrics_markdown_bundle.v1"


class BacktestEvaluationReportBundle(BaseModel):
    """benchmark-relative backtest 지표용 불변 markdown report bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_bundle_policy: Literal["benchmark_relative_metrics_markdown_bundle.v1"]
    benchmark_relative_result: BacktestBenchmarkRelativeResult
    markdown_report: str

    @field_validator("markdown_report")
    @classmethod
    def validate_markdown_report(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("markdown_report must be a non-empty string.")
        return value


def render_backtest_evaluation_report_bundle(
    *,
    benchmark_relative_result: BacktestBenchmarkRelativeResult,
) -> BacktestEvaluationReportBundle:
    """이미 계산된 benchmark-relative 결과로 evaluation report bundle을 렌더링한다."""
    markdown_report = render_benchmark_relative_metrics_markdown(
        benchmark_relative_result.metrics,
    )
    return BacktestEvaluationReportBundle(
        report_bundle_policy=BACKTEST_REPORT_BUNDLE_POLICY_V1,
        benchmark_relative_result=benchmark_relative_result,
        markdown_report=markdown_report,
    )
