"""Guard the benchmark data and future backtest planning document.

These tests are docs-only. They do not import runtime code, read config, fetch
data, or inspect live/runtime state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN = REPO_ROOT / "docs" / "BENCHMARK_DATA_AND_BACKTEST_PLAN.md"
OBJECTIVE = REPO_ROOT / "docs" / "INVESTMENT_OBJECTIVE_BENCHMARK.md"


@pytest.fixture(scope="module")
def plan_text() -> str:
    assert PLAN.is_file(), "benchmark data and backtest plan must exist"
    return PLAN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def objective_text() -> str:
    assert OBJECTIVE.is_file(), "investment objective doc must exist"
    return OBJECTIVE.read_text(encoding="utf-8")


def test_plan_is_future_direction_not_runtime_requirement(plan_text: str) -> None:
    lowered = plan_text.lower()
    assert "planning" in lowered
    assert "future-direction" in lowered or "future direction" in lowered
    assert "not an implemented runtime spec" in lowered
    assert "not an active implementation" in lowered
    assert "fetches no data" in lowered or "no data fetch" in lowered
    assert "builds no backtest harness" in lowered or "no historical backtest harness" in lowered


def test_plan_has_required_sections(plan_text: str) -> None:
    for heading in (
        "## 1. Purpose and Non-Goals",
        "## 2. Primary Benchmark Definition",
        "## 3. FX Source and Rules",
        "## 4. Secondary Diagnostic Baselines",
        "## 5. Observation Frequency and Annualization",
        "## 6. Bias Controls",
        "## 7. Replay vs True Backtest",
        "## 8. Output Contract",
        "## 9. Phasing After This Plan",
        "## 10. Explicit Prohibitions Carried Forward",
    ):
        assert heading in plan_text


def test_primary_benchmark_and_krw_unhedged_conversion(plan_text: str) -> None:
    lowered = plan_text.lower()
    assert "s&p 500 total return" in lowered
    assert "dividends reinvested" in lowered
    assert "not s&p" in lowered and "price return" in lowered
    assert "krw-unhedged" in lowered
    assert "sp500_tr_krw_level(t)" in plan_text
    assert "sp500_tr_usd_level(t) * usdkrw(t)" in plan_text
    assert "diagnostic" in lowered
    assert "raw usd" in lowered or "usd-basis" in lowered


def test_fx_and_alignment_rules(plan_text: str) -> None:
    lowered = plan_text.lower()
    assert "usdkrw" in lowered
    assert "close" in lowered
    assert "time zone" in lowered or "timezone" in lowered
    assert "krx" in lowered
    assert "nyse" in lowered
    assert "common calendar dates" in lowered or "common dates" in lowered
    assert "no forward-fill" in lowered
    assert "no interpolation" in lowered


def test_secondary_diagnostic_baselines(plan_text: str) -> None:
    lowered = plan_text.lower()
    assert "static s&p 500 100%" in lowered
    assert "static autostock neutral allocation baseline" in lowered
    assert "llm-vs-static" in lowered or "llm allocator" in lowered
    assert "cash 20%" in lowered
    assert "kr 40%" in lowered
    assert "us 24%" in lowered
    assert "gold 16%" in lowered
    assert "kr/us/gold" in lowered
    assert "60/40" in plan_text
    assert "kospi200" in lowered or "kospi 200" in lowered
    assert "gold proxy" in lowered
    assert "diagnostics" in lowered
    assert "do not replace" in lowered or "does not replace" in lowered


def test_observation_frequency_and_annualization(plan_text: str) -> None:
    lowered = plan_text.lower()
    assert "daily trading-day" in lowered
    assert "weekly" in lowered
    assert "monthly" in lowered
    assert "periods_per_year" in plan_text
    assert "252" in plan_text
    assert "52" in plan_text
    assert "12" in plan_text
    assert "sqrt(252)" in plan_text
    assert "hardcoded 252" in lowered
    assert "parameterize" in lowered
    assert "only for trading-day-frequency" in lowered or "correct only for trading-day" in lowered


def test_bias_controls_are_concrete_and_auditable(plan_text: str) -> None:
    lowered = plan_text.lower()
    assert "look-ahead bias" in lowered
    assert "as_of <= decision_time" in plan_text
    assert "future-dated" in lowered
    assert "survivorship bias" in lowered
    assert "point-in-time" in lowered
    assert "delisted" in lowered
    assert "split/dividend" in lowered
    assert "total-return" in lowered
    assert "dividend adjusted" in lowered
    assert "timezone and calendar" in lowered
    assert "fixtures" in lowered
    assert "audit/test" in lowered


def test_replay_vs_true_backtest_distinction(plan_text: str) -> None:
    lowered = plan_text.lower()
    assert "deterministic replay" in lowered
    assert "same explicit input produces the same output" in lowered
    assert "not" in lowered and "strategy performance" in lowered
    assert "historical backtest" in lowered
    assert "produce a nav track record" in lowered
    assert "exist yet" in lowered
    assert "renderer" in lowered and "reused" in lowered


def test_output_contract_references_existing_symbols(plan_text: str) -> None:
    for token in (
        "domain.portfolio.NavSnapshot",
        "paper_review.BenchmarkReturnPoint",
        "compute_benchmark_relative_metrics",
        "render_benchmark_relative_metrics_markdown",
    ):
        assert token in plan_text


def test_phasing_and_allocator_v2_gate(plan_text: str) -> None:
    lowered = plan_text.lower()
    assert "future task" in lowered
    assert "evaluate v1" in lowered
    assert "static diagnostic" in lowered
    assert "allocator v2" in lowered
    assert "static autostock neutral baseline" in lowered
    assert "reduce or remove" in lowered
    assert "9-12월 paper" in plan_text
    assert "frozen" in lowered and "strategy" in lowered
    assert "not proof of 10-year alpha" in lowered


def test_explicit_prohibitions(plan_text: str) -> None:
    lowered = plan_text.lower()
    assert "no unsupported expected-return probability tables" in lowered
    assert "operational safety gates remain strict" in lowered
    assert "separate from the investment" in lowered
    assert "no live-order" in lowered
    assert "no data fetch" in lowered
    assert "adapter" in lowered
    assert "historical harness" in lowered


def test_multi_option_decisions_record_default_alternative_and_reason(plan_text: str) -> None:
    lowered = plan_text.lower()
    assert lowered.count("recommended default") >= 10
    assert lowered.count("alternative") >= 10
    assert "rejected because" in lowered


def test_objective_doc_points_to_plan(objective_text: str) -> None:
    lowered = objective_text.lower()
    assert "docs/BENCHMARK_DATA_AND_BACKTEST_PLAN.md" in objective_text
    assert "krw-unhedged benchmark basis" in lowered
    assert "observation frequency / annualization" in lowered
    assert "bias controls" in lowered
