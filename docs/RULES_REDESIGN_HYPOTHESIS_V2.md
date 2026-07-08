# Rules Redesign Hypothesis V2

This document is a sanitized, self-contained research-governance hypothesis
specification. It does not authorize implementation, deployment, paper
activation, live activation, or investment action.

## Context

Phase 2f-0 froze the current rules allocator as a failed local-evidence
candidate. Phase 2e-16 evidence showed the current rules allocator
underperformed both the S&P 500 TR KRW benchmark terminal return and the static
neutral baseline terminal return.

This document is a hypothesis spec only. No implementation is authorized by
this document alone. No deployment or investment recommendation exists.

## Failed Candidate Identifier

```text
failed_candidate: local_monthly_rules_allocator_v1
failed_candidate_status: frozen_failed_local_evidence_candidate
```

## New Candidate Identifier

```text
candidate_id: local_monthly_rules_allocator_v2_hypothesis
candidate_status: hypothesis_only_not_implemented
```

## Core Problem Statement

Using the Phase 2e-16 local evidence context, the current rules allocator had
materially lower terminal wealth than both the benchmark and the static neutral
baseline. A working hypothesis is that the current rules allocator likely
over-penalized equity participation or remained too defensive relative to the
S&P benchmark.

This is not a proven causal claim. It is a versioned redesign hypothesis that
must be tested before any implementation can support further interpretation.
Lower drawdown cannot compensate for terminal wealth underperformance. A
redesigned candidate must be evaluated by terminal wealth first, not drawdown
first.

## Redesign Hypotheses

These are versioned hypotheses only, not implementation code:

H1: Increase S&P-core participation so the allocator is not structurally underweight US equity in long bull markets.
H2: Treat risk-off as a temporary risk-budget reduction, not a broad long-term retreat from the benchmark.
H3: Use benchmark-relative drawdown and benchmark-relative recovery triggers in addition to absolute trend signals.
H4: Keep static neutral baseline as a mandatory comparator for every future evidence run.
H5: Any tactical overlay must improve terminal wealth after costs, not merely reduce volatility.

## Non-Goals

- No stock selection.
- No LLM/news signal yet.
- No live/paper deployment.
- No tuning weights on the same evidence without a versioned protocol.
- No changing USDKRW/data repair logic.
- No changing benchmark metric math.
- No weakening dataset/NAV sanity gates.
- No claim that lower MDD alone is success.

## Candidate Design Envelope

Allowed future design directions, without implementation in this phase:

- S&P-core dominant baseline.
- Tactical satellite overlays.
- Risk-off capped by explicit relative-performance guard.
- Maximum cash/gold drag budget.
- Required minimum US-equity participation unless evidence supports otherwise.
- Relative drawdown recovery logic.
- Cost-aware turnover limits.

Do not assign final weights in this phase. Do not implement any of these
directions in this phase.

## Evidence Acceptance Protocol

Any future v2 implementation must satisfy the following evidence gates before
paper/live consideration:

Gate A: v2 terminal return must exceed S&P 500 TR KRW benchmark terminal return.
Gate B: v2 terminal return must exceed static neutral baseline terminal return.
Gate C: v2 terminal excess return must exceed rules v1 terminal excess return.
Gate D: v2 must pass dataset continuity, NAV sanity, frequency-aware metrics, and static baseline evidence export.
Gate E: v2 must not rely on lower drawdown alone to claim success.
Gate F: v2 must report costs, turnover proxy if available, max relative drawdown, and rules-minus-static comparisons.

## Anti-Overfit Protocol

- No repeated ad hoc retuning on the same repaired local evidence.
- Any parameter change requires a version bump.
- Any parameter change requires a stated hypothesis.
- Rerun must compare against the S&P benchmark and static neutral baseline.
- Final evaluation must be documented before paper/live consideration.
- If a validation/holdout split is introduced later, it must be documented
  before using it.

## Prohibited Interpretations

- No investment recommendation.
- No deployment approval.
- No live/paper activation.
- No claim that any security should be bought or sold.
- No claim that lower drawdown alone is success.
- No claim that the v2 hypothesis is expected to beat S&P before evidence.
