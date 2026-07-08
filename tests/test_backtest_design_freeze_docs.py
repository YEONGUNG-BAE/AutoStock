"""Guard the Phase 2b backtest design-freeze document.

These tests are docs/rules only. They do not import runtime code, read config,
fetch data, inspect live state, call KIS, or assert source-code behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FREEZE_DOC = REPO_ROOT / "docs" / "BACKTEST_DESIGN_FREEZE.md"
GATE_DOC = REPO_ROOT / "docs" / "LOCAL_EVIDENCE_GATE_REVIEW.md"
REDESIGN_DOC = REPO_ROOT / "docs" / "RULES_REDESIGN_HYPOTHESIS_V2.md"
PLAN_DOC = REPO_ROOT / "docs" / "BENCHMARK_DATA_AND_BACKTEST_PLAN.md"
SCOUT_RULE = REPO_ROOT / ".cursor" / "rules" / "04-scout-date-id-and-data.mdc"


@pytest.fixture(scope="module")
def freeze_text() -> str:
    assert FREEZE_DOC.is_file(), "backtest design freeze doc must exist"
    return FREEZE_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def gate_text() -> str:
    assert GATE_DOC.is_file(), "local evidence gate review doc must exist"
    return GATE_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def redesign_text() -> str:
    assert REDESIGN_DOC.is_file(), "rules redesign hypothesis v2 doc must exist"
    return REDESIGN_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def plan_text() -> str:
    assert PLAN_DOC.is_file(), "benchmark data and backtest plan must exist"
    return PLAN_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def scout_rule_text() -> str:
    assert SCOUT_RULE.is_file(), "scout/date-id rule file must exist"
    return SCOUT_RULE.read_text(encoding="utf-8")


def _lower(text: str) -> str:
    return text.lower()


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def _assert_tokens(text: str, tokens: tuple[str, ...]) -> None:
    lowered = _normalized(text)
    for token in tokens:
        assert _normalized(token) in lowered, f"missing concept token: {token}"


def test_freeze_doc_is_planning_only_not_runtime_spec(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "planning / future-direction",
            "not an implemented runtime spec",
            "not an active implementation requirement",
            "no engine is built",
            "no data is fetched",
            "no strategy is run",
            "no NAV track record",
            "No S&P-relative performance number",
        ),
    )


def test_evidence_tiers_and_grades(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "Tier A",
            "Asset-Class Clean Backtest",
            "near-clean long-horizon evidence",
            "Tier B",
            "Stock-Level Masked Diagnostic Backtest",
            "diagnostic / optimistic-upper-bound",
            "Tier C",
            "Forward Paper",
            "only clean validation of LLM/news/stock-selection value",
        ),
    )


def test_verified_phase_2a_context_is_encoded(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "source records -> SQLiteDateIdSourceStore -> ScoutInputBuilder",
            "BenchmarkReturnPoint",
            "KRW-unhedged",
            "S&P 500 TR USD times USDKRW",
            "BacktestInstrumentBar",
            "`date`, `as_of`, `symbol`, `market`, `close_adjusted`, and `source_name`",
            "AsOfFilteredSourceView",
            "source_timestamp <= decision_time",
            "inclusive boundary",
            "DateIdSourceRecord",
            "AllocatorDecision",
            "AssetBucket",
            "TargetWeights",
            "kr/us/gold",
            "masking leak",
            "sma_price",
            "return_bps",
            "rolling_volume",
            "vwap",
            "synthetic_factor_v1",
            "quantitative signal set is thin",
        ),
    )


def test_stock_level_survivorship_warning_and_diagnostic_labels(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "survivor-biased",
            "survivorship bias",
            "diagnostic only",
            "deployable S&P-relative alpha",
            "positive result = optimistic upper-bound",
            "negative result = stronger weakness signal",
        ),
    )
    assert "낙관적 상한선" in freeze_text
    assert "더 강한 약점 신호" in freeze_text


def test_primary_success_and_required_decomposition(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "AutoStock net terminal wealth > S&P 500 TR KRW-unhedged terminal wealth",
            "Lower MDD but lower terminal wealth is NOT a primary-objective success",
            "up-capture",
            "down-capture",
            "cash drag",
            "gold contribution",
            "KR allocation contribution",
            "cost drag",
            "net terminal wealth",
            "S&P-relative terminal wealth",
        ),
    )


def test_asset_class_first_scope_and_rules_baseline_freeze(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "First clean historical backtest = asset-class allocation/regime only",
            "no individual-stock-selection alpha claim",
            "dynamic rules-only allocator",
            "version-frozen before",
            "seeing backtest results",
            "200-day MA regime",
            "volatility targeting",
            "monthly rebalance",
            "tune -> backtest -> retune on the same history",
            "train / validation / final-holdout",
            "read the final holdout once",
        ),
    )


def test_asof_source_timestamp_and_decision_gating(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "CSV as_of -> DateIdSourceRecord.source_timestamp",
            "copy CSV `as_of` into `DateIdSourceRecord.source_timestamp`",
            "do not restamp",
            "AsOfFilteredSourceView",
            "records_allowed = source_timestamp <= d",
            "inclusive boundary",
            "feed unmodified `ScoutInputBuilder`",
            "do not modify `SQLiteDateIdSourceStore`",
            "fixed pre-open KST timestamp",
        ),
    )


def test_signal_execution_split_and_cost_execution_timing(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "Decision uses data available as_of <= decision_time",
            "Execution occurs at the next executable bar",
            "d+1",
            "just-observed close",
            "costs must be applied at execution",
            "chosen convention must be documented before producing NAV",
        ),
    )


def test_derived_features_are_asof_safe(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "Every derived feature must be computed using only data with as_of <= decision_time",
            "rolling windows",
            "expanding windows",
            "models fit only on data available as_of",
            "full-sample fitted scalers",
            "full-sample percentiles",
            "full-sample clusters",
            "full-history z-scores",
            "fitting PCA / clustering / factor models on the entire history",
        ),
    )


def test_decision_frequency_periods_per_year_and_llm_replay(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "Recommended default for LLM tiers",
            "monthly",
            "weekly LLM decision",
            "daily only if explicitly justified",
            "periods_per_year",
            "annualization",
            "information-ratio",
            "prompt",
            "input",
            "output",
            "model name",
            "model version",
            "inference settings",
            "Historical replay MUST reuse stored LLM output",
            "Do NOT re-infer during replay",
            "frozen replay phase",
        ),
    )


def test_fx_and_cost_consistency(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "Use the SAME USDKRW series",
            "S&P TR KRW conversion",
            "KRW valuation of US/Gold positions",
            "Cost model must be fixed before producing historical NAV",
            "brokerage fees",
            "Korean sell transaction tax",
            "FX spread",
            "ETF/proxy expense",
            "slippage",
            "rebalance turnover costs",
            "S&P TR index is a frictionless hurdle",
        ),
    )


def test_masked_llm_schema_note_is_future_direction(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "Design only. Do not implement in this phase",
            "`AllocatorDecision` names `kr/us/gold`",
            "leaks market identity",
            "separate anonymous schema",
            "asset_A",
            "asset_B",
            "asset_C",
            "target_weights",
            "Tier 1 does not validate current v1",
            "does not prove real LLM/news/context value",
        ),
    )


def test_phasing_and_explicit_prohibitions(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "Phase 2b-impl",
            "as_of -> source_timestamp",
            "no restamping",
            "Phase 2c",
            "asset-class clean backtest first",
            "no LLM until deterministic baselines are evaluated",
            "no unsupported expected-return probability tables",
            "no unsupported return-probability tables",
            "no personal investment advice",
            "no allocation recommendation for the user",
            "no claims that AutoStock is expected to beat S&P before evidence",
            "live KIS",
            "startup smoke",
            "paper-day pilot",
        ),
    )


def test_multi_option_decisions_record_default_alternative_and_reason(freeze_text: str) -> None:
    lowered = _lower(freeze_text)
    assert lowered.count("recommended default") >= 13
    assert lowered.count("alternative") >= 13
    assert lowered.count("reason") >= 13


def test_forward_pointers_are_future_direction_only(
    plan_text: str, scout_rule_text: str
) -> None:
    assert "docs/BACKTEST_DESIGN_FREEZE.md" in plan_text
    assert "planning / future-direction only" in _lower(plan_text)
    assert "## Future Direction — NOT ACTIVE IMPLEMENTATION REQUIREMENT" in scout_rule_text
    assert "docs/BACKTEST_DESIGN_FREEZE.md" in scout_rule_text
    assert "not an active requirement" in _lower(scout_rule_text)
    _assert_tokens(
        scout_rule_text,
        (
            "Current v1 schema/runtime",
            "Scout behavior",
            "must not change",
        ),
    )


def test_local_evidence_gate_review_scope_is_sanitized_and_local_only(
    gate_text: str,
) -> None:
    assert gate_text.startswith("# Local Evidence Gate Review")
    _assert_tokens(
        gate_text,
        (
            "local monthly KOSPI-primary evidence only",
            "repaired sibling CSV",
            "frequency-aware policy",
            "static neutral baseline evidence is included",
            "no deployment recommendation",
            "no investment recommendation",
            "do not include raw CSV rows",
            "source records",
            "source names",
            "raw FX values",
            "configuration values",
            "secrets",
        ),
    )


def test_local_evidence_gate_review_includes_phase_2e_16_values(
    gate_text: str,
) -> None:
    for value in (
        "606.8516459084041211247327601",
        "1308.719922162372568027027300",
        "-701.8682762539684469022945399",
        "-63.29150318490450077920038984",
        "1140.747516803951804075925185",
        "-167.9724053584207639511021150",
        "-48.44058973989600446483907708",
        "-533.8958708955476829511924249",
    ):
        assert value in gate_text


def test_local_evidence_gate_definitions_and_results(gate_text: str) -> None:
    _assert_tokens(
        gate_text,
        (
            "Gate 1: rules terminal return should exceed S&P 500 TR KRW benchmark terminal return",
            "Gate 2: rules terminal return should exceed static neutral baseline terminal return",
            "Gate 3: lower drawdown alone cannot override terminal wealth underperformance",
            "Gate 4: evidence must pass dataset/NAV/frequency/static-baseline sanity before interpretation",
            "Gate 1: not met",
            "Gate 2: not met",
            "terminal wealth underperformance dominates",
            "drawdown cannot rescue the result",
            "Gate 4: met for this local evidence run",
        ),
    )


def test_local_evidence_gate_interpretation_and_freeze(gate_text: str) -> None:
    _assert_tokens(
        gate_text,
        (
            "The current local rules allocator did not meet the local evidence gate",
            "underperformed both the S&P 500 TR KRW benchmark and the static neutral baseline",
            "static neutral baseline reduced the gap to the benchmark relative to the rules allocator",
            "should not proceed to paper/live deployment based on this local evidence",
            "research gate outcome, not investment advice",
            "failed local evidence candidate",
            "Do not tune weights ad hoc",
            "versioned hypothesis",
            "explicit hypothesis",
            "version bump",
            "backtest rerun",
            "comparison against the S&P benchmark and static neutral baseline",
            "evidence export",
        ),
    )


def test_local_evidence_gate_hypotheses_and_prohibitions(gate_text: str) -> None:
    _assert_tokens(
        gate_text,
        (
            "hypotheses only, not implementation instructions",
            "defensive under-participation",
            "long equity bull markets",
            "risk-off trigger",
            "benchmark-relative drag",
            "dynamic risk budget",
            "relative drawdown",
            "static neutral baseline as mandatory comparator",
            "S&P-core allocation dominance",
            "No investment recommendation",
            "No deployment approval",
            "No live/paper activation",
            "No claim that lower MDD alone is success",
            "No claim that any security should be bought or sold",
        ),
    )


def test_local_evidence_gate_bad_conclusions_are_absent(gate_text: str) -> None:
    bad_phrases = (
        "ready for deployment",
        "should be used with real capital",
        "buy s&p",
        "buy/sell any security",
    )
    lowered = gate_text.lower()
    for phrase in bad_phrases:
        assert phrase not in lowered


def test_phase_2f_0_design_freeze_update(freeze_text: str) -> None:
    _assert_tokens(
        freeze_text,
        (
            "Phase 2f-0 Local Evidence Gate Review and Redesign Freeze",
            "Phase 2e-16 produced static-baseline local evidence",
            "did not meet local terminal wealth gates versus the S&P benchmark or the static neutral baseline",
            "research gate result, not investment advice",
            "frozen as a failed local-evidence candidate",
            "Future redesign must be hypothesis/version/evidence driven",
        ),
    )


def test_rules_redesign_hypothesis_v2_context_and_identifiers(
    redesign_text: str,
) -> None:
    assert redesign_text.startswith("# Rules Redesign Hypothesis V2")
    _assert_tokens(
        redesign_text,
        (
            "Phase 2f-0 froze the current rules allocator as a failed local-evidence candidate",
            "underperformed both the S&P 500 TR KRW benchmark terminal return",
            "static neutral baseline terminal return",
            "hypothesis spec only",
            "No implementation is authorized by this document alone",
            "No deployment or investment recommendation exists",
            "failed_candidate: local_monthly_rules_allocator_v1",
            "failed_candidate_status: frozen_failed_local_evidence_candidate",
            "candidate_id: local_monthly_rules_allocator_v2_hypothesis",
            "candidate_status: hypothesis_only_not_implemented",
        ),
    )


def test_rules_redesign_hypothesis_v2_core_problem_statement(
    redesign_text: str,
) -> None:
    _assert_tokens(
        redesign_text,
        (
            "materially lower terminal wealth than both the benchmark and the static neutral baseline",
            "working hypothesis",
            "over-penalized equity participation",
            "too defensive relative to the S&P benchmark",
            "not a proven causal claim",
            "Lower drawdown cannot compensate for terminal wealth underperformance",
            "evaluated by terminal wealth first, not drawdown first",
        ),
    )


def test_rules_redesign_hypothesis_v2_h1_to_h5(redesign_text: str) -> None:
    _assert_tokens(
        redesign_text,
        (
            "H1: Increase S&P-core participation so the allocator is not structurally underweight US equity in long bull markets",
            "H2: Treat risk-off as a temporary risk-budget reduction, not a broad long-term retreat from the benchmark",
            "H3: Use benchmark-relative drawdown and benchmark-relative recovery triggers in addition to absolute trend signals",
            "H4: Keep static neutral baseline as a mandatory comparator for every future evidence run",
            "H5: Any tactical overlay must improve terminal wealth after costs, not merely reduce volatility",
        ),
    )


def test_rules_redesign_hypothesis_v2_non_goals_and_envelope(
    redesign_text: str,
) -> None:
    _assert_tokens(
        redesign_text,
        (
            "No stock selection",
            "No LLM/news signal yet",
            "No live/paper deployment",
            "No tuning weights on the same evidence without a versioned protocol",
            "No changing USDKRW/data repair logic",
            "No changing benchmark metric math",
            "No weakening dataset/NAV sanity gates",
            "No claim that lower MDD alone is success",
            "S&P-core dominant baseline",
            "Tactical satellite overlays",
            "Risk-off capped by explicit relative-performance guard",
            "Maximum cash/gold drag budget",
            "Required minimum US-equity participation unless evidence supports otherwise",
            "Relative drawdown recovery logic",
            "Cost-aware turnover limits",
            "Do not assign final weights in this phase",
            "Do not implement any of these directions in this phase",
        ),
    )


def test_rules_redesign_hypothesis_v2_acceptance_gates(redesign_text: str) -> None:
    _assert_tokens(
        redesign_text,
        (
            "Gate A: v2 terminal return must exceed S&P 500 TR KRW benchmark terminal return",
            "Gate B: v2 terminal return must exceed static neutral baseline terminal return",
            "Gate C: v2 terminal excess return must exceed rules v1 terminal excess return",
            "Gate D: v2 must pass dataset continuity, NAV sanity, frequency-aware metrics, and static baseline evidence export",
            "Gate E: v2 must not rely on lower drawdown alone to claim success",
            "Gate F: v2 must report costs, turnover proxy if available, max relative drawdown, and rules-minus-static comparisons",
        ),
    )


def test_rules_redesign_hypothesis_v2_anti_overfit_and_prohibitions(
    redesign_text: str,
) -> None:
    _assert_tokens(
        redesign_text,
        (
            "No repeated ad hoc retuning on the same repaired local evidence",
            "Any parameter change requires a version bump",
            "Any parameter change requires a stated hypothesis",
            "Rerun must compare against the S&P benchmark and static neutral baseline",
            "Final evaluation must be documented before paper/live consideration",
            "validation/holdout split",
            "No investment recommendation",
            "No deployment approval",
            "No live/paper activation",
            "No claim that any security should be bought or sold",
            "No claim that lower drawdown alone is success",
            "No claim that the v2 hypothesis is expected to beat S&P before evidence",
        ),
    )


def test_phase_2f_1_design_freeze_and_gate_review_pointer(
    freeze_text: str,
    gate_text: str,
) -> None:
    _assert_tokens(
        freeze_text,
        (
            "Phase 2f-1 Versioned Rules Redesign Hypothesis Spec",
            "hypothesis-only v2 redesign spec",
            "Current v1 remains frozen as a failed local-evidence candidate",
            "No implementation is authorized yet",
            "Future implementation must be versioned and evidence-gated against the S&P benchmark and static neutral baseline",
        ),
    )
    _assert_tokens(
        gate_text,
        (
            "docs/RULES_REDESIGN_HYPOTHESIS_V2.md",
            "this pointer does not change the local gate outcome",
        ),
    )
