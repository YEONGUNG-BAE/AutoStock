"""Guard the S&P 500 benchmark-relative investment-objective planning docs.

`docs/INVESTMENT_OBJECTIVE_BENCHMARK.md` records the clarified long-term economic
objective (beat S&P 500 total return over ~10 years) and the design direction it
implies. These guards lock its load-bearing *concepts* so a future edit cannot
silently drop them: the primary objective, the strict separation of operational
safety from investment defensiveness, the primary/diagnostic benchmark set, the
current design mismatch (absolute metrics exist, benchmark-relative calculation
exists but benchmark sourcing/backtest evidence remain missing; `AllocationRegime`
orphaned), the required future metrics / evaluation order / Allocator v2
direction, and the failure/success criteria. They also verify the
README note+link, the exact Future Direction section in the relevant Cursor
rules, and the TECH_DEBT forward pointer.

Concept matching, not brittle exact-sentence matching: assertions check for
stable tokens/concepts (often lowercased) rather than one exact English sentence,
so a Korean or slightly reworded phrasing of the same idea still passes. This is
a docs/rules guard only -- no runtime import, no network, no secrets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "INVESTMENT_OBJECTIVE_BENCHMARK.md"
README = REPO_ROOT / "README.md"
TECH_DEBT = REPO_ROOT / "docs" / "TECH_DEBT.md"

DOC_REL = "docs/INVESTMENT_OBJECTIVE_BENCHMARK.md"
FUTURE_DIRECTION_TITLE = "## Future Direction — NOT ACTIVE IMPLEMENTATION REQUIREMENT"
RULES_WITH_FUTURE_DIRECTION = (
    "05-allocator.mdc",
    "06-risk-filters-and-orders.mdc",
    "08-logs-debug-postmortem.mdc",
    "09-testing-paper-trading.mdc",
    "11-runtime-config-and-mode.mdc",
    "14-broker-api-and-paper-broker.mdc",
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), "investment objective benchmark doc must exist"
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme_text() -> str:
    assert README.is_file(), "README must exist"
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tech_debt_text() -> str:
    assert TECH_DEBT.is_file(), "TECH_DEBT doc must exist"
    return TECH_DEBT.read_text(encoding="utf-8")


def _rule_text(name: str) -> str:
    path = REPO_ROOT / ".cursor" / "rules" / name
    assert path.is_file(), f"rule file must exist: {name}"
    return path.read_text(encoding="utf-8")


def test_doc_exists_and_nonempty(doc_text: str) -> None:
    assert doc_text.strip(), "investment objective doc must not be empty"


def test_doc_is_planning_future_direction_not_runtime_spec(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "planning" in lowered
    assert "future" in lowered and "direction" in lowered
    # It must disclaim being an implemented / active spec.
    assert "not an implemented runtime spec" in lowered
    assert "not an active implementation requirement" in lowered


def test_primary_objective_is_beat_sp500_total_return_over_decade(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "s&p 500 total return" in lowered
    assert "terminal wealth" in lowered
    # ~10 year horizon in some stable form.
    assert any(tok in lowered for tok in ("10-year", "10 year", "~10", "decade"))
    # Framed against being merely a safer balanced bot.
    assert "balanced" in lowered


def test_operational_safety_vs_investment_defensiveness(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "operational safety" in lowered
    assert "investment defensiveness" in lowered
    # Safety is strict / never relaxed; defensiveness may be relaxed.
    assert "never" in lowered
    assert "relax" in lowered
    # Weakening operational safety is an automatic failure.
    assert "automatic failure" in lowered


def test_primary_benchmark_sp500_tr_krw_unhedged(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "krw-unhedged" in lowered or "krw unhedged" in lowered
    assert "dividends reinvested" in lowered or "total return" in lowered


def test_secondary_diagnostic_benchmarks_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "diagnostic" in lowered
    assert "kospi" in lowered
    assert "60/40" in doc_text
    assert "static" in lowered
    assert "neutral baseline" in lowered


def test_current_design_mismatch_absolute_and_relative_calculation_status(doc_text: str) -> None:
    lowered = doc_text.lower()
    # Do NOT overstate as "no evaluation exists": absolute and relative calculations exist.
    assert "absolute" in lowered
    assert "already exist" in lowered
    assert "benchmark-relative" in lowered
    assert "src/paper_review/metrics.py" in doc_text
    assert "src/paper_review/models.py" in doc_text
    assert "calculation models/functions now exist" in lowered
    assert "benchmark data fetching" in lowered
    assert "historical" in lowered and "backtest evidence" in lowered
    # AllocationRegime exists but is orphaned (not a brand-new concept).
    assert "AllocationRegime" in doc_text
    assert "orphaned" in lowered


def test_correct_target_direction_benchmark_relative_active_allocator(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "benchmark-relative active allocator" in lowered
    assert "core" in lowered
    assert "evidence-gated" in lowered


def test_required_future_metrics_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    for token in (
        "excess return",
        "tracking error",
        "information ratio",
        "up-capture",
        "down-capture",
        "beta",
        "turnover",
        "cost drag",
        "attribution",
    ):
        assert token in lowered, f"missing future metric concept: {token}"


def test_required_future_evaluation_order(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "historical backtest" in lowered
    assert "out-of-sample" in lowered
    # Paper is forward validation, not proof of a long-run S&P edge.
    assert "paper" in lowered
    assert "forward" in lowered
    assert "proof" in lowered


def test_required_future_allocator_v2_candidate_fields(doc_text: str) -> None:
    for field in (
        "allocation_regime",
        "target_equity_exposure",
        "benchmark_core_exposure",
        "active_deviation_from_benchmark",
        "deviation_rationale",
        "up_market_participation_check",
        "defensive_hedge_level",
    ):
        assert field in doc_text, f"missing Allocator v2 candidate field: {field}"
    # Must explicitly say not to build it now.
    assert "do not implement" in doc_text.lower()


def test_required_future_risk_control_concepts(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "up-market participation" in lowered
    assert "mdd killswitch" in lowered
    # Benchmark objective must stay separate from live/activation authorization.
    assert "separate" in lowered
    assert "activation" in lowered or "live-order authorization" in lowered


def test_failure_and_success_criteria_present(doc_text: str) -> None:
    lowered = doc_text.lower()
    assert "failure criteria" in lowered
    assert "success criteria" in lowered
    assert "chronically" in lowered
    assert "positive information ratio" in lowered
    # Success requires operational safety stayed strict.
    assert "strict" in lowered


def test_doc_does_not_overpromise(doc_text: str) -> None:
    lowered = doc_text.lower()
    # No fabricated guarantees or probability-of-outperformance claims.
    assert "guarantee" not in lowered
    assert "guaranteed" not in lowered


def test_readme_notes_objective_and_links_doc(readme_text: str) -> None:
    assert DOC_REL in readme_text
    lowered = readme_text.lower()
    assert "s&p 500" in lowered
    # Safety necessary-but-not-sufficient concept (Korean or English phrasing).
    assert "operational safety" in lowered or "운영 안전" in readme_text


@pytest.mark.parametrize("rule_name", RULES_WITH_FUTURE_DIRECTION)
def test_rules_have_future_direction_section(rule_name: str) -> None:
    text = _rule_text(rule_name)
    assert FUTURE_DIRECTION_TITLE in text, f"missing exact Future Direction title in {rule_name}"
    assert DOC_REL in text, f"missing doc cross-reference in {rule_name}"
    assert "benchmark" in text.lower(), f"missing benchmark concept in {rule_name}"


def test_tech_debt_forward_pointer(tech_debt_text: str) -> None:
    assert DOC_REL in tech_debt_text
    assert "s&p 500 total return" in tech_debt_text.lower()
