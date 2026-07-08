# Backtest Design Freeze

This is a PLANNING / FUTURE-DIRECTION document.
It is NOT an implemented runtime spec.
It is NOT an active implementation requirement.
No engine is built by this document.
No data is fetched by this document.
No strategy is run by this document.
No NAV track record or S&P-relative performance number is produced by this document.

This document freezes the evaluation design that a later explicit implementation
phase must follow. It does not authorize engine, loader-conversion, masking,
schema, runtime, config, broker, allocator, risk, order, scout, or live/paper
execution changes.

## 1. Purpose and Non-Goals

Freezing the design before writing the walk-forward engine keeps the evaluation
question stable. The purpose is bias control, reproducibility, and avoiding
self-deception: look-ahead bias, survivorship bias, overfitting, FX distortion,
and omitted costs can otherwise enter through small implementation choices. The
future engine must answer the frozen question rather than silently changing the
question after seeing results.

Recommended default: freeze the evidence tiers, timing rules, scoring objective,
cost/FX treatment, and replay protocol before engine code exists. Alternative:
let the engine implementation choose these rules as it is written. Reason: the
alternative makes it too easy to tune the experiment around observed results.

Non-goals:

- no walk-forward engine
- no real data fetch, yfinance use, FRED use, network use, or loader behavior
  change
- no loader-to-`DateIdSourceRecord` conversion implementation
- no LLM evaluation, LLM input masking, or anonymous schema implementation
- no Allocator v2, `AllocationRegime` wiring, `AllocatorDecision` schema change,
  runtime/schema/config change, or live/paper execution behavior change
- no NAV track record and no S&P-relative performance number
- no personal investment advice, allocation recommendation, unsupported
  return-probability tables, or unsupported expected-return probability tables

The current verified pipeline remains:

```text
source records -> SQLiteDateIdSourceStore -> ScoutInputBuilder (read-only) -> allocator -> risk -> PaperBroker -> NAV
```

Verified Phase 2a context this freeze assumes:

- `BenchmarkReturnPoint` is KRW-unhedged, scoring-only, and generated from S&P
  500 TR USD times USDKRW.
- `BacktestInstrumentBar` preserves `date`, `as_of`, `symbol`, `market`,
  `close_adjusted`, and `source_name`; it is generic for proxy rows or
  individual security rows.
- `AsOfFilteredSourceView` is read-only, returns only
  `source_timestamp <= decision_time`, uses an inclusive boundary, and composes
  with unmodified `ScoutInputBuilder`.
- `DateIdSourceRecord` uses `source_timestamp` as the timestamp field.
- Current `AllocatorDecision` names KR/US/GOLD buckets:
  `AssetBucket` KR/US/GOLD and `TargetWeights` `kr/us/gold`. These market names
  are a masking leak for anonymized LLM comparisons.
- Current indicators are only `sma_price`, `return_bps`, `rolling_volume`, and
  `vwap`; the quantitative signal set is thin.
- The KR factor source is synthetic: `synthetic_factor_v1`.

## 2. Evidence Tiers

### Tier A - Asset-Class Clean Backtest

Scope:

- S&P 500 TR KRW-unhedged
- static neutral baseline
- dynamic rules-only allocator, if version-frozen before results
- current-v1 allocation approximation if feasible

Grade: near-clean long-horizon evidence.

Validates:

- allocation/regime layer
- up-capture
- down-capture
- cash drag
- gold contribution/hedge contribution
- KR allocation contribution
- structural beta
- turnover/cost drag

Does not validate:

- stock selection
- individual security alpha
- LLM/news/context value

Recommended default: run this as the first clean historical tier. Alternative:
begin with individual-stock replay. Reason: asset-class allocation is where the
current structural questions live, while stock-level history needs
survivorship-clean point-in-time data before it can support deployable alpha
claims.

### Tier B - Stock-Level Masked Diagnostic Backtest

Scope:

- same universe
- same features
- same `as_of` cutoff
- same rebalance schedule
- same cost model
- same constraints
- rules-only vs masked-numeric LLM

Grade: diagnostic / optimistic-upper-bound only.

Rules:

- may use a survivor-biased universe if point-in-time universe is unavailable
- must be labeled as survivor-biased or potentially survivor-biased
- must not be read as clean deployable alpha
- positive result = optimistic upper-bound / possibility signal
- negative result = stronger weakness signal
- survivorship bias need not act symmetrically on rules vs LLM because style
  tilt can favor one side

Mandatory report warning text:

English:

```text
This stock-level historical replay uses an incomplete and potentially survivor-biased universe unless explicitly proven otherwise. Results are diagnostic only. They must not be interpreted as clean evidence of deployable S&P-relative alpha. Positive results are optimistic upper-bound evidence; negative results are stronger evidence of weakness.
```

Korean:

```text
이 종목-level historical replay는 point-in-time 전체 종목 universe가 입증되지 않는 한 survivorship bias 가능성이 있다. 따라서 결과는 diagnostic으로만 해석한다. 양호한 결과는 낙관적 상한선이고, 부진한 결과는 더 강한 약점 신호다.
```

Recommended default: use Tier B only after Tier A and label it diagnostic.
Alternative: treat masked stock-level replay as primary evidence. Reason: a
survivor-biased or potentially survivor-biased universe can overstate historical
stock-selection value and can favor rules or LLM unevenly.

### Tier C - Forward Paper

Scope:

- real symbols
- real markets
- sectors
- news/context if enabled
- actual LLM behavior
- real pipeline shape
- operational latency
- paper execution assumptions

Grade: only clean validation of LLM/news/stock-selection value, but
short-horizon and possibly single-regime.

Recommended default: use forward paper to validate a frozen strategy after
deterministic baselines. Alternative: use forward paper as a substitute for
long-horizon historical evidence. Reason: forward paper is operationally real
but too short and regime-dependent to replace a long-horizon backtest.

## 3. Primary Objective and Scoring

Primary success:

```text
AutoStock net terminal wealth > S&P 500 TR KRW-unhedged terminal wealth
```

Net means:

- costs included
- taxes/fees/spreads/proxy expenses/slippage modeled before any historical NAV
  claim
- FX conversion consistent

Secondary diagnostics:

