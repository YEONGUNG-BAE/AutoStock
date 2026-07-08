# Rules V2 Implementation Contract

This document is a sanitized, self-contained implementation contract. It is not
implementation.

## Status and Authorization Boundary

This document authorizes a future implementation phase only after this contract
is committed and reviewed. It does not authorize deployment, paper trading, live
trading, or investment action. It does not change the Phase 2f-0 gate result.
It preserves V1 as a frozen failed local-evidence candidate.

## Candidate Identity

```text
candidate_id: local_monthly_rules_allocator_v2_contract
candidate_status: implementation_contract_only_not_implemented
supersedes_for_research: local_monthly_rules_allocator_v1
```

Do not delete or mutate V1.

## Design Objective

- Primary objective remains terminal wealth above the S&P 500 TR KRW benchmark.
- Secondary objective is terminal wealth above the static neutral baseline.
- Lower drawdown alone is not success.
- Tactical overlays must improve net terminal wealth after costs.
- Future V2 must be evaluated against the S&P benchmark and static neutral
  baseline.

## V2 Design Envelope

V2 is defined as a S&P-core dominant monthly rules allocator. This section
specifies the intended contract only, not final implementation code.

Core allocation:

- US equity is the dominant risky allocation.
- US equity should not be structurally underweight the S&P benchmark during
  normal/risk-on conditions.

Satellite allocation:

- KR equity and gold are satellite allocations.
- Satellite allocations must not dominate terminal wealth behavior unless
  explicitly justified by future evidence.

Cash/gold drag:

- Cash and gold together must have an explicit cap under normal/risk-on
  conditions.
- Risk-off may increase cash/gold, but only as a temporary risk-budget
  reduction.

Relative performance:

- Benchmark-relative drawdown and recovery state must be considered before
  extended defensive positioning.
- Risk-off logic must have an explicit re-entry/recovery condition.

## Proposed V2 Policy Constants

These constants are contract-level proposals only. They are not implemented in
this phase.

```text
RULES_ALLOCATOR_V2_CONTRACT_POLICY = "local_monthly_rules_allocator_v2_contract.sp_core_relative_recovery.v1"

NORMAL_TARGET_WEIGHTS:
  asset_us: 0.70
  asset_kr: 0.15
  asset_gold: 0.10
  cash: 0.05

DEFENSIVE_TARGET_WEIGHTS:
  asset_us: 0.50
  asset_kr: 0.10
  asset_gold: 0.25
  cash: 0.15

MIN_US_WEIGHT_NORMAL: 0.65
MAX_CASH_GOLD_WEIGHT_NORMAL: 0.20
MAX_CASH_GOLD_WEIGHT_DEFENSIVE: 0.40
```

Rationale:

- Normal target is S&P-core dominant.
- Cash minimum remains 0.05.
- Defensive state reduces but does not abandon US equity.
- Defensive cash+gold is capped.
- Proposed constants are fixed before implementation to avoid ad hoc retuning.

## V2 State Logic Contract

Intended future state logic, in text only:

```text
normal_state:
  default state unless risk trigger is active

defensive_state:
  temporary risk-budget reduction state

risk_trigger:
  may use trend/risk signals, but must not be purely absolute-drawdown-only

relative_recovery_trigger:
  must allow re-entry toward normal state when benchmark-relative recovery occurs

extended_defense_guard:
  must prevent indefinite defensive positioning when terminal wealth drag is accumulating
```

Do not implement state logic in this phase.

## Required Implementation Boundaries for Future Code Phase

Future implementation must:

- Add V2 without mutating V1 behavior.
- Keep V1 tests passing.
- Expose V2 with explicit policy string.
- Make V2 selectable by local evaluation config only in a later explicit phase.
- Keep dataset/NAV/frequency/static-baseline sanity gates.
- Keep evidence export sanitized.
- Add tests proving V1 unchanged.
- Add tests proving V2 target weights satisfy the contract.
- Add tests proving V2 does not use LLM/news/scout/runtime/live modules.
- Not change benchmark metric math.
- Not change data repair logic.

## Required Evidence Gates for Future V2 Evaluation

Gate A: V2 terminal return must exceed S&P 500 TR KRW benchmark terminal return.
Gate B: V2 terminal return must exceed static neutral baseline terminal return.
Gate C: V2 terminal excess return must exceed rules V1 terminal excess return.
Gate D: V2 must pass dataset continuity, NAV sanity, frequency-aware metrics, and static baseline evidence export.
Gate E: V2 must not rely on lower drawdown alone to claim success.
Gate F: V2 must report costs, turnover proxy if available, max relative drawdown, and rules-minus-static comparisons.

## Anti-Overfit and Versioning Protocol

- Constants are fixed before implementation.
- Any parameter change after evidence requires a new policy version.
- Any parameter change requires a stated hypothesis.
- No repeated ad hoc retuning on repaired local evidence.
- Future validation/holdout split must be documented before use.
- No same-history retune loop.

## Prohibited Interpretations

- No investment recommendation.
- No deployment approval.
- No live/paper activation.
- No claim that V2 will beat S&P before evidence.
- No claim that lower MDD alone is success.
- No claim that any security should be bought or sold.
