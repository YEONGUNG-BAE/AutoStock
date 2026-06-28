# Controlled Day 1 — No-Write Safety-Block Emitter Design Proposal (2E)

> **Design proposal only, no implementation.** This describes *how* the run-free
> no-write safety-block emitter gap could be closed later. It adds no runtime code,
> changes no runtime behavior, and authorizes no run.

## Scope and what this is NOT

- **This is NOT Paper-Day.** It is not the Paper-Day KIS live market-data
  validation track.
- **This is NOT tiny-live order validation.** It is not the tiny-live order path.
- **This is NOT an implementation.** No emitter is implemented here; nothing is
  added to runtime code.
- **This is a design proposal only** for a future run-free safety-block emitter.

It authorizes nothing. The following stay prohibited:

```text
no live KIS
no network
no live orders
no activation
no daemon
no automatic restart
no live adapter construction
no submit_order implementation
no tiny-live order path
```

## Problem statement

- 2A/2B/2C/2D currently prove no-write boundaries around intent generation,
  adapter construction, blocked live submit, PAPER role enforcement, and the
  operator evidence checklist:
  - 2A — `docs/CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md` (boundary inventory).
  - 2B — `tests/test_controlled_day1_no_write_order_decision_contract.py` (contract tests).
  - 2C — `docs/CONTROLLED_DAY1_NO_WRITE_OPERATOR_EVIDENCE_CHECKLIST.md` (operator checklist).
  - 2D — `docs/CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md` (rollup / exit criteria).
- But there is **no run-free public emitter** for the full no-write safety block:

```text
paper_only=true
activation_authorized=false
real_order_adapter_constructed=false
orders=0
fills=0
```

- The current emitter lives **inside the attended Paper-Day diagnostic**
  (`src/composition/attended_paper_day.py`) and is not a standalone Controlled
  Day 1 no-write API: it requires an `AttendedPaperDayConfig` + `DiagnosticCounters`
  and a full diagnostic run, and `is_clean_pass(...)` does not even assert these
  flags.
- **Do not invent runtime code in this task.** This document only proposes a shape
  and the decisions needed before any future implementation.

## Design goals

A future emitter, *if* implemented, should be:

```text
run-free
deterministic
no network
no KIS/client dependency
no config/config.toml dependency
no runtime artifacts dependency
no live adapter construction
no broker submit path
reusable by future no-write contract tests
explicit track separation from Paper-Day and tiny-live
```

## Proposed shape

The following is a **proposal only** and is **not** added to runtime code.

- **Function name candidate:** `build_no_write_safety_block(...)` (or a frozen
  model `NoWriteSafetyBlock`) under a Controlled Day 1 no-write namespace, distinct
  from Paper-Day diagnostics.
- **Input candidate:** nothing live — at most a small, explicit value object such
  as counts already known to be zero (or no input at all, since the no-write
  default is constant). No broker, no client, no config, no runtime artifact.
- **Output candidate:** a frozen, JSON-serializable safety block carrying the
  fixed no-write invariants.
- **Required fields:**

```text
paper_only            (default true)
activation_authorized (default false)
real_order_adapter_constructed (default false)
orders                (default 0)
fills                 (default 0)
nonterminal_journal   (empty / absent in a clean no-write block)
validation_only       (or evidence_scope) flag marking this as a no-write,
                      validation-only record rather than a live/paper run
```

Again: **do not add this to runtime code** in this task.

## Contract candidates for future implementation

When the emitter is implemented on its own track, contract tests should assert:

```text
- default output has paper_only=true
- activation_authorized=false
- real_order_adapter_constructed=false
- orders=0
- fills=0
- no broker/client/config/runtime dependency
- cannot represent activation_authorized=true in Controlled Day 1 no-write mode
- cannot construct live adapter
- can be used by future tests without running Paper-Day
```

## Non-goals

```text
no Paper-Day refactor in this task
no tiny-live design
no live order support
no submit_order implementation
no adapter wiring
no executable runbook
no change to ops/src/config behavior
```

## Decision required before implementation

Before any future implementation, these decisions must be made on their own track:

- **Form:** whether to implement the emitter as a **domain model**, a
  **composition helper**, or an **evidence helper**.
- **Sharing:** whether Paper-Day and Controlled Day 1 should
  **share a safety-block shape** or keep separate emitters (a shared shape risks
  coupling tracks; separate emitters risk drift).
- **Backward compatibility:** how to avoid retroactively changing
  **historical Paper-Day evidence semantics** if a shared shape is adopted.
- **Testability:** how future tests should assert the safety block
  **without running live diagnostics** (run-free, deterministic assertions).

## References

- `docs/CONTROLLED_DAY1_NO_WRITE_ORDER_DECISION_BOUNDARY.md` — 2A static boundary inventory.
- `tests/test_controlled_day1_no_write_order_decision_contract.py` — 2B no-write contract tests.
- `docs/CONTROLLED_DAY1_NO_WRITE_OPERATOR_EVIDENCE_CHECKLIST.md` — 2C operator evidence checklist.
- `docs/CONTROLLED_DAY1_NO_WRITE_READINESS_ROLLUP.md` — 2D readiness rollup / exit criteria.
