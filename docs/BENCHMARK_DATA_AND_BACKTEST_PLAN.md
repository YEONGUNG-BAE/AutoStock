# Benchmark Data and Backtest Plan

> **Status: planning / future-direction document.**
> This is **not an implemented runtime spec** and **not an active implementation
> requirement**. This document fetches no data, builds no backtest harness,
> changes no runtime behavior, and authorizes no Allocator v2 work. It freezes
> data-source, conversion, alignment, annualization, and bias-control decisions
> that a future explicit implementation phase must follow.

## 1. Purpose and Non-Goals

The historical backtest must be designed around data correctness before any
code is written. The main risks are easy to hide in implementation details:
look-ahead bias can make decisions appear clairvoyant, survivorship bias can
remove failed securities from history, and an unconverted USD benchmark can make
KRW NAV excess return meaningless.

Recommended default: write and guard this data plan before implementing the
harness. Alternative: let the harness choose data rules as it is implemented.
The alternative is rejected because it makes bias controls hard to audit and
allows FX/accounting-basis drift to enter silently.

Non-goals for this phase:

- no data fetch, download, API client, or adapter
- no historical backtest harness
- no S&P 500, KOSPI, gold, or FX data ingestion
- no Allocator v2, no `AllocationRegime` wiring, and no schema changes
- no allocation/risk/order/broker/emergency/runtime behavior changes
- no config changes and no reading of `config/config.toml`

## 2. Primary Benchmark Definition

Primary benchmark: **S&P 500 total return** with dividends reinvested, not S&P
500 price return.

Primary accounting basis: **KRW-unhedged**. The bot's NAV is reported as KRW
through `NavSnapshot.total_nav_krw`, so the benchmark series supplied to
`paper_review` must already be on the same KRW accounting basis.

Exact conversion for any future USD source series:

```text
sp500_tr_krw_level(t) = sp500_tr_usd_level(t) * usdkrw(t)
```

The conversion must use a consistent as-of timing convention for both the S&P
500 total-return level and the USDKRW reference. A USD-basis S&P 500 TR series
or a KRW-hedged S&P 500 TR series may be tracked as a **diagnostic lens** only.
Neither diagnostic series replaces the primary KRW-unhedged objective.

Recommended default: primary scoring uses KRW-unhedged S&P 500 total return.
Alternative: score against raw USD S&P 500 TR. The alternative is rejected for
primary evaluation because it mixes KRW bot NAV with USD benchmark levels and
corrupts excess return through FX distortion.

## 3. FX Source and Rules

Future implementation must select a named USDKRW close source before fetching
or loading data. Recommended default: a daily official or institutional USDKRW
close with explicit timestamp, source name, time zone, and holiday calendar.
Alternative: intraday spot snapshots. The alternative is rejected for primary
scoring because it adds timing ambiguity unless the strategy itself executes on
that intraday FX basis.

Close-basis alignment rule:

- S&P 500 total-return level uses the NYSE local close for its trade date.
- USDKRW uses the selected daily local close for its own market date.
- KRX NAV and KR-market references use the Korea local close for their trade
  date.
- The primary aligned return computation uses calendar dates common to the
  resulting NAV and benchmark series.

The KRX and NYSE closes do not occur at the same instant. Recommended default:
use each market's own local close, label every observation with its source
timestamp and normalized calendar date, and align by common calendar dates for
return scoring. Alternative: shift the S&P 500 series to the next Korean
business day. The alternative may be useful for a trading-decision simulation,
but it is not the default performance-scoring convention because it changes the
economic holding-period label and can obscure the simple KRW investor return.

Non-trading-day handling: no forward-fill and no interpolation inside the
aligned return computation. This preserves Phase 1's common-date-only alignment:
if NAV or benchmark is missing for a date, that date is not used for period
returns.

Recommended default: common dates only. Alternative: forward-fill missing
benchmark or FX values. The alternative is rejected for primary scoring because
it can create artificial zero-return periods and hide holiday/calendar gaps.

## 4. Secondary Diagnostic Baselines

These baselines are diagnostics. They explain where performance came from; they
do not replace the S&P 500 total-return objective.

- **Static S&P 500 100%**: the primary hurdle, constructed from S&P 500 total
  return converted to KRW-unhedged levels using the rule in Section 2.