- lower MDD
- lower volatility
- lower down-capture
- better risk-adjusted return
- lower relative drawdown
- better information ratio

Explicit rule:

```text
Lower MDD but lower terminal wealth is NOT a primary-objective success.
```

Every clean backtest report must decompose:

- up-capture
- down-capture
- cash drag
- gold contribution / hedge contribution
- KR allocation contribution
- turnover / cost drag
- net terminal wealth
- S&P-relative terminal wealth

Reuse Phase 1 benchmark-relative metrics and the Phase 1.5 renderer. Do not
rebuild them in this phase.

Recommended default: score primary success by net terminal wealth against S&P
500 TR KRW-unhedged, with diagnostics reported separately. Alternative: promote
lower MDD or volatility to primary success. Reason: the clarified objective is
wealth relative to the S&P hurdle, while risk diagnostics explain the path.

## 4. Backtest Scope Decision

Frozen scope:

```text
First clean historical backtest = asset-class allocation/regime only.
```

Details:

- proxies for KR / US / Gold plus cash / FX
- no individual-stock-selection alpha claim from this tier
- stock selection is evaluated through stock-level masked diagnostic replay with
  downgraded evidence grade, and through forward paper with real
  pipeline/context

Rationale:

- survivorship-clean point-in-time single-stock data is costly and hard
- asset-class layer is where this project's structural drag lives
- existing concerns about cash/gold/KR drag, up-capture, and low beta are
  allocation/regime-layer concerns

Recommended default: implement asset-class allocation/regime first. Alternative:
make individual security replay the first historical engine target. Reason: the
alternative risks reporting stock-selection claims before point-in-time universe
quality is solved.

## 5. Rules-Only Baseline Freeze Protocol

A dynamic rules-only allocator must be specified and version-frozen before
seeing backtest results.

Prefer literature/common-sense simple rules:

- 200-day MA regime
- volatility targeting
- monthly rebalance
- cash/gold caps
- cost inclusion

Prohibited iterative loop:

```text
tune -> backtest -> retune on the same history
```

If tuning is needed, require a train / validation / final-holdout split. Example:
design on the early window, validate on the middle window, and read the final
holdout once at the end. Repeatedly peeking at validation also overfits.

Every parameter change requires:

- written hypothesis
- version bump
- rerun record

Recommended default: freeze simple rules before results, with train /
validation / final-holdout discipline only when tuning is unavoidable.
Alternative: tune parameters against the full replay period. Reason: full-period
tuning converts the backtest into a fitting exercise instead of evidence.

## 6. As-Of / Look-Ahead Invariants

### Source Invariant

When loader output / instrument bars are converted to `DateIdSourceRecord`:

```text
CSV as_of -> DateIdSourceRecord.source_timestamp
```

Rules:

- copy CSV `as_of` into `DateIdSourceRecord.source_timestamp`
- do not restamp with load time
- do not restamp with ingest time
- do not restamp with current time
- restamping silently defeats Phase 2a `AsOfFilteredSourceView`

Recommended default: preserve source `as_of` exactly as `source_timestamp`.
Alternative: stamp conversion or ingest time. Reason: restamping moves the
availability boundary and creates look-ahead leakage.

### Decision Gating

At decision time `d`:

```text
records_allowed = source_timestamp <= d
```

Rules:

- use Phase 2a `AsOfFilteredSourceView`
- inclusive boundary
- feed unmodified `ScoutInputBuilder`
- do not modify `ScoutInputBuilder`
- do not modify `SQLiteDateIdSourceStore`

Recommended default: compose the Phase 2a read-only guard with unmodified
builder/store behavior. Alternative: add filtering inside `ScoutInputBuilder` or
the store. Reason: the existing guard is already the boundary and keeps runtime
components unchanged.

### Fixed Decision Timestamp

Canonical decision timestamp default:

- monthly decision
- first eligible trading day
- fixed pre-open KST timestamp

Alternative: weekly or daily decision for rules-only/static tiers if explicitly
justified.

Reason: cross-market NYSE/KRX/FX close mismatches must be resolved
deterministically by `as_of`, not by calendar-date assumptions.

## 7. Signal-Date vs Execution-Date Rule

Decisions and executions are separated:

```text
Decision uses data available as_of <= decision_time.
Execution occurs at the next executable bar.
```

Examples:

- decision at `d` pre-open KST
- execute at `d` open if the target market is not yet open and all used data was
  available before the decision
- otherwise execute at `d+1` open or `d+1` close, depending on the chosen
  convention

The chosen convention must be documented before producing NAV.

Prohibited:

- using a just-observed close to trade at that same close
- applying costs after scoring instead of at execution

Costs must be applied at execution.

Recommended default: execute at the next executable bar after the decision and
source-availability checks. Alternative: same-date execution at a close.
Reason: same-close execution is only valid if the close was not also the signal
observation; otherwise it trades on information unavailable at that price.

## 8. Derived-Feature As-Of Safety

All derived features must be as-of safe.

Applies to:

- percentile
- z-score
- volatility
- rank
- clustering
- factor exposure
- beta
- sector or style grouping
- any future feature-rich masked LLM tier

Rule:

```text
Every derived feature must be computed using only data with as_of <= decision_time.
```

Allowed:

- rolling windows
- expanding windows
- models fit only on data available as_of the decision time

Prohibited:

- full-sample fitted scalers
- full-sample percentiles
- full-sample clusters
- full-history z-scores
- fitting PCA / clustering / factor models on the entire history before replay

Reason: full-sample fitting leaks future distribution into past features.

Recommended default: rolling or expanding feature computation at each decision
time. Alternative: precompute full-history scalers, clusters, or percentiles.
Reason: the alternative leaks future distributional information into earlier
decisions.

## 9. Decision Frequency and LLM Cost/Replay

Recommended default for LLM tiers:

- monthly

Reason:

- about 240 decisions over 20 years
- daily is about 5,000 decisions over 20 years
- monthly better matches allocation/regime judgment
- lower turnover
- cheaper/faster
- easier replay and audit

Alternative:

- weekly LLM decision if monthly is too sparse
- daily only if explicitly justified and cost/replay burden is accepted

Rules-only/static tiers:

- may use daily, weekly, monthly, quarterly, or band rebalancing
- must specify `periods_per_year`
- annualization and information-ratio calculations must match chosen frequency

