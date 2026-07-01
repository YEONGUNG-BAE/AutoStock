# Investment Objective — S&P 500 Benchmark-Relative Direction

> **Status: planning / future-direction document.**
> It is **not an implemented runtime spec** and it is **not an active implementation requirement**.
> It records the intended long-term economic objective and the design direction
> that objective implies. Nothing here changes current runtime
> behavior, operational safety gates, or the paper-only default. No metric,
> backtest harness, or Allocator v2 described below is implemented by this
> document. Anything labeled "future" must not be built until an explicit later
> phase authorizes it.

## 1. Primary investment objective

The primary economic objective of AutoStock is **long-term terminal wealth that
exceeds a buy-and-hold investment in the S&P 500 total-return index over a
roughly 10-year horizon**, measured after realistic costs (commissions, taxes,
slippage, FX).

- The benchmark to beat is **S&P 500 total return** (dividends reinvested), not
  an absolute percentage target and not "a safer balanced portfolio."
- Beating the benchmark means **higher terminal wealth over ~10 years**,
  accepting higher interim volatility and drawdown than a defensive balanced
  allocation — provided the long-run compounding outcome is superior.
- Capital preservation, cash buffers, and gold are **instruments in service of
  long-run outperformance**, not the objective itself. They are justified only
  when they plausibly improve the long-run benchmark-relative outcome (for
  example, avoiding drawdowns deep enough to impair compounding), not as a
  permanent de-risking stance.

This reframes the earlier planning emphasis (a defensive, balanced,
downside-protected allocation targeting a modest absolute annual return) as a
**means**, subordinate to the benchmark-relative **end**.

## 2. Operational safety vs investment defensiveness

Two very different kinds of "conservatism" must never be conflated.

**Operational safety — STRICT, never relaxed by this objective:**

- paper-only default; no live orders without the full config + environment +
  credential + startup gates
- no-write / read-only postures where specified
- activation gates, real-order-adapter construction guard, no automatic restart,
  no daemon
- fail-closed on missing or invalid config
- secret protection (no keys / tokens / accounts / raw frames / tracebacks in
  logs or evidence)
- live KIS is **Operator-only**; never Cursor/Claude-initiated

**Investment defensiveness — MAY be relaxed to pursue the benchmark objective:**

- persistent large cash allocation (10–30% normal band today)
- persistent gold allocation (18–22% normal band today)
- structurally low equity beta / weak up-market participation
- slow re-entry after de-risking

Pursuing the S&P benchmark objective may justify **relaxing investment
defensiveness** (more equity exposure, faster re-entry, less permanent
cash/gold). It **never** justifies relaxing any operational safety gate. A
proposal that touches operational safety in the name of performance is out of
scope for this objective and must be rejected — see Sections 11–12 where that is
an automatic failure.

## 3. Primary benchmark

- **Primary benchmark: the S&P 500 total-return index** (dividends reinvested).
- **Accounting basis: KRW-unhedged is primary.** The bot's NAV is realized in
  KRW (KR accounts, KRW cash), so the primary comparison is the bot's KRW net
  return vs S&P 500 total return converted to KRW at prevailing FX (unhedged).
  This captures the actual investor experience, including USD/KRW movement.
- A USD-denominated S&P 500 TR view may be reported as a **secondary** lens to
  separate strategy performance from FX, but the **primary** benchmark
  accounting is KRW-unhedged.

## 4. Secondary diagnostic benchmarks

These are **diagnostic** references used to attribute where performance comes
from. They are not the objective; only the S&P 500 TR (Section 3) defines
success or failure.

- **Static neutral baseline** — the bot's active strategy replaced by a fixed,
  never-actively-rebalanced allocation. Isolates the value added by active
  allocation versus doing nothing.
- **KOSPI 200 total return** — isolates the KR-market beta contribution.
- **Static KR/US/Gold mix** — a fixed weighting (for example the planning
  "balanced" 50/30/20 invested split) held constant. Isolates active-allocation
  value versus a static multi-asset mix.
- **60/40 (equity / bond-or-cash)** — a classic defensive reference. Shows
  whether the bot is merely reproducing a generic defensive portfolio.

## 5. Current design mismatch

Ground truth from the current codebase (stated precisely — do **not** overstate
this as "no evaluation exists"):

- **Absolute NAV metrics already exist** in `src/paper_review/metrics.py` (total
  return, annualized return, max drawdown, worst/best daily, daily volatility,
  average cash/invested, NAV snapshot count).
