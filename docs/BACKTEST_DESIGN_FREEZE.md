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