LLM evidence persistence:

First inference run must persist:

- prompt
- input
- output
- model name
- model version or local checkpoint identifier
- inference settings
- timestamp
- code/config version
- any masking/feature adapter version

Replay rule:

```text
Historical replay MUST reuse stored LLM output.
Do NOT re-infer during replay.
```

Separate:

- inference generation phase
- frozen replay phase

Reason:

- model stochasticity
- version drift
- local inference non-determinism
- reproducibility

Recommended default: monthly LLM decisions with persisted outputs and frozen
replay. Alternative: re-infer during each replay run. Reason: re-inference
changes the decision series and destroys reproducibility.

## 10. FX and Cost Consistency

FX rule:

```text
Use the SAME USDKRW series for S&P TR KRW conversion and for KRW valuation of US/Gold positions.
```

Reason: mismatched FX series creates fake alpha or fake drag.

Cost model must be fixed before producing historical NAV.

Cost model must include, as applicable:

- brokerage fees
- Korean sell transaction tax
- FX spread
- ETF/proxy expense
- slippage
- rebalance turnover costs

Scoring convention:

- S&P TR index is a frictionless hurdle
- implementable portfolios, including static baseline and AutoStock, include
  rebalancing costs

Static baseline must document rebalance rule:

- monthly
- quarterly
- or band-based

Recommended default: use one USDKRW series for benchmark conversion and
position valuation, and freeze all applicable costs before NAV. Alternative:
mix FX sources or add costs after scoring. Reason: either choice can create fake
relative performance.

## 11. Masked LLM Comparison Design Note

Design only. Do not implement in this phase.

Current issue:

- `AllocatorDecision` names `kr/us/gold`
- therefore it leaks market identity even if input data is anonymized

Future Tier-1 masked comparison needs a separate anonymous schema, for example:

```text
asset_A
asset_B
asset_C
cash
```

or a generic list:

```text
target_weights:
  - asset_id: asset_A
    weight: ...
  - asset_id: asset_B
    weight: ...
```

Rules:

- rules-only and LLM must receive the same anonymized information from the same
  canonical feature table
- Tier 1 measures whether LLM beats rules on anonymized numeric patterns
- Tier 1 does not validate current v1
- Tier 1 does not prove real LLM/news/context value
- do not conflate masked diagnostic success with deployable v1 alpha

Recommended default: treat anonymous schema as a later future-direction task.
Alternative: reuse current `kr/us/gold` schema for masked LLM comparison.
Reason: current market names leak identity and would invalidate masking.

## 12. Phasing After This Freeze

### Phase 2b-impl

Loader-to-`DateIdSourceRecord` conversion contract:

- `as_of -> source_timestamp`
- no restamping
- no runtime wiring unless explicitly scoped
- no strategy execution

### Phase 2c

Walk-forward engine:

- asset-class clean backtest first
- NAV track record generation
- benchmark-relative report using existing metrics plus renderer
- cost model applied
- signal/execution timing enforced
- no LLM until deterministic baselines are evaluated

### Later LLM tiers

Only after deterministic baselines:

- masked numeric diagnostic
- optional feature-rich masked diagnostic
- forward paper remains real validation

Decision rule:

- if LLM cannot beat static/rules baseline, reduce/remove LLM rather than
  elaborate it

Forward paper:

- operational/forward validation of a frozen strategy
- not a substitute for long-horizon historical evidence

Recommended default: Phase 2b-impl conversion contract, then Phase 2c
asset-class engine, then later LLM tiers. Alternative: implement LLM and masking
before deterministic baselines. Reason: deterministic baselines are the minimum
evidence floor.

## 13. Explicit Prohibitions Carried Forward

- no unsupported expected-return probability tables
- no unsupported return-probability tables
- no personal investment advice
- no allocation recommendation for the user
- no claims that AutoStock is expected to beat S&P before evidence
- operational safety gates remain strict and separate from investment objective
- live KIS, live order, startup smoke, paper-day pilot, activation, daemon, and
  automatic restart remain operator-only and out of scope for this document

Recommended default: keep this document as a design freeze only until a later
task explicitly asks for implementation. Alternative: treat this document as
permission to begin engine, masking, schema, runtime, or data-fetch work.
Reason: the alternative would violate the phase boundary and blur design with
implementation.

### Frozen Rules-Only Baseline v1

Version id: `rules_allocator.v1`.

Algorithm:

- for each generic non-cash asset row, if `current_price >= long_ma`, use
  `risk_on_weight`
- otherwise use `risk_off_weight`
- clamp that preliminary weight to `[min_weight, max_weight]`
- assign the residual `1 - sum(non_cash_bounded_weights)` to cash
- reject the input if the residual cash weight is below `cash_min_weight`

Default synthetic fixture parameters for tests only:

- `asset_A`: `risk_on_weight = 0.70`, `risk_off_weight = 0.35`,
  `min_weight = 0`, `max_weight = 0.80`
- `asset_B`: `risk_on_weight = 0.15`, `risk_off_weight = 0.05`,
  `min_weight = 0`, `max_weight = 0.25`
- `asset_C`: `risk_on_weight = 0.10`, `risk_off_weight = 0.10`,
  `min_weight = 0`, `max_weight = 0.20`
- cash: `cash_min_weight = 0.05`

This baseline is not investment advice and is not an allocation recommendation.
No backtest results have been seen for this frozen rule. Any change requires a
version bump, written hypothesis, and rerun record.

### Phase 2c-1 Snapshot Builder Contract

The Phase 2c-1 snapshot builder consumes already-converted source records or a
read-only source reader, wraps them with the Phase 2a `AsOfFilteredSourceView`,
and builds exactly one `BacktestFeatureSnapshot` for one `decision_time`.

Rules:

- only records with `source_timestamp <= decision_time` are visible
- the builder reads only `FactType.PRICE`
- tests convert the whole synthetic input slice once before as-of slicing
- missing visible data raises `ValueError` instead of forward-filling
- malformed payloads, wrong schema names, and symbol/market mismatches fail
  fast
- `long_ma` comes from asset config and is not computed here
- rolling feature calculation belongs to a later phase
- no loop over dates is implemented
- no execution, NAV, or benchmark-relative metrics are produced
- output is suitable for `rules_allocator.v1`

