# Local Evidence Gate Review

This document records a local research evidence gate review for the current
rules allocator candidate. It is sanitized and self-contained.

## Scope

- Local monthly KOSPI-primary evidence only.
- Evidence used repaired sibling CSV inputs from the operator-local data
  directory outside the repository.
- Benchmark metrics used the local monthly frequency-aware policy.
- Static neutral baseline evidence is included as a mandatory comparator.
- This review makes no deployment recommendation and no investment
  recommendation.

## Evidence Inputs

These Phase 2e-16 values are sanitized research evidence only:

```text
rules terminal_strategy_return: 606.8516459084041211247327601
rules terminal_benchmark_return: 1308.719922162372568027027300
rules terminal_excess_return: -701.8682762539684469022945399
rules max_relative_drawdown: -63.29150318490450077920038984

static neutral terminal_strategy_return: 1140.747516803951804075925185
static neutral terminal_benchmark_return: 1308.719922162372568027027300
static neutral terminal_excess_return: -167.9724053584207639511021150
static neutral max_relative_drawdown: -48.44058973989600446483907708

rules_minus_static_terminal_return: -533.8958708955476829511924249
rules_minus_static_excess_return: -533.8958708955476829511924249
```

The evidence inputs above do not include raw CSV rows, source records, source
names, raw FX values, configuration values, or secrets.

## Gate Definitions

Gate 1: rules terminal return should exceed S&P 500 TR KRW benchmark terminal return.
Gate 2: rules terminal return should exceed static neutral baseline terminal return.
Gate 3: lower drawdown alone cannot override terminal wealth underperformance.
Gate 4: evidence must pass dataset/NAV/frequency/static-baseline sanity before interpretation.

## Gate Results

- Gate 1: not met.
- Gate 2: not met.
- Gate 3: terminal wealth underperformance dominates; drawdown cannot rescue
  the result.
- Gate 4: met for this local evidence run.

## Research Interpretation

The current local rules allocator did not meet the local evidence gate.

The current rules allocator underperformed both the S&P 500 TR KRW benchmark
and the static neutral baseline in terminal return. The static neutral baseline
reduced the gap to the benchmark relative to the rules allocator.

The current rules allocator should not proceed to paper/live deployment based
on this local evidence. This is a research gate outcome, not investment advice.

## Redesign Freeze

- Freeze the current rules allocator as a failed local evidence candidate.
- Do not tune weights ad hoc.
- Do not optimize on the same evidence without a versioned hypothesis.
- Future changes require an explicit hypothesis, version bump, backtest rerun,
  comparison against the S&P benchmark and static neutral baseline, and evidence
  export.

The versioned redesign hypothesis spec is recorded in
`docs/RULES_REDESIGN_HYPOTHESIS_V2.md`; this pointer does not change the local
gate outcome above.

## Candidate Redesign Hypotheses

These are hypotheses only, not implementation instructions:

- Reduce defensive under-participation in long equity bull markets.
- Redesign risk-off trigger to avoid excessive benchmark-relative drag.
- Evaluate dynamic risk budget against relative drawdown, not only absolute
  drawdown.
- Keep static neutral baseline as mandatory comparator.
- Consider S&P-core allocation dominance before adding tactical overlays.

## Explicit Prohibited Interpretations

- No investment recommendation.
- No deployment approval.
- No live/paper activation.
- No claim that lower MDD alone is success.
- No claim that any security should be bought or sold.