- **Static AutoStock neutral allocation baseline**: a dumb static version of the
  current allocator posture. `config/config.toml.example` defines runtime mode
  and broker/account-role examples, not allocation targets. The constructible
  neutral baseline therefore uses `.cursor/rules/05-allocator.mdc`: 20% cash on
  total-account basis, with the invested 80% split KR 50 / US 30 / Gold 20.
  That implies total-account weights of cash 20%, KR 40%, US 24%, Gold 16%.
  Current rule bands are cash 10-30 and gold 18-22 on invested assets in normal
  mode. The purpose is the LLM-vs-static question: does the active allocator
  beat a simple frozen version of itself?
- **Static KR/US/Gold mix baseline**: construct a daily return series from fixed
  KR equity, US equity, and gold proxy weights. Recommended default: use the
  same KR 50 / US 30 / Gold 20 invested split as the neutral allocator baseline,
  without the 20% cash sleeve, to isolate multi-asset beta. Alternative: use a
  50/30/20 total-account mix. The alternative is allowed as a secondary
  diagnostic, but not the default, because it does not match the current
  allocator's invested-assets convention.
- **60/40 or balanced defensive baseline**: construct a fixed defensive
  portfolio from 60% equity and 40% cash/bond proxy, or a cash/equity/gold
  balanced variant if reliable Korean cash/bond proxy data is unavailable.
  Recommended default: document the exact constituents and weights before use.
  Alternative: omit this baseline. The alternative is rejected because it leaves
  no reference for whether AutoStock is merely reproducing a generic defensive
  portfolio.
- **KOSPI200 total-return proxy**: candidate proxy for KR equity beta. Candidate
  sources may include a KOSPI200 total-return index or a total-return ETF proxy.
  Selection and fetching are later phases.
- **Gold proxy**: candidate proxy for KRW gold exposure. Candidate sources may
  include a KRW gold ETF total-return proxy or a gold spot series converted to
  KRW with explicit costs. Selection and fetching are later phases.

Recommended default: include the S&P 500 100%, static AutoStock neutral,
KR/US/Gold, 60/40, KOSPI200 TR proxy, and gold proxy diagnostics when their data
can be sourced cleanly. Alternative: only compare to S&P 500 TR. The alternative
is insufficient for attribution even though S&P 500 TR remains the objective.

## 5. Observation Frequency and Annualization

Recommended primary frequency: daily trading-day observations. This matches the
existing `paper_review` daily-return vocabulary and supports enough observations
for tracking error, information ratio, capture ratios, and beta.

Alternative: weekly or monthly observations. Weekly/monthly series reduce noise
and calendar-mismatch headaches, but they lower sample size and can hide
drawdown/capture behavior that matters for risk control.

`periods_per_year` must be explicit and must match the chosen frequency:

- trading-day daily: `periods_per_year = 252`
- weekly: `periods_per_year = 52`
- monthly: `periods_per_year = 12`

Phase 1's `compute_benchmark_relative_metrics()` annualizes information ratio
with `sqrt(252)`. That is correct only for trading-day-frequency return series.
Future harness work must pass or otherwise preserve a `periods_per_year` value
consistent with the actual observation frequency. A later phase may need to
parameterize Phase 1's hardcoded 252; this plan documents that need but does not
change Phase 1 code.

Recommended default: keep daily trading-day frequency and `periods_per_year =
252` for the first historical harness. Alternative: use monthly frequency and
`periods_per_year = 12`. The alternative is useful for long-horizon summary
reporting, but not the first harness default because it weakens risk/capture
diagnostics.

## 6. Bias Controls

Each future harness rule must be enforceable by input timestamps and testable in
fixtures.

### Look-Ahead Bias

Concrete rule: every input must carry an `as_of` timestamp. A decision at date
or time `d` may read only inputs with `as_of <= decision_time`. Benchmark and FX
values used to score period `t` must not use data from `t+1` or later.

Audit/test: create a fixture where a future-dated price, FX value, or benchmark
point is present and assert the harness refuses it or excludes it from the
decision input. Log the maximum input `as_of` consumed by every simulated
decision.

### Survivorship Bias

Concrete rule: any equity universe or security history must be point-in-time.
Delisted names, suspended names, corporate-action transitions, and index
membership changes must remain represented as they were known at the simulated
date. Survivorship-clean data selection is a later phase and must be solved
before claims of S&P outperformance.