### Phase 2c-2 Rolling Feature Contract

The Phase 2c-2 rolling feature builder computes `long_ma` from source records
for one `decision_time` and returns `SnapshotAssetConfig` objects suitable for
the Phase 2c-1 snapshot builder.

Rules:

- use the Phase 2a `AsOfFilteredSourceView`
- use only records with `source_timestamp <= decision_time`
- read only `FactType.PRICE`
- compute a simple observation-count moving average from the latest visible
  `lookback_count` observations
- no full-sample fitting, normalization, scaling, clustering, percentiles, or
  factor fitting
- no forward-fill and no interpolation
- insufficient visible history raises `ValueError`
- no single-step decision artifact is produced
- no allocator execution is performed
- no loop over dates is implemented
- no execution, NAV, or benchmark-relative metrics are produced

### Phase 2c-2a Count-Based MA Observation Spacing Guard

Phase 2c-2 rolling MA is count-based. A count-based MA only has the intended
monthly meaning when input observations are uniformly monthly, so Phase 2c-2a
adds a fail-fast spacing precondition guard.

Rules:

- validate the latest `lookback_count` visible observations for one
  `decision_time`
- monthly period key comes from payload `date` as `YYYY-MM`
- duplicate monthly periods fail
- skipped monthly periods fail
- insufficient visible history fails
- no forward-fill and no interpolation
- no time-window MA is implemented yet
- no loop over dates is implemented
- no execution, NAV, or benchmark-relative metrics are produced
- a future time-window MA may be added later as a separate explicit phase if
  needed

### Phase 2c-3 Single-Step Rules Decision Contract

Phase 2c-3 composes the already-frozen single-step pieces for exactly one
`decision_time` and records one later `intended_execution_time`.

Rules:

- validate count-based monthly observation spacing before rolling MA
- build rolling `SnapshotAssetConfig` values for the same `decision_time`
- build one as-of-safe `BacktestFeatureSnapshot`
- allocate with `rules_allocator.v1`
- official builder API is `make_rules_only_single_step_decision`
- return one immutable `BacktestSingleStepDecision` with top-level
  `allocator_version`
- require top-level `allocator_version` to match nested target weights and be
  `rules_allocator.v1`
- require `decision_time < intended_execution_time`
- no loop over multiple decision dates is implemented
- no execution price, fills, costs, slippage, FX spread, tax, holdings state,
  cash ledger, NAV, or benchmark-relative metrics are produced

### Phase 2c-4 Execution Price Selection Contract

Phase 2c-4 selects executable prices for one already-built
`BacktestSingleStepDecision`. For each non-cash asset it selects the first
valid price record whose `source_timestamp` is at or after
`intended_execution_time`.

Rules:

- input is one `BacktestSingleStepDecision` plus read-only source records
- output is one immutable `BacktestExecutionPriceSlice`
- the public API is `select_execution_prices_for_single_step_decision`
- the policy string is
  `first_visible_price_at_or_after_intended_execution_time.v1`
- select the first valid price at or after `intended_execution_time`
- relies on Phase 2c-3 enforcing `decision_time < intended_execution_time`, so
  same-decision-time prices can never be used as execution prices
- selects prices for non-cash assets only; cash has no execution price
- preserves asset order from `decision.snapshot_asset_configs`
- ties at the same earliest `source_timestamp` break deterministically by max
  `(date_id.value, source_name)`
- a missing future executable price raises `ValueError`
- does not use `AsOfFilteredSourceView`; this selector looks forward from the
  intended execution timestamp
- does not execute trades, produce fills, compute quantities, compute costs,
  maintain holdings or a cash ledger, produce NAV, or compute
  benchmark-relative metrics
- does not fetch or use real data

### Phase 2c-5 Single-Rebalance Accounting Contract

Phase 2c-5 applies one already-built ``BacktestSingleStepDecision`` and one
``BacktestExecutionPriceSlice`` to one ``BacktestPortfolioState`` using an
explicit ``usdkrw_rate`` and ``BacktestCostModel``.

Rules:

- input is one decision, one execution price slice, one portfolio state, one
  cost model, and one explicit ``usdkrw_rate``
- output is one immutable ``BacktestRebalanceResult``
- the public API is ``apply_single_rebalance_accounting``
- the accounting policy string is ``single_rebalance_target_weight_accounting.v1``
- the cost model version string is
  ``simple_proportional_fee_sell_tax_fx_spread.v1``
- computes one rebalance only from ``decision.target_weights`` and selected
  execution prices
- fee applies to all buy and sell traded KRW notional
- KR sell tax applies only to KR sells
- FX spread applies to US/GOLD trades
- computes post-trade holdings, cash, and one post-trade portfolio value
- post-trade portfolio value equals pre-trade value minus total costs within
  exact Decimal arithmetic
- does not implement a walk-forward loop
- does not produce a NAV series
- does not compute benchmark-relative metrics
- does not fetch or use real data

### Phase 2c-6 One-Period Step Contract

Phase 2c-6 composes exactly one period from already-frozen building blocks:

``source records -> single-step rules decision -> execution price selection
-> single-rebalance accounting -> next portfolio state``.

Rules:

- input is one read-only source, one ``decision_time``, one
  ``intended_execution_time``, one ``BacktestPortfolioState``, one
  ``BacktestCostModel``, one explicit ``usdkrw_rate``, and rolling asset
  configs
- output is one immutable ``BacktestSinglePeriodStepResult``
- the public API is ``run_single_period_rules_rebalance_step``
- the period step policy string is ``single_period_rules_rebalance_step.v1``
- calls ``make_rules_only_single_step_decision`` for the signal-side decision
- calls ``select_execution_prices_for_single_step_decision`` for execution prices
- calls ``apply_single_rebalance_accounting`` for trades and costs
- builds ``next_portfolio_state`` from rebalance post-trade cash and holdings
  with ``as_of = intended_execution_time``
- does not recompute target weights, execution prices, or trades beyond the
  existing building blocks
- does not implement a walk-forward loop
- does not generate a rebalance schedule
- does not produce a NAV series
- does not compute benchmark-relative metrics
- does not fetch or use real data

### Phase 2c-7 Explicit-Schedule Walk-Forward NAV Contract

