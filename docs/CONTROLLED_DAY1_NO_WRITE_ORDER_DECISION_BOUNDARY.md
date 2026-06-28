# Controlled Day 1 — No-Write Order-Decision Boundary Inventory

> **Static boundary inventory only.** This is an inventory task, not an implementation task.
> This document maps the current repo's strategy / risk /
> order-decision / write-boundary surfaces so the *next* step can write no-write
> contract tests on a factual basis. It is reading and static inspection only —
> it changes no runtime code, alters no `ops`/`src`/`config` behavior, and
> authorizes no run. Where a requested symbol or behavior does not exist in the
> checked-in repo, it is marked **Not found** rather than invented or created.

## Scope and what this is NOT

- **This is NOT Paper-Day.** It is not the Paper-Day KIS live market-data
  validation track (live quote read / parsing / evidence / envelope / operator
  flow). Do not mix this with Paper-Day docs.
- **This is NOT tiny live order validation.** It is not the tiny-live order path
  and must not become one. The tiny-live surfaces (`src/broker/tiny_live_gate.py`)
  are inventoried here only as an out-of-scope boundary to keep clear of.
- **This IS** a static boundary inventory for Controlled Day 1
  **no-write order-decision readiness**: proving the strategy / risk / order-decision flow
  can reach a *hypothetical* order intent while broker write paths remain
  impossible and no live adapter is constructed.
- **This is static inventory, not implementation.** No runtime code is added or
  changed; missing references are documented as gaps, never created.

## Hard prohibitions (this track)

```text
no live KIS
no network
no live orders
no activation (activation_authorized must stay false)
no daemon
no automatic restart
no live adapter wiring / construction
no submit_order implementation or invocation against a live broker
no tiny-live order runbook
no runtime / ops / src / config behavior change
no raw frame / payload / field-value / URL / token / app key / approval key /
  account / traceback logging
```

Controlled Day 1 readiness is established by **static inspection and offline
tests only**. Nothing in this track runs live KIS, opens the network, constructs
a live adapter, or submits an order. Forbidden operational tokens appear in this
document only as prohibition or boundary assertions.

## Inventory

Each subsection below is one requested category. Every row uses the columns
`Category | File | Symbol / function / class | Current behavior | Safety
implication | Gap / next contract-test need`. A category that has no
corresponding checked-in symbol is marked **Not found** in the File/Symbol cells.

### 1. Strategy / decision path

| Category | File | Symbol / function / class | Current behavior | Safety implication | Gap / next contract-test need |
| --- | --- | --- | --- | --- | --- |
| Strategy / decision path | `src/decision/sqlite_decision_store.py` | `SQLiteDecisionStore`, `DuplicateDecisionIdError` | Persists/loads decisions; rejects duplicate decision ids. | Pure persistence; no broker call, no network. | Contract: decision store path can be exercised with no broker/adapter present. |
| Strategy / decision path | `src/orchestration/active_decision_store.py` | `ActiveDecisionStore`, `DecisionPublicationCandidate` | Holds the active/published decision candidate. | Decision selection only; does not place orders. | Contract: a published decision does not imply any submit. |
| Strategy / decision path | `src/orchestration/decision_refresh_scheduler.py` | `DecisionRefreshScheduler`, `DecisionRefreshRunner` (Protocol) | Schedules decision refresh. | Scheduling only; no execution side effect. | Contract: refresh never triggers a live adapter. |
| Strategy / decision path | `src/orchestration/fast_loop_execution.py` | `ActiveDecisionReader` (Protocol) | Reads the active decision for the fast loop. | Read-only decision input to execution. | Contract: reading a decision is write-free. |
| Strategy / decision path | `src/composition/attended_paper_day.py` | `DeterministicPaperDecisionPublisher` | Deterministically publishes a paper decision in the diagnostic. | Paper-only publisher; no live order. | Confirm reuse here stays paper/no-write under Controlled Day 1. |

### 2. Risk gate / risk checks