- **Benchmark-relative calculation models/functions now exist** in
  `src/paper_review/models.py` and `src/paper_review/metrics.py` for an
  externally supplied total-return benchmark series: bot/benchmark return,
  excess return, relative drawdown, tracking error, information ratio,
  up/down capture, beta, and alignment counts/warnings. This is calculation
  support only.
- **Human-readable benchmark-relative report rendering now exists** in
  `src/paper_review/report.py` for already-computed
  `BenchmarkRelativeMetrics`. This renderer is offline presentation only; it
  does not fetch benchmark data, read runtime evidence, prove long-term
  outperformance, or integrate benchmark-relative results into postmortems.
- **Paper-Day market-data evidence is not portfolio NAV** and is invalid for
  investment-performance measurement. First real benchmark-relative investment
  numbers require a valid strategy NAV series plus a KRW-unhedged S&P 500
  total-return benchmark series.
- The **allocator is structurally biased toward persistent cash and gold and low
  equity beta** (`src/allocator/rules.py`: cash 10–30, gold 18–22 / 15–25 hard
  bands). That is a defensive posture, not a benchmark-tracking-with-active-
  deviation posture.
- The **`AllocationRegime` enum exists but is orphaned**: it is defined in
  `src/allocator/models.py` (NORMAL / REBALANCING / DEFENSIVE / EMERGENCY) but is
  **not a field on `AllocatorDecision`**. It is available to revive and connect
  in a future Allocator v2; it is not a brand-new concept.
- There is **no historical backtest harness** and **no true multi-regime
  out-of-sample evaluation**. Current replay tests prove determinism, not that
  the strategy beats the S&P 500 over a decade.

The mismatch, therefore: the system can measure *absolute* paper performance and
can calculate benchmark-relative paper metrics when supplied an aligned
benchmark series, but it **cannot yet answer the end-to-end question that matters
for this objective — did it beat the S&P 500 total return, net of costs over a
credible historical and forward evaluation?**

## 6. Correct target direction

The intended (future) design direction:

- **Benchmark-relative active allocator.** The allocator's job becomes "hold the
  S&P 500 equity core by default, and deviate only when evidence justifies an
  expected benchmark-relative improvement," rather than "always hold a defensive
  balanced mix."
- **S&P 500 / US-equity core as the default exposure.** Absent a justified
  deviation, the neutral position leans toward the benchmark's equity exposure,
  not toward cash and gold.
- **Evidence-gated deviations.** Every deviation from the benchmark core (raising
  cash, adding gold, tilting KR, de-risking) must carry an explicit,
  Date-ID-backed rationale and be measured for whether it actually improved the
  benchmark-relative outcome. Deviations that chronically cost excess return are
  removed.
- Defensive actions (cash, gold, de-risking) remain available but must **earn
  their place** through benchmark-relative evidence rather than being structural
  defaults.

## 7. Benchmark-relative metrics and remaining future metrics

To evaluate the objective, the following **benchmark-relative** metrics are
required. Phase 1 adds deterministic `paper_review` calculation support for the
core return/risk metrics when an external benchmark series is supplied; this
section still does not implement data fetching, backtesting, runtime behavior,
or allocator changes. Phase 1.5 adds human-readable rendering of those computed
metrics only.

- bot net return (after commissions, taxes, slippage, FX)
- S&P 500 total return over the same window (KRW-unhedged primary)
- excess return (bot − benchmark)
- relative drawdown (drawdown of the bot-versus-benchmark ratio — the worst
  underperformance stretch)
- tracking error (standard deviation of excess return)
- information ratio (excess return ÷ tracking error)
- up-capture ratio (bot return during benchmark-up periods)
- down-capture ratio (bot return during benchmark-down periods)
- beta to the benchmark
- absolute max drawdown (already computed today; retained)
- turnover
- cost drag (commissions + taxes + slippage + FX expressed as a return drag)
- exposures over time (equity / cash / gold, KR / US)
- attribution (how much excess return came from asset allocation vs timing vs FX)

## 8. Required future evaluation order

The objective must be evaluated in this order; each step gates the next. This is
a future plan, not a current workflow:

0. **Follow the benchmark data and backtest plan** in
   `docs/BENCHMARK_DATA_AND_BACKTEST_PLAN.md` before implementing any historical
   harness. That plan freezes the KRW-unhedged benchmark basis, FX conversion,
   observation frequency / annualization, alignment, and bias controls.