Audit/test: use a fixture with a delisted or removed symbol and assert it remains
available in the historical universe until its real removal date, and that
post-removal data is not visible before the removal as-of timestamp.

### Split/Dividend Adjustment

Concrete rule: benchmark series must be total-return with dividends reinvested.
Underlying price series used for equities, ETFs, or proxies must be split and
dividend adjusted when returns are computed, unless the series is explicitly a
cash-flow-inclusive ledger value.

Audit/test: use a fixture with a split/dividend event and assert the adjusted
return series does not create a false drawdown or false gain on the corporate
action date.

### Timezone and Calendar

Concrete rule: retain original source timestamps and time zones, then derive one
normalized calendar date for alignment. Primary performance scoring uses common
calendar dates across NAV and benchmark. No forward-fill and no interpolation
are allowed in the aligned return computation.

Audit/test: use fixtures with KRX-only holidays, NYSE-only holidays, and FX
holidays. Assert only common dates produce period returns, and missing dates are
reported rather than filled.

Recommended default: timestamped, point-in-time inputs plus common-date scoring.
Alternative: calendar-expanded series with forward-filled values. The
alternative is rejected for primary scoring because it can hide stale data and
manufacture period returns.

## 7. Replay vs True Backtest

Deterministic replay means the same explicit input produces the same output. It
is regression evidence for code determinism. It is **not** strategy performance
evidence.

Historical backtest means running the strategy over historical point-in-time
as-of data to produce a NAV track record. That track record then becomes the
investment-performance input. This historical backtest does **not** exist yet.

Phase 1.5's renderer will consume the future backtest's output NAV series plus
the supplied benchmark series through the existing metrics path. The renderer is
reused, not rebuilt.

Recommended default: keep replay tests and historical backtest claims separate
in naming, docs, and test assertions. Alternative: call deterministic replay a
backtest. The alternative is rejected because it conflates regression
determinism with investment evidence.

## 8. Output Contract

The future harness must produce data shapes that plug into the existing
`paper_review` layer without new reporting logic:

- NAV series compatible with `domain.portfolio.NavSnapshot`, especially
  `as_of` and `total_nav_krw`.
- Benchmark series compatible with `paper_review.BenchmarkReturnPoint`, where
  `total_return_index_value` is already in the chosen accounting basis.
- Metrics generated by `compute_benchmark_relative_metrics(nav_snapshots,
  benchmark_points)`.
- Human-readable output rendered by
  `render_benchmark_relative_metrics_markdown(metrics)`.

Recommended default: harness output is an explicit tuple/list of `NavSnapshot`
and `BenchmarkReturnPoint` objects passed into existing functions. Alternative:
create a separate backtest-specific metrics/report model. The alternative is
rejected for the first harness because it duplicates Phase 1 and Phase 1.5
contracts and increases the chance of metric drift.

## 9. Phasing After This Plan

Phase 2 implementation, in a future task, should build the historical backtest
harness according to this plan. This document itself is only the data plan.

After the harness exists:

1. Evaluate v1 against KRW-unhedged S&P 500 TR and the static diagnostic
   baselines.
2. If v1 has evidence of benchmark-relative value, consider Allocator v2.
3. If the LLM allocator cannot beat the static AutoStock neutral baseline,
   reduce or remove the active LLM allocator rather than elaborate it.
4. Treat 9-12월 paper trading as operational and forward validation of a frozen
   strategy, not proof of 10-year alpha.

Recommended default: freeze the evaluated strategy before paper validation.
Alternative: keep tuning during paper and treat the combined record as one
track record. The alternative is rejected because it invalidates attribution and
turns paper results into a moving-target experiment.

## 10. Explicit Prohibitions Carried Forward

- No unsupported expected-return probability tables anywhere in this plan or in
  later reports.
- No benchmark-relative claim may replace evidence from a valid strategy NAV
  series plus KRW-unhedged S&P 500 total-return benchmark series.
- Operational safety gates remain strict and separate from the investment
  objective.
- No live-order, broker, allocator, risk, emergency, config, daemon, restart, or
  runtime behavior may be changed by this plan.
- No data fetch, adapter, or historical harness is authorized by this document.

Recommended default: all later implementation tasks must cite this plan and
state which section they implement. Alternative: rely on general benchmark
language in the investment objective doc. The alternative is rejected because it
does not provide auditable data, FX, calendar, and bias rules.