| Category | File | Symbol / function / class | Current behavior | Safety implication | Gap / next contract-test need |
| --- | --- | --- | --- | --- | --- |
| Risk gate / risk checks | `src/risk/filter.py` | `RiskFilter.evaluate`, `has_blocking_errors` | Evaluates a `RiskFilterInput` into a `ValidationResult` (blocking issues). | Gate that can refuse an action before any order is generated. | Contract: a blocked result yields no order intent and no submit. |
| Risk gate / risk checks | `src/risk/filter.py` | `_check_mdd_killswitch`, `_check_single_position_cap`, `_check_cash_band` | Individual risk checks (MDD killswitch, position cap, cash band). | Each can produce a blocking issue. | Contract: killswitch/cap/band violations remain decision-only (no live effect). |
| Risk gate / risk checks | `src/risk/rules.py` | `slippage_tolerance_percent`, `money_cap_from_nav_percent`, `mdd_level_from_percent`, … | Pure numeric rule helpers. | No side effects. | Reference only; no write path. |
| Risk gate / risk checks | `src/risk/models.py` | risk input/result models | Typed risk I/O. | Data only. | Reference only. |

### 3. Order intent model / decision artifact

| Category | File | Symbol / function / class | Current behavior | Safety implication | Gap / next contract-test need |
| --- | --- | --- | --- | --- | --- |
| Order intent model / decision artifact | `src/domain/order.py` | `OrderIntent` (BaseModel), `Fill`, `OrderResult`, `OrderStatus` | The order-intent value object and result/fill/status types. | An `OrderIntent` is a *hypothetical intent*; constructing it places nothing. | Contract: a hypothetical `OrderIntent` may be produced with no broker present. |
| Order intent model / decision artifact | `src/risk/order_generation.py` | `OrderIntentGenerator.generate`, `_build_order_intent`, `OrderGenerationResult` | Generates an `OrderIntent` from validated risk input. | Generation is pure; it does not submit. | Contract: generation reaches an intent and stops short of submit. |

### 4. Paper loop input / runner / execution boundary

| Category | File | Symbol / function / class | Current behavior | Safety implication | Gap / next contract-test need |
| --- | --- | --- | --- | --- | --- |
| Paper loop input / runner / execution boundary | `src/paper_loop/models.py` | `PaperLoopInput` (`broker_account_role` must be `PAPER`), `PaperLoopResult`, `generated_order_intent` / `executable_order_intent` / `broker_order_result` | Loop I/O; input validation forces `AccountRole.PAPER`. | Account-role guard pins the loop to paper. | Contract: input rejects any non-PAPER role; result can carry an intent without a broker result. |
| Paper loop input / runner / execution boundary | `src/paper_loop/runner.py` | `PaperLoopRunner.run`; `self._broker.submit_order(...)` (line ~184) | Runs a validated bundle and calls `submit_order` on the injected broker. | **The submit call site.** Default broker is `PaperBrokerAdapter(ledger)`. | Contract: with no broker / a no-write stub, the run must stop before submit (hypothetical-intent-only mode). |
| Paper loop input / runner / execution boundary | `src/execution/trigger_order_bridge.py` | `TriggerOrderBridge.dispatch`, `FireBroker` (Protocol) `.submit_order` | Bridges a fired trigger to broker `submit_order`, then reconciles via ledger. | Another submit call site; broker is a `FireBroker` protocol. | Contract: dispatch can be exercised in a no-write mode that never reaches `submit_order`. |
| Paper loop input / runner / execution boundary | `src/execution/paper_execution_coordinator.py` | paper execution coordinator | Coordinates paper execution. | Paper-only coordination. | Confirm no live path under Controlled Day 1. |
| Paper loop input / runner / execution boundary | `src/orchestration/execution_gate.py` | `SessionHealthExecutionGate.evaluate`, `gate_execution_reason`, `evaluate_gate_safe` | Computes whether execution is gated for a market/time. | Gate can refuse execution before any submit. | Contract: a closed gate yields no submit. |

### 5. Broker adapter abstraction

| Category | File | Symbol / function / class | Current behavior | Safety implication | Gap / next contract-test need |
| --- | --- | --- | --- | --- | --- |
| Broker adapter abstraction | `src/broker/protocols.py` | `BrokerAdapter` (Protocol): `submit_order`, `get_cash`, `get_position`, `list_positions` | The abstract broker contract. | Defines the only write entry point (`submit_order`). | Contract: a no-write run constructs no concrete `BrokerAdapter` that can write. |
| Broker adapter abstraction | `src/broker/paper_broker.py` | `PaperBrokerAdapter`, `.create(...)`, `submit_order` | In-memory paper broker; `submit_order` mutates the local ledger only. | Paper write path (local ledger), never a live venue. | Reference: this is the paper write surface to keep distinct from a no-write run. |

### 6. Live adapter construction point (if any)