Phase 2c-7 repeats the frozen Phase 2c-6 one-period step over an explicit
schedule, carries portfolio state forward, and produces AutoStock-only NAV
points.

Rules:

- input is read-only source records, explicit ``BacktestPeriodSpec`` values,
  one ``initial_portfolio_state``, rolling asset configs, one
  ``BacktestCostModel``, and cash settings
- output is one immutable ``BacktestWalkForwardResult``
- the public API is ``run_explicit_schedule_rules_walk_forward_nav``
- the walk-forward policy string is
  ``explicit_schedule_rules_walk_forward_nav.v1``
- repeats ``run_single_period_rules_rebalance_step`` once per supplied period
- carries ``next_portfolio_state`` forward as the next period input state
- produces one post-trade ``BacktestNavPoint`` per period with
  ``portfolio_value_krw = post_trade_portfolio_value_krw``
- uses explicit per-period ``usdkrw_rate`` from each period spec
- the schedule is supplied by the caller; it is not generated from calendars
- does not load real data
- does not compute benchmark-relative metrics
- does not compare with S&P
- does not render reports
- benchmark-relative scoring is deferred to a later phase

### Phase 2c-8 Benchmark Adapter Contract

Phase 2c-8 adapts AutoStock-only walk-forward ``BacktestNavPoint`` values and
explicit ``BenchmarkReturnPoint`` inputs to existing Phase 1
``BenchmarkRelativeMetrics``.

Rules:

- input is one ``BacktestWalkForwardResult`` and explicit benchmark return
  points supplied by the caller
- output is one immutable ``BacktestBenchmarkRelativeResult``
- the public API is ``compute_walk_forward_benchmark_relative_metrics``
- the benchmark adapter policy string is
  ``walk_forward_nav_to_benchmark_relative_metrics.v1``
- uses NAV point ``as_of.date()`` as the strategy calendar date
- uses NAV point ``portfolio_value_krw`` as the strategy total-return level
- aligns strategy and benchmark on common calendar dates only
- no forward-fill, back-fill, or interpolation for missing dates
- requires at least 2 common calendar dates
- rejects duplicate strategy or benchmark calendar dates
- calls existing ``compute_benchmark_relative_metrics``; does not duplicate
  benchmark-relative math
- benchmark points are explicit inputs; no benchmark data is loaded or fetched
- does not run walk-forward execution
- does not render markdown reports
- does not produce investment conclusions or S&P beat/lose claims
- does not fetch or use real data

### Phase 2c-9 Benchmark-Relative Report Bundle Contract

Phase 2c-9 wraps an already-computed ``BacktestBenchmarkRelativeResult`` and
calls the existing Phase 1.5 markdown renderer.

Rules:

- input is one ``BacktestBenchmarkRelativeResult`` supplied by the caller
- output is one immutable ``BacktestEvaluationReportBundle``
- the public API is ``render_backtest_evaluation_report_bundle``
- the report bundle policy string is
  ``benchmark_relative_metrics_markdown_bundle.v1``
- passes ``benchmark_relative_result.metrics`` to existing
  ``render_benchmark_relative_metrics_markdown`` exactly once
- does not compute benchmark-relative metrics
- does not run walk-forward
- does not load or fetch benchmark data
- does not load or fetch strategy data
- does not write report files or create artifacts
- does not add project-level investment conclusions beyond the existing
  renderer output
- real-data execution and persisted report artifacts are deferred to a later
  explicit phase

### Phase 2c-10 Synthetic Evaluation Pipeline Contract

Phase 2c-10 composes explicit synthetic source records, period specs, rolling
asset configs, initial portfolio state, cost model, cash settings, and explicit
``BenchmarkReturnPoint`` inputs into one ``BacktestEvaluationPipelineResult``.

Rules:

- input is read-only source records, explicit period specs, rolling asset
  configs, initial portfolio state, cost model, cash settings, and explicit
  benchmark return points supplied by the caller
- output is one immutable ``BacktestEvaluationPipelineResult``
- the public API is ``run_explicit_synthetic_backtest_evaluation_pipeline``
- the evaluation pipeline policy string is
  ``explicit_synthetic_walk_forward_benchmark_report_pipeline.v1``
- calls existing ``run_explicit_schedule_rules_walk_forward_nav`` for
  walk-forward NAV
- calls existing ``compute_walk_forward_benchmark_relative_metrics`` for
  benchmark-relative metrics
- calls existing ``render_backtest_evaluation_report_bundle`` for markdown
  report bundle output
- does not load or fetch real data
- does not generate rebalance schedule
- does not fetch benchmark data
- does not write report files or create artifacts
- does not add project-level investment conclusions
- real-data backtest execution is deferred to a later explicit phase

### Phase 2d-0 Local Data Preflight Contract

Phase 2d-0 validates the operator-local sibling data directory layout and
monthly CSV metadata before any real-data backtest execution.

Rules:

- local real-data directory must be outside the git repo
- default data root is sibling ``repo_root.parent / "autostock-data"``
- checks CSV existence, schema, monthly period coverage, symbol/market
  uniqueness, and ``as_of`` timestamp sanity
- returns metadata and warnings only
- does not execute backtest
- does not compute NAV
- does not compute benchmark-relative metrics
- does not render reports
- does not fetch or download data
- does not commit real data
- real-data backtest execution is deferred to a later explicit phase

### Phase 2d-1 Local Monthly Dataset Assembly Contract

Phase 2d-1 reads operator-local monthly CSV files from the sibling
``autostock-data`` directory and assembles in-memory backtest inputs.

Rules:

- local monthly CSVs are read only from sibling ``autostock-data``
- default data root is ``repo_root.parent / "autostock-data"``
- assembles ``DateIdSourceRecord`` source records
- assembles KRW-unhedged S&P benchmark points from SP500TR * USDKRW
- computes common periods across all instruments and benchmark alignment
- no forward-fill, back-fill, or interpolation for benchmark alignment
- KOSPI primary is a KR proxy, not implementable ETF evidence
- does not run backtest
- does not compute NAV
- does not compute benchmark-relative metrics
- does not render reports
- does not fetch or download data
- does not commit real data
- real-data backtest execution is deferred to a later explicit phase

### Phase 2d-2 Local Monthly Run Config Contract