1. **Integrate and report benchmark-relative metrics** (Section 7) with supplied
   benchmark series on the existing paper ledger / NAV history.
2. **Build a historical backtest harness** that can replay the strategy across
   multiple market regimes out-of-sample.
3. **Evaluate the current strategy (v1)** against the S&P 500 TR using those
   metrics and backtests — establish whether v1 has any benchmark-relative edge.
4. **Only if warranted, design Allocator v2** (Section 9) to close the gap, then
   re-run metrics + backtests on v2.
5. **Then run paper trading** as forward / operational validation of the frozen,
   backtest-supported strategy.

Paper trading and tiny-live are **not** proofs of long-run S&P outperformance;
they validate operation and forward behavior of a strategy already supported by
backtest evidence. Paper is confirmation, not the primary proof of a 10-year
edge.

## 9. Required future Allocator v2 direction

A future Allocator v2 (**DO NOT implement from this document**) should:

- **Connect the existing `AllocationRegime`** enum into the decision schema
  instead of leaving it orphaned.
- Consider adding **candidate fields** (names illustrative, not a committed
  schema, **DO NOT implement now**):
  - `allocation_regime` — revive NORMAL / REBALANCING / DEFENSIVE / EMERGENCY as
    an explicit decision field
  - `target_equity_exposure` — intended total equity exposure relative to the
    benchmark
  - `benchmark_core_exposure` — the default S&P / US-equity core weight
  - `active_deviation_from_benchmark` — the signed deviation of the decision from
    the benchmark core
  - `deviation_rationale` — Date-ID-backed justification for any deviation
  - `up_market_participation_check` — an explicit check that the allocation can
    participate in benchmark up-moves
  - `defensive_hedge_level` — a graded, evidence-gated defensive level rather than
    permanent cash/gold bands
- Preserve all existing Python-owned validation and safety semantics. Allocator
  v2 changes the *allocation objective*, not the *safety gates*.

These are directional candidates for a later design phase. No field here is a
current requirement, and none may be added to the `allocator_decision.v1` schema
now.

## 10. Required future risk/control concepts

Future risk / control work implied by the objective (planning only):

- Reframe risk limits to be **benchmark-relative-aware** (track relative drawdown
  and tracking error), while keeping the absolute MDD killswitch stages as an
  operational safety floor.
- Keep the **MDD killswitch and all hard filters as operational safety**,
  independent of the investment objective. They are not to be relaxed to chase
  benchmark performance.
- Add **up-market participation** as a first-class control so the bot cannot
  chronically sit out benchmark rallies.
- Keep the **benchmark objective strictly separate from live-order
  authorization**: adopting a benchmark objective must never lower a live or
  activation gate.
- Cost-awareness: turnover and cost drag become explicit constraints because
  excess return is measured net of costs.

## 11. Failure criteria

The objective is **not met** (failure) if, over the evaluation horizon:

- the bot's KRW net return **chronically trails S&P 500 total return** after
  costs — persistently negative excess return with no evidence-based path to
  close it;
- the strategy is **structurally unable to participate in benchmark up-markets**
  (chronically low up-capture) because of a permanent defensive posture;
- deviations from the benchmark core show a **negative information ratio** —
  active decisions destroy value versus simply holding the benchmark;
- **or**, as a hard stop unrelated to returns, **any operational safety gate is
  weakened** in the name of performance. Weakening operational safety is an
  **automatic failure** regardless of returns.

## 12. Success criteria

The objective is **met** (success) if, over the evaluation horizon:

- the bot's KRW net return **exceeds S&P 500 total return** after realistic costs
  (commissions, taxes, slippage, FX), with a **positive information ratio**
  (active deviations added value);
- the outperformance is achieved with a **relative drawdown the operator finds
  acceptable** for the age / risk profile — higher interim volatility than a
  defensive mix is acceptable;
- the result is **supported by out-of-sample backtest evidence across multiple
  regimes**, not a single lucky paper window;
- and **all operational safety gates remained strict** throughout — no safety
  relaxation was used to obtain the result.

---

*This document is planning and future direction only. It does not authorize live
orders, activation, a daemon, automatic restart, or any change to the current
paper-only runtime. Implementation of any "future" item requires an explicit
later phase.*