| Category | File | Symbol / function / class | Current behavior | Safety implication | Gap / next contract-test need |
| --- | --- | --- | --- | --- | --- |
| Live adapter construction point | `src/broker/kis_live_adapter.py` | `KisLiveReadOnlyBrokerAdapter`, `KisLiveOrderBlockedError` | `submit_order` **raises** `KisLiveOrderBlockedError` ("read-only adapter does not submit orders"); only read-only inquiry is allowed. | The live adapter cannot submit by construction. | Contract: assert `submit_order` raises and is never invoked in a no-write run. |
| Live adapter construction point | **Not found** (no `src/` construction site) | `KisLiveReadOnlyBrokerAdapter(...)` constructor call | **Not found in any `src/` execution path.** Static grep finds no `KisLiveReadOnlyBrokerAdapter(...)` construction in `src/`; the only instantiation is in `tests/test_kis_live_adapter.py` (unit test of the read-only adapter itself). | No production/order-decision path constructs a live adapter — the construction point does not exist to be reached. | Contract: a static guard that no `src/` order-decision path constructs `KisLiveReadOnlyBrokerAdapter`. |
| Live adapter construction point | `src/broker/tiny_live_gate.py` | `TinyLiveGate`, `build_tiny_live_gate`, `validate_tiny_live_manual_gate`, `build_tiny_live_order_request` | Tiny-live order gating surfaces. | **OUT OF SCOPE for this track** — tiny-live is a separate, later path. | Contract: Controlled Day 1 no-write must not touch tiny-live gate construction. |

### 7. Activation / paper / live flags

| Category | File | Symbol / function / class | Current behavior | Safety implication | Gap / next contract-test need |
| --- | --- | --- | --- | --- | --- |
| Activation / paper / live flags | `src/composition/operator_approval_intent.py` | `activation_authorized` fields/checks | Carries operator approval/activation intent. | Activation must never flip true in this track. | Contract: `activation_authorized` stays `false`. |
| Activation / paper / live flags | `src/composition/verified_precheck_receipt.py` | `activation_authorized` (`strict_bool`, must be `False`) | Precheck receipt requires `activation_authorized is False`. | Hard guard against accidental activation. | Contract: reuse this guard in no-write assertions. |
| Activation / paper / live flags | `src/composition/receipt_freshness_policy.py` | `activation_authorized` constant `False` (NO-GO) | Freshness policy holds activation constant false / NO-GO. | Activation is structurally NO-GO here. | Reference invariant. |
| Activation / paper / live flags | `src/composition/attended_paper_day.py` | safety dict: `paper_only=True`, `activation_authorized=False`, `real_order_adapter_constructed=False`, `automatic_restart=False` | Diagnostic emits these constants. | The canonical safety-flag shape to mirror. | Contract: no-write evidence asserts these exact constants. |

### 8. Evidence fields (paper_only / activation_authorized / real_order_adapter_constructed / orders / fills / nonterminal_journal)

| Category | File | Symbol / function / class | Current behavior | Safety implication | Gap / next contract-test need |
| --- | --- | --- | --- | --- | --- |
| Evidence fields | `src/composition/attended_paper_day.py` | safety/evidence dict (~lines 1268–1278) | Serializes `paper_only`, `activation_authorized`, `real_order_adapter_constructed`, `automatic_restart`, `outcome`, `stop_reason`, `nonterminal_journal`, `counters`. | One factual record of the no-write boundary. | Contract: no-write run records this boundary block. |
| Evidence fields | `src/composition/attended_paper_day.py` | `counters.inc("orders")`, `counters.inc("fills")` (~lines 1057–1058) | Increments order/fill counters when an order/fill occurs. | A true no-write run must leave both at zero. | Contract: `orders == 0` and `fills == 0`. |
| Evidence fields | `src/composition/attended_paper_day.py` | `DiagnosticCounters` (~line 261), evidence `sensitive_data_present: False` (~line 318) | Counters + evidence rows mark `sensitive_data_present=false`. | No secret/raw-frame leakage in evidence. | Contract: evidence rows keep `sensitive_data_present=false`. |
| Evidence fields | `src/composition/attended_paper_day.py` | `nonterminal_journal`, `_completion_verdict` → `NO_GO, "nonterminal_journal"` | Nonterminal journal entries force NO_GO. | Stuck journal cannot pass. | Contract: no-write run has no nonterminal journal. |

### 9. Kill switch / abort / stop-reason surfaces