Phase 2d-2 builds a frozen KOSPI-primary monthly run configuration from an
assembled local monthly dataset.

Rules:

- builds frozen KOSPI-primary monthly run config from assembled local dataset
- uses explicit common periods only
- first common period is warm-up baseline for initial portfolio state
- the first ``rolling_lookback_count`` common periods are signal warm-up
- first execution starts only after enough visible rolling observations exist
- for lookback 3, first execution uses the 4th common period
- ``period_specs`` count equals ``len(common_periods) - rolling_lookback_count``
- creates explicit ``BacktestPeriodSpec`` values after signal warm-up
- uses previous period timestamp for decision and current period timestamp for
  execution
- uses current period USDKRW rate
- creates rolling configs, initial portfolio, cost model, and cash settings
- ``asset_us`` and benchmark may both read ``monthly/sp500tr_monthly.csv``
- KOSPI primary is KR proxy, not implementable ETF evidence
- does not read CSVs directly
- does not run backtest
- does not compute NAV
- does not compute benchmark-relative metrics
- does not render reports
- does not fetch or download data
- does not commit real data
- real-data backtest execution is deferred to a later explicit phase

### Phase 2d-3 Local Real-Data Evaluation Dry-Run Contract

Phase 2d-3 introduces the first explicit operator-local real-data evaluation
dry-run runner.

Rules:

- reads local monthly CSVs only from sibling ``autostock-data``
- assembles dataset through ``assemble_local_monthly_dataset(...)``
- builds frozen KOSPI-primary run config through
  ``build_kospi_primary_monthly_run_config(...)``
- runs walk-forward NAV in memory through
  ``run_explicit_schedule_rules_walk_forward_nav(...)``
- computes benchmark-relative metrics in memory through
  ``compute_walk_forward_benchmark_relative_metrics(...)``
- renders markdown report bundle in memory through
  ``render_backtest_evaluation_report_bundle(...)``
- does not write files or artifacts
- does not fetch or download data
- does not commit real data
- does not call ``run_explicit_synthetic_backtest_evaluation_pipeline(...)``
- KOSPI primary is KR proxy, not implementable ETF evidence
- ``asset_us`` and benchmark may both read ``monthly/sp500tr_monthly.csv``
- result is research evidence only
- no project-level investment conclusion or S&P-beat claim

### Phase 2d-3a Warm-Up-Safe Local Dry-Run Patch

Phase 2d-3a corrects local dry-run warm-up semantics so default KOSPI-primary
evaluation succeeds with normal one-row-per-period monthly CSVs.

Rules:

- the first ``rolling_lookback_count`` common periods are signal warm-up
- first execution starts only after enough visible rolling observations exist
- for lookback 3, first execution uses the 4th common period
- this avoids insufficient visible observations without duplicating rows
- ``asset_us`` and benchmark may both read ``monthly/sp500tr_monthly.csv``
- KOSPI primary is KR proxy, not implementable ETF evidence
- result is research evidence only
- no project-level investment conclusion or S&P-beat claim

### Phase 2d-4 Operator Local Dry-Run CLI Contract

Phase 2d-4 adds an operator command-line entry point for the local monthly
real-data evaluation dry-run.

Rules:

- CLI runs local dry-run from sibling ``autostock-data``
- default data root is sibling ``repo_root.parent / "autostock-data"``
- CLI calls ``run_local_monthly_evaluation_dry_run(...)``; it does not
  reimplement the dry-run pipeline
- CLI prints sanitized summary only to stdout
- full markdown report is not printed by default
- optional ``--show-markdown-preview`` prints at most the first 20 lines of
  the in-memory markdown report
- no report files or artifacts are written
- no fetch or download
- no raw CSV rows
- no config or secrets read or printed
- rejected output/write/fetch/live/paper CLI args exit nonzero
- result is research evidence only
- no project-level investment conclusion or S&P-beat claim

### Phase 2d-5 Sanitized Local Evidence Export Contract

Phase 2d-5 adds an explicit opt-in writer for sanitized local dry-run evidence
bundles.

Rules:

- export is opt-in only through ``--export-output-root``
- without ``--export-output-root``, CLI remains summary-only and writes no files
- default output root is sibling ``repo_root.parent / "autostock-data" / "outputs"``
- output root must be outside the repository
- export accepts an already-computed ``LocalMonthlyEvaluationDryRunResult`` and
  does not rerun the dry-run
- writes exactly three files: summary markdown, metrics JSON, manifest JSON
- no raw CSV rows
- no raw source records
- no config or secrets
- no fetch or download
- no live/paper/runtime writes
- no project-level investment conclusion or S&P-beat claim
- output artifacts must not be committed

### Phase 2e-1 KOSPI-Primary Weight Feasibility Patch

Phase 2e-0 real operator run exposed an infeasible KOSPI-primary weight
combination when US and KR were risk-on while gold was risk-off.

Rules:

- ``rules_allocator.v1`` remains unchanged and correctly enforces the cash floor
- KOSPI-primary run config policy is updated to v2
- fixed weights guarantee
  ``sum(max(risk_on, risk_off)) + cash_min_weight <= 1``
- builder validates weight-table feasibility before returning
  ``LocalMonthlyRunConfig``
- this is a feasibility patch only, not an investment conclusion
- evidence export remains opt-in and repo-external
- no S&P-relative project conclusion exists yet

### Phase 2e-2 Rebalance Aggregate Cost Invariant Patch

Phase 2e-1 removed the KOSPI-primary cash-floor blocker. A follow-up operator
real-data run then exposed an aggregate cost exact-equality failure:

``total_cost_krw must equal total_fee_krw + total_tax_krw + total_fx_spread_krw``.

Rules:

- ``rules_allocator.v1`` remains unchanged
- KOSPI-primary run config weights remain unchanged from v2
- rebalance accounting keeps
  ``single_rebalance_target_weight_accounting.v1`` and
  ``simple_proportional_fee_sell_tax_fx_spread.v1``
- the patch makes Decimal aggregate summation deterministic and internally
  consistent via high-precision local summation helpers
- no rounding, quantization, or tolerance-based money comparison is introduced
- evidence export remains opt-in and repo-external
- no S&P-relative project conclusion exists yet

### Phase 2e-3 Local Execution Timestamp Alignment Patch