| Category | File | Symbol / function / class | Current behavior | Safety implication | Gap / next contract-test need |
| --- | --- | --- | --- | --- | --- |
| Kill switch / abort / stop-reason surfaces | `src/emergency/mdd.py` | `detect_mdd_killswitch`, `build_mdd_liquidation_plan` | Detects MDD killswitch stage; builds liquidation plan. | Emergency stop path; must not auto-execute live in this track. | Contract: killswitch detection stays decision-only (no live submit). |
| Kill switch / abort / stop-reason surfaces | `src/emergency/models.py` | `EmergencyTriggerType.MDD_KILLSWITCH` | Killswitch trigger type + invariants. | Defines the killswitch contract. | Reference invariant. |
| Kill switch / abort / stop-reason surfaces | `src/config/settings.py` | `ExecutionMode.MDD_KILLSWITCH` | Killswitch execution mode. | Mode enum only. | Reference. |
| Kill switch / abort / stop-reason surfaces | `src/risk/filter.py` | `_check_mdd_killswitch` | Risk-gate killswitch check. | Blocks action on killswitch. | Contract: killswitch block produces no order intent. |
| Kill switch / abort / stop-reason surfaces | `src/composition/attended_paper_day.py` | `stop_reason`, `_completion_verdict`, `_abort_partial_acquire` | Computes terminal `stop_reason` / aborts a partial lock acquire. | Abort/stop reason surface for the run. | Contract: a no-write run terminates with a clean, expected `stop_reason`. |
| Kill switch / abort / stop-reason surfaces | `src/orchestration/execution_gate.py` | `gate_execution_reason`, `evaluate_gate_safe` | Produces an execution-gate reason; safe-evaluates the gate. | Pre-submit refusal surface. | Contract: gated reason yields no submit. |

## No-write contract candidates

Candidate invariants for the next step (contract tests to be written *after* this
inventory is reviewed):

```text
- a hypothetical order intent MAY be produced (OrderIntentGenerator reaches an
  OrderIntent / PaperLoopResult carries generated_order_intent)
- no live adapter is constructed (no KisLiveReadOnlyBrokerAdapter instance in the
  order-decision path; no tiny-live gate construction)
- no submit is called (PaperLoopRunner / TriggerOrderBridge never reach
  submit_order in no-write mode)
- no KIS / broker write path is touched (no live venue write; paper ledger write
  only if explicitly a paper run, otherwise none)
- activation_authorized remains false
- paper_only remains true
- real_order_adapter_constructed remains false
- orders remains zero and fills remains zero
- evidence records the no-write / validation-only boundary
  (paper_only / activation_authorized / real_order_adapter_constructed plus
  orders=0 / fills=0, sensitive_data_present=false)
```

## Future contract gap — evidence/safety-invariant assertion

The evidence/safety invariant (`paper_only` true, `activation_authorized` false,
`real_order_adapter_constructed` false, `orders == 0`, `fills == 0`) is **a future
contract gap**: there is **no standalone public function or model** that emits this
safety block without running the attended Paper-Day diagnostic. The only emitter is
the safety/evidence dict inside `src/composition/attended_paper_day.py`, which
requires an `AttendedPaperDayConfig` + `DiagnosticCounters` and a full diagnostic
run — and `is_clean_pass(...)` does **not** even assert these flags. Per the
no-invention rule, no standalone API is created just to make a test pass. The
invariant is **covered in this inventory** (categories 7 and 8 above) and remains a
**future contract gap** until either a public, run-free safety-block accessor exists
or a Paper-Day-scoped evidence test is wired on its own track. No-write contract
tests therefore assert the constructible/blocked surfaces (intent generation,
blocked live submit, no `src/` live-adapter construction, paper-role enforcement)
and leave the evidence-emission assertion to that future gap.

A docs-only design proposal for closing this gap later (no implementation) lives in
`docs/CONTROLLED_DAY1_NO_WRITE_SAFETY_BLOCK_EMITTER_PROPOSAL.md`.

## Do not proceed to tiny live until

```text
- this boundary inventory is reviewed
- the no-write contract tests are implemented and green
- an operator evidence checklist for the no-write boundary exists
- the acceptance path (ops/acceptance_check.sh) remains green
```

The operator evidence checklist for this no-write boundary lives in
`docs/CONTROLLED_DAY1_NO_WRITE_OPERATOR_EVIDENCE_CHECKLIST.md`. It tells the
Operator which offline/synthetic evidence to collect and when to stop, and
authorizes no live / tiny / write command. The no-write readiness rollup and
exit criteria live in `docs/CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md`.

Tiny-live order validation is a **separate, later** track. It is explicitly out
of scope here, and nothing in Controlled Day 1 no-write readiness authorizes it.