Phase 2e-2 removed the aggregate cost blocker. A follow-up operator real-data
run then exposed a current-period execution timestamp alignment issue:

``no future executable price at or after intended execution time``.

Rules:

- ``execution_prices.py`` remains unchanged and still selects the first price
  at or after ``intended_execution_time``
- local monthly run config policy is updated to v3
- ``decision_time`` remains the previous-period latest instrument source
  timestamp
- ``intended_execution_time`` becomes the current-period earliest instrument
  source timestamp
- benchmark and FX timestamps do not determine execution alignment
- KOSPI-primary v2 feasible weights remain unchanged
- evidence export remains opt-in and repo-external
- no S&P-relative project conclusion exists yet

### Phase 2e-4 Local Benchmark Calendar Alignment Patch

Phase 2e-3 removed the execution-price blocker. A follow-up operator real-data
run then exposed a benchmark common calendar date alignment issue:

``at least 2 common calendar dates are required for benchmark-relative metrics``.

Rules:

- generic ``benchmark_adapter.py`` remains unchanged
- local dry-run aligns monthly benchmark points to strategy NAV timestamps
  before metric adaptation
- alignment preserves benchmark return/value fields and changes only ``as_of``
- execution period mapping is derived from ``run_config.dataset.common_periods``
  and ``rolling_lookback_count``
- evidence export remains opt-in and repo-external
- no S&P-relative project conclusion exists yet

### Phase 2e-5 Local NAV and Position Accounting Sanity Patch

Phase 2e-4 enabled real-data sanitized evidence export. The first operator
output produced an economically impossible strategy terminal return.

Rules:

- this phase adds local evidence-quality sanity checks before metrics/report/export
- ``LOCAL_NAV_SANITY_POLICY_V1`` gates walk-forward NAV, holdings, and trades
- checks include positive NAV, cash bounds, finite period/terminal returns,
  duplicate holdings, post-trade accounting identity, and trade notional bounds
- these checks are evidence-quality guards, not strategy objectives and not
  investment rules
- impossible NAV evidence fails fast with ``ValueError`` before benchmark
  alignment, adapter metrics, or evidence export
- evidence export remains opt-in and repo-external
- no S&P-relative project conclusion exists yet

### Phase 2e-6 Sanitized NAV Accounting Diagnostic Patch

Phase 2e-5 added a local NAV sanity gate before benchmark metrics, report
rendering, or evidence export. The operator real-data run failed at step 20
with a post-trade accounting identity violation:
``cash + holdings != post_trade_portfolio_value_krw``.

This phase adds sanitized diagnostics to identify the mismatch class without
printing raw CSV rows, raw source records, config values, secrets, or
investment conclusions.

Rules:

- ``LOCAL_NAV_SANITY_DIAGNOSTIC_POLICY_V1`` governs per-step sanitized NAV
  accounting diagnostics
- diagnostics include aggregate values, ratios, counts, asset IDs, markets,
  and timestamps only
- diagnostics exclude raw CSV rows, source records, source names, config/secrets,
  and investment conclusions
- ``validate_local_monthly_walk_forward_nav_sanity`` still fails fast before
  benchmark alignment, adapter metrics, report rendering, or evidence export
- evidence export remains blocked until NAV sanity passes
- no S&P-relative project conclusion exists yet

### Phase 2e-7 Local NAV Accounting Materiality Tolerance Patch

Phase 2e-6 sanitized NAV diagnostics showed the operator real-data step 20
accounting delta was ``-1E-19 KRW``, a sub-atomic Decimal drift rather than an
economically meaningful accounting mismatch.

Rules:

- local NAV sanity policy is updated to ``LOCAL_NAV_SANITY_POLICY_V2``
  (``local_monthly_walk_forward_nav_sanity.v2``); ``LOCAL_NAV_SANITY_POLICY_V1``
  remains as historical reference
- the post-trade accounting identity
  (``cash_krw_after + recomputed_holdings_value_krw ==
  post_trade_portfolio_value_krw``) now allows only explicit immaterial Decimal
  drift tolerance
- tolerance values are ``1E-6 KRW`` absolute
  (``LOCAL_NAV_ACCOUNTING_ABS_TOLERANCE_KRW``) and ``1E-18`` relative
  (``LOCAL_NAV_ACCOUNTING_REL_TOLERANCE``)
- material mismatches still fail fast with sanitized ``ValueError`` messages
  that exclude raw numeric internals
- immaterial drift within tolerance passes NAV sanity and may emit a dry-run
  warning; diagnostics distinguish ``accounting_delta_immaterial_decimal_drift``
  from ``accounting_delta_material``
- no rounding or quantization is introduced; monetary values remain ``Decimal``
- rebalance, walk-forward, execution-price, local-run-config, and benchmark math
  remain unchanged
- evidence export remains blocked until all NAV sanity checks pass
- no S&P-relative project conclusion exists yet

### Phase 2e-8 Operator Real CSV Dry-Run After NAV Materiality Patch

Phase 2e-7 added immaterial Decimal drift tolerance for the post-trade NAV
accounting identity. The operator reran the real CSV local dry-run after that
patch.

Result:

- the prior step-20 accounting drift blocker was removed
- the next real-data blocker is ``nav_points[121] period return exceeds
  max_abs_period_return``

Evidence export remains blocked until NAV sanity passes. Existing stale output
files under sibling ``autostock-data/outputs`` are not valid current evidence.
No S&P-relative project conclusion exists yet.

### Phase 2e-9 Sanitized NAV Period Return Diagnostic Patch

Phase 2e-8 confirmed the step-20 accounting drift blocker was removed. The next
real-data blocker is ``nav_points[121] period return exceeds
max_abs_period_return``.

This phase adds sanitized diagnostics for excessive period-return failures.

Rules:

- ``LOCAL_NAV_PERIOD_RETURN_DIAGNOSTIC_POLICY_V1`` governs per-nav-point
  sanitized period-return diagnostics
- diagnostics include aggregate NAV/cash values, period return, cash weights,
  counts, asset IDs, markets, and ratios only
- diagnostics exclude raw CSV rows, source records, source names, raw prices,
  raw quantities, config/secrets, and investment conclusions
- ``validate_local_monthly_walk_forward_nav_sanity`` period-return failures emit
  sanitized ``ValueError`` messages that instruct operators to run the period
  return diagnostic; raw numeric internals are excluded
- the period-return sanity threshold is not weakened
- evidence export remains blocked until NAV sanity passes
- existing stale output files under sibling ``autostock-data/outputs`` are not
  valid current evidence
- no S&P-relative project conclusion exists yet

### Phase 2e-10 Sanitized NAV Valuation Component Diagnostic Patch

Phase 2e-9 diagnosed ``nav_point_index=121`` as a positive NAV spike. Trade
notional ratio and holdings list did not explain the magnitude.

This phase adds sanitized per-asset valuation component diagnostics.

Rules:

- ``LOCAL_NAV_VALUATION_COMPONENT_DIAGNOSTIC_POLICY_V1`` governs per-nav-point
  sanitized valuation component diagnostics
- diagnostics include aggregate KRW component values, value ratios, contribution
  ratios, execution price ratios, USDKRW ratios, quantity ratios, asset IDs,
  markets, counts, and warnings
- diagnostics exclude raw CSV rows, source records, source names, raw execution
  prices, raw quantities, config/secrets, and investment conclusions
- NAV sanity thresholds are not weakened
- evidence export remains blocked until NAV sanity passes
- existing stale output files under sibling ``autostock-data/outputs`` are not
  valid current evidence
- no S&P-relative project conclusion exists yet

### Phase 2e-11 Local USDKRW Continuity Guard Patch

Phase 2e-10 diagnosed the positive NAV spike at ``nav_point_index=121`` as driven
primarily by USDKRW discontinuity in US/GOLD valuation. Sanitized valuation
component diagnostics showed extreme ``usdkrw_rate_ratio`` values for US and
GOLD assets.

Rules:

- local monthly dataset policy is updated to
  ``sibling_local_monthly_csv_dataset.v2``; v1 remains for historical reference
- ``LOCAL_USDKRW_MIN_RATE``, ``LOCAL_USDKRW_MAX_RATE``,
  ``LOCAL_USDKRW_MIN_MONTHLY_RATIO``, and ``LOCAL_USDKRW_MAX_MONTHLY_RATIO``
  provide broad USDKRW level and month-to-month ratio sanity guards
- ``validate_local_usdkrw_rate_continuity`` rejects impossible FX data during
  dataset assembly before walk-forward NAV can be computed
- no automatic FX normalization or rescaling is performed
- diagnostics and errors remain sanitized and exclude raw CSV rows, source
  records, source names, raw FX values, config/secrets, and investment
  conclusions
- evidence export remains blocked until dataset and NAV sanity pass
- no S&P-relative project conclusion exists yet

### Phase 2e-13 Frequency-Aware Benchmark Metric Policy Patch

Phase 2e-12 produced the first repaired-data sanitized local evidence export.
Metrics still required an explicit frequency policy before interpretation.

Rules:

- generic paper-review benchmark metrics keep default
  ``periods_per_year=252`` for daily/paper-review callers
- local monthly dry-run passes ``periods_per_year=12`` through the benchmark
  adapter into ``compute_benchmark_relative_metrics``
- ``information_ratio_annualized`` annualizes with
  ``sqrt(periods_per_year)`` and is therefore frequency-aware
- ``BENCHMARK_ADAPTER_POLICY_V2`` applies when a non-default
  ``periods_per_year`` is used; default adapter behavior remains v1
- ``LOCAL_BENCHMARK_METRIC_FREQUENCY_POLICY_V1`` documents the local monthly
  ``periods_per_year=12`` policy
- ``tracking_error_daily_percent`` remains a legacy field name; local monthly
  dry-run warnings clarify that the value is per aligned observation
- benchmark return, excess return, relative drawdown, capture, and beta math are
  unchanged
- no evidence interpretation or S&P-relative project conclusion exists yet

### Phase 2e-15 Local Static Neutral Baseline Patch

Phase 2e-14 produced frequency-aware repaired-data local evidence. Evidence
interpretation still requires comparison against a deterministic static neutral
baseline before any project-level conclusion can be considered.

Rules:

- static neutral baseline policy is
  ``local_monthly_static_neutral_baseline_us60_kr20_gold15_cash5.v1``
- static neutral baseline uses fixed US 0.60 / KR 0.20 / GOLD 0.15 /
  CASH 0.05 weights
- weights are frozen and not optimized to current evidence
- static baseline uses the same local monthly dataset, period specs, execution
  schedule, cost model, NAV sanity, benchmark alignment, and
  ``periods_per_year=12`` benchmark metric frequency as the rules dry-run
- static baseline is non-tactical and uses no moving-average, rules, LLM, or
  runtime decision logic
- exported static baseline fields are sanitized numeric evidence only
- no pass/fail, deployment, recommendation, investment advice, or S&P-relative
  project conclusion exists yet

### Phase 2f-0 Local Evidence Gate Review and Redesign Freeze

Phase 2e-16 produced static-baseline local evidence for the current rules
allocator candidate. The current rules allocator did not meet local terminal
wealth gates versus the S&P benchmark or the static neutral baseline.

This is a research gate result, not investment advice. The current rules
allocator is frozen as a failed local-evidence candidate.

Future redesign must be hypothesis/version/evidence driven. Do not tune weights
ad hoc or optimize on the same evidence without a versioned hypothesis. Future
candidate changes require a backtest rerun, comparison against the S&P benchmark
and static neutral baseline, and sanitized evidence export.

### Phase 2f-1 Versioned Rules Redesign Hypothesis Spec

Phase 2f-1 adds a hypothesis-only v2 redesign spec in
``docs/RULES_REDESIGN_HYPOTHESIS_V2.md``. Current v1 remains frozen as a failed
local-evidence candidate.

No implementation is authorized yet. Future implementation must be versioned and
evidence-gated against the S&P benchmark and static neutral baseline.

### Phase 2f-2 Rules V2 Implementation Contract Spec

Phase 2f-2 adds an implementation contract only in
``docs/RULES_V2_IMPLEMENTATION_CONTRACT.md``. No allocator code is changed.

V1 remains frozen as a failed local-evidence candidate. V2 implementation must
be separate, versioned, and evidence-gated against the S&P benchmark and static
neutral baseline.
